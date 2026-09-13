"""`app.infrastructure.providers.ucs_central.client`.

Same contract as the UCS Manager client — callers see exactly one error
type no matter which layer failed — plus the one thing that client does
not have to do: impose a timeout. `ucscsdk` has no timeout parameter
anywhere (`UcscHandle.__init__` takes none, and `ucscsession.post` calls
`ucscdriver.post` without forwarding one, so `urlopen` runs with
`timeout=None`), so the deadline is the wrapper's own and is worth
testing.

`ucscsdk` itself is not exercised here; the real `UcscHandle` is swapped
for a stub after construction.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.error import URLError

import pytest
from ucscsdk.ucscexception import UcscException, UcscLoginError

from app.infrastructure.providers.ucs_central.client import (
    UcsCentralClient,
    UcsCentralConnectionError,
    _validate_endpoint,
)

pytestmark = pytest.mark.unit


class StubHandle:
    def __init__(
        self, *, error: Exception | None = None, result: Any = None, block_seconds: float = 0.0
    ) -> None:
        self.ip = "central.lab.example.com"
        self._error = error
        self._result = result
        self._block_seconds = block_seconds
        self.calls: list[str] = []

    def _maybe_raise(self) -> None:
        if self._block_seconds:
            # Stands in for urllib blocking with no timeout of its own.
            import time

            time.sleep(self._block_seconds)
        if self._error is not None:
            raise self._error

    def login(self) -> bool:
        self.calls.append("login")
        self._maybe_raise()
        return True

    def logout(self) -> bool:
        self.calls.append("logout")
        self._maybe_raise()
        return True

    def query_classid(self, class_id: str) -> Any:
        self.calls.append(f"query_classid:{class_id}")
        self._maybe_raise()
        return self._result


def _client(handle: StubHandle, *, timeout_seconds: float = 5.0) -> UcsCentralClient:
    client = UcsCentralClient(
        endpoint="central.lab.example.com",
        username="admin",
        password="secret",
        timeout_seconds=timeout_seconds,
    )
    client._handle = handle  # ty: ignore[invalid-assignment]
    return client


class TestEndpointValidation:
    """`__create_uri` interpolates the endpoint raw, so a URL becomes
    "https://https://host:443" and fails as an opaque connection error.
    """

    def test_accepts_a_bare_host(self) -> None:
        assert _validate_endpoint("  central.example.com  ") == "central.example.com"

    def test_rejects_a_url(self) -> None:
        with pytest.raises(ValueError, match="bare hostname or IP"):
            _validate_endpoint("https://central.example.com")

    def test_rejects_an_embedded_port(self) -> None:
        # ucscsdk hardcodes 443 and raises for anything else, so a port is
        # always wrong here — unlike ucsmsdk, where it is merely unsupported.
        with pytest.raises(ValueError, match="must not include a port"):
            _validate_endpoint("central.example.com:8443")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="is empty"):
            _validate_endpoint("   ")


class TestErrorTranslation:
    """Both SDK exception roots and raw network errors must surface as one
    type, so callers never have to know which layer broke.
    """

    @pytest.mark.parametrize(
        "error",
        [
            UcscLoginError("authentication failed"),
            UcscException("ERR-xml-parse-error", "bad request"),
            URLError("connection refused"),
            OSError("host unreachable"),
        ],
    )
    async def test_login_failures_become_one_error_type(self, error: Exception) -> None:
        with pytest.raises(UcsCentralConnectionError):
            await _client(StubHandle(error=error)).login()

    @pytest.mark.parametrize(
        "error",
        [UcscException("ERR", "denied"), URLError("reset by peer")],
    )
    async def test_query_failures_become_one_error_type(self, error: Exception) -> None:
        with pytest.raises(UcsCentralConnectionError, match="computeSystem"):
            await _client(StubHandle(error=error)).query_classid("computeSystem")

    async def test_logout_failure_is_swallowed(self) -> None:
        """Always called from `finally`, so it must never mask the error
        the caller is already handling.
        """
        handle = StubHandle(error=UcscException("ERR", "already gone"))
        await _client(handle).logout()
        assert handle.calls == ["logout"]


class TestTimeout:
    """The SDK provides no timeout at all, so without this a wedged UCS
    Central hangs the collector until Kubernetes kills the pod, with no
    logged reason.
    """

    async def test_a_hung_call_fails_instead_of_blocking_forever(self) -> None:
        client = _client(StubHandle(block_seconds=0.5), timeout_seconds=0.05)
        with pytest.raises(UcsCentralConnectionError, match="timed out"):
            await asyncio.wait_for(client.query_classid("computeBlade"), timeout=5.0)

    async def test_the_timeout_message_names_the_missing_sdk_feature(self) -> None:
        """So whoever reads the log doesn't go looking for an SDK timeout
        setting to tune.
        """
        client = _client(StubHandle(block_seconds=0.5), timeout_seconds=0.05)
        with pytest.raises(UcsCentralConnectionError, match="ucscsdk has no timeout of its own"):
            await client.login()

    async def test_a_timed_out_client_refuses_every_further_call(self) -> None:
        """`ucscsdk` cannot cancel the abandoned thread, which may still be
        running against `self._handle`; a second call would risk two
        threads on one session.
        """
        client = _client(StubHandle(block_seconds=0.5), timeout_seconds=0.05)
        with pytest.raises(UcsCentralConnectionError, match="timed out"):
            await client.login()

        with pytest.raises(UcsCentralConnectionError, match="never returned within its"):
            await client.query_classid("computeBlade")

    async def test_a_timed_out_calls_thread_is_not_joined_at_interpreter_exit(self) -> None:
        """A daemon thread is never in `concurrent.futures.thread`'s
        join-at-exit bookkeeping, unlike a `ThreadPoolExecutor`'s workers.
        See `app.infrastructure.blocking`'s module docstring.
        """
        import concurrent.futures.thread as cf_thread
        import threading

        before = set(threading.enumerate())
        client = _client(StubHandle(block_seconds=0.5), timeout_seconds=0.05)
        with pytest.raises(UcsCentralConnectionError, match="timed out"):
            await client.login()

        [spawned] = set(threading.enumerate()) - before
        assert spawned.daemon
        assert spawned.name.startswith("ucs-central-")
        assert spawned not in cf_thread._threads_queues


class TestQueryResults:
    async def test_returns_a_list(self) -> None:
        client = _client(StubHandle(result=["a", "b"]))
        assert await client.query_classid("computeSystem") == ["a", "b"]

    async def test_none_becomes_an_empty_list(self) -> None:
        """A domain with nothing of that class is not an error."""
        client = _client(StubHandle(result=None))
        assert await client.query_classid("computeRackUnit") == []
