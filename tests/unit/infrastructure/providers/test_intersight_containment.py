"""Intersight: one bad server never aborts the run; odd table failures stay contained."""

from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.unit.infrastructure.providers.test_intersight_provider import (
    _TABLES,
    _collect,
    _FakeClient,
    _provider,
)

from app.domain.enums import UnreachableReason
from app.domain.services.normalize import normalize_text
from app.infrastructure.providers.intersight import provider as isv
from app.infrastructure.providers.intersight.provider import IntersightProvider

pytestmark = pytest.mark.unit


def _three() -> dict[str, list[dict[str, Any]]]:
    """
    The shared estate with a third server.

    Returns:
        dict[str, list[dict[str, Any]]]: Tables with three summaries.
    """
    tables = dict(_TABLES)
    tables["compute/PhysicalSummaries"] = [
        {"Moid": f"s{i}", "Name": f"n{i}", "Serial": f"SER{i}", "ManagementMode": "Intersight"}
        for i in (1, 2, 3)
    ]
    return tables


async def test_a_mapper_crash_on_the_second_server_costs_only_that_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Servers 1 and 3 are yielded; 2 is a COLLECTION_ERROR stub keyed like a real mapping."""
    real = isv.mapping.to_provider_server

    def flaky(summary: Any, **kwargs: Any) -> Any:
        """
        Map, except server 2.

        Args:
            summary (Any): The summary.
            **kwargs (Any): Passed on.

        Returns:
            Any: The mapped server.

        Raises:
            KeyError: For server 2.
        """
        if summary["Moid"] == "s2":
            raise KeyError("Health")
        return real(summary, **kwargs)

    monkeypatch.setattr(isv.mapping, "to_provider_server", flaky)
    provider = _provider(_FakeClient(_three()))
    with capture_logs() as logs:
        servers = await _collect(provider)

    assert [s.serial for s in servers] == ["SER1", "SER2", "SER3"]
    stub = servers[1]
    assert stub.reachable is False
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert stub.external_id == "intersight/s2"
    assert stub.vendor == "cisco"
    assert stub.unreachable_detail is not None and "KeyError" in stub.unreachable_detail
    assert provider.server_failures == 1
    assert any("n2" in e and "KeyError" in e for e in provider.collection_errors)
    event = next(e for e in logs if e["event"] == "intersight.server_failed")
    assert (event["moid"], event["serial"], event["name"]) == ("s2", "SER2", "n2")
    summary = next(e for e in logs if e["event"] == "intersight.run_summary")
    assert summary["server_failures"] == 1
    assert summary["unreadable_subresources"] == 0
    assert summary["budget_errors"] == 0


async def test_the_stub_has_the_same_identity_as_a_clean_mapping_of_the_same_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different identity would make ingest write a second document for server 2."""
    clean = (await _collect(_provider(_FakeClient(_three()))))[1]
    real = isv.mapping.to_provider_server

    def flaky(summary: Any, **kwargs: Any) -> Any:
        """
        Map, except server 2.

        Args:
            summary (Any): The summary.
            **kwargs (Any): Passed on.

        Returns:
            Any: The mapped server.

        Raises:
            KeyError: For server 2.
        """
        if summary["Moid"] == "s2":
            raise KeyError("Health")
        return real(summary, **kwargs)

    monkeypatch.setattr(isv.mapping, "to_provider_server", flaky)
    stub = (await _collect(_provider(_FakeClient(_three()))))[1]

    def identity(ps: Any) -> tuple[Any, ...]:
        """
        The fields ingest correlates on, plus name and manager.

        Args:
            ps (Any): A provider server.

        Returns:
            tuple[Any, ...]: Vendor, normalized serial, external id, name, manager id.
        """
        return (ps.vendor, normalize_text(ps.serial), ps.external_id, ps.name, ps.manager_id)

    assert stub.reachable is False and clean.reachable is True
    assert identity(stub) == identity(clean)


class _OddClient(_FakeClient):
    """A client whose `compute/Boards` query raises a non-Intersight error."""

    async def list_all(self, resource: str, **kwargs: Any) -> Any:
        """
        Delegate, but blow up oddly on one resource.

        Args:
            resource (str): The resource.
            **kwargs (Any): Passed on.

        Yields:
            dict[str, Any]: Rows.

        Raises:
            ValueError: For `compute/Boards`.
        """
        if resource == "compute/Boards":
            raise ValueError("odd payload")
        async for row in super().list_all(resource, **kwargs):
            yield row


async def test_a_non_intersight_error_in_a_table_leaves_it_unread_and_recorded() -> None:
    """The table is `None`, its dependants are named as skipped, the run carries on."""
    provider = _provider(_OddClient())
    with capture_logs() as logs:
        servers = await _collect(provider)
    assert len(servers) == 2
    errors = provider.collection_errors
    assert any("compute/Boards" in e and "ValueError" in e for e in errors)
    for table in ("storage/Controllers", "graphics/Cards", "processor/Units"):
        assert any(e.startswith(table) and "compute/Boards" in e for e in errors)
    summary = next(e for e in logs if e["event"] == "intersight.run_summary")
    assert summary["unreadable_subresources"] == 1


async def test_a_summary_without_moid_is_logged_and_recorded() -> None:
    """The silent skip is now visible."""
    tables = dict(_TABLES)
    tables["compute/PhysicalSummaries"] = [
        {"Name": "ghost", "Serial": "G1", "ManagementMode": "Intersight"}
    ]
    provider: IntersightProvider = _provider(_FakeClient(tables))
    with capture_logs() as logs:
        servers = await _collect(provider)
    assert servers == []
    event = next(e for e in logs if e["event"] == "intersight.summary_without_moid")
    assert event["serial"] == "G1"
    assert any("no Moid" in e and "G1" in e for e in provider.collection_errors)


async def test_get_one_escapes_the_moid_hint_in_its_filter() -> None:
    """A quote in the Moid hint is doubled, not interpolated raw."""
    seen: list[str | None] = []

    class _Spy(_FakeClient):
        async def list_all(self, resource: str, **kwargs: Any) -> Any:
            """
            Record the filter and yield nothing.

            Args:
                resource (str): The resource.
                **kwargs (Any): Includes `filter_expr`.

            Yields:
                dict[str, Any]: Never.
            """
            seen.append(kwargs.get("filter_expr"))
            return
            yield {}

    from app.domain.ports.provider import ServerIdentity

    await _provider(_Spy()).get_one(ServerIdentity(external_id="intersight/a'b"))
    assert seen == ["Moid eq 'a''b'"]
