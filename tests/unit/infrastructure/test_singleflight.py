"""`app.infrastructure.singleflight.coalesce` — in-process request
coalescing, added in the slice 6 performance pass after `tools/loadtest.py`
found a cache-stampede tail-latency problem on `GET /api/v1/servers`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from app.infrastructure import singleflight
from app.infrastructure.singleflight import coalesce, drain

pytestmark = pytest.mark.unit


async def test_concurrent_identical_keys_share_one_computation() -> None:
    call_count = 0
    started = asyncio.Event()

    async def compute() -> str:
        nonlocal call_count
        call_count += 1
        started.set()
        await asyncio.sleep(0.05)
        return "result"

    async def waiter() -> str:
        await started.wait()
        return await coalesce("k", compute)

    first = asyncio.create_task(coalesce("k", compute))
    # Give `first` a chance to register itself and start `compute()` before
    # the others arrive, so they observe the in-flight future rather than
    # racing to register their own.
    others = [asyncio.create_task(waiter()) for _ in range(9)]

    results = await asyncio.gather(first, *others)

    assert call_count == 1
    assert results == ["result"] * 10


async def test_distinct_keys_do_not_block_each_other() -> None:
    call_counts: dict[str, int] = {"a": 0, "b": 0}

    async def make_compute(key: str) -> Callable[[], Awaitable[str]]:
        async def compute() -> str:
            call_counts[key] += 1
            await asyncio.sleep(0.01)
            return key

        return compute

    results = await asyncio.gather(
        coalesce("a", await make_compute("a")),
        coalesce("b", await make_compute("b")),
    )

    assert results == ["a", "b"]
    assert call_counts == {"a": 1, "b": 1}


async def test_exception_propagates_to_every_waiter() -> None:
    started = asyncio.Event()

    async def compute() -> str:
        started.set()
        await asyncio.sleep(0.02)
        raise ValueError("boom")

    async def waiter() -> None:
        await started.wait()
        await coalesce("err", compute)

    first = asyncio.create_task(coalesce("err", compute))
    others = [asyncio.create_task(waiter()) for _ in range(3)]

    results = await asyncio.gather(first, *others, return_exceptions=True)

    assert all(isinstance(r, ValueError) for r in results)


async def test_a_later_call_after_completion_runs_fresh() -> None:
    call_count = 0

    async def compute() -> int:
        nonlocal call_count
        call_count += 1
        return call_count

    first = await coalesce("seq", compute)
    second = await coalesce("seq", compute)

    assert first == 1
    assert second == 2


async def test_a_cancelled_waiter_does_not_fail_the_leader() -> None:
    """Failure mode 1 of the singleflight module docstring: a cancelled waiter
    used to cancel the shared bare `Future` and crash the leader with
    `InvalidStateError`.
    """
    started = asyncio.Event()

    async def compute() -> str:
        started.set()
        await asyncio.sleep(0.05)
        return "result"

    leader = asyncio.create_task(coalesce("waiter-cancel", compute))
    await started.wait()
    waiter = asyncio.create_task(coalesce("waiter-cancel", compute))
    await asyncio.sleep(0)  # let `waiter` register against the same task
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert await leader == "result"


async def test_a_cancelled_leader_does_not_fail_the_waiters() -> None:
    """Failure mode 2 of the same docstring: a cancelled leader used to store
    `CancelledError` onto the shared Future, failing every uninvolved waiter.
    """
    call_count = 0
    started = asyncio.Event()

    async def compute() -> str:
        nonlocal call_count
        call_count += 1
        started.set()
        await asyncio.sleep(0.05)
        return "result"

    first = asyncio.create_task(coalesce("leader-cancel", compute))
    await started.wait()
    second = asyncio.create_task(coalesce("leader-cancel", compute))
    await asyncio.sleep(0)
    first.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first

    assert await second == "result"
    assert call_count == 1


async def test_the_entry_stays_until_the_task_settles_even_if_every_caller_cancels() -> None:
    """The running computation owns the key until it finishes; dropping the
    entry on a caller's cancel would let a second caller start a duplicate.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def compute() -> str:
        started.set()
        await release.wait()
        return "result"

    waiter = asyncio.create_task(coalesce("stays", compute))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert "stays" in singleflight._inflight
    inner_task = singleflight._inflight["stays"]
    release.set()
    await inner_task  # let it actually finish and its done-callback run
    assert "stays" not in singleflight._inflight


async def test_an_unretrieved_exception_is_logged_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure every waiter cancelled away from must be logged, not left to
    asyncio's GC-time warning. Spied via `monkeypatch`, not `capture_logs()`:
    `cache_logger_on_first_use` freezes a logger's processors on its first use.
    """
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        singleflight.logger, "warning", lambda event, **kw: calls.append((event, kw))
    )
    started = asyncio.Event()

    async def compute() -> str:
        started.set()
        await asyncio.sleep(0.02)
        raise ValueError("boom")

    waiter = asyncio.create_task(coalesce("logged", compute))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    await asyncio.sleep(0.05)  # let compute() finish and the done-callback run

    assert calls == [("singleflight.unretrieved_exception", {"key": "logged", "error": "boom"})]


async def test_eager_task_execution_still_cleans_up_the_entry() -> None:
    """Under `asyncio.eager_task_factory` `compute()` can finish inside
    `create_task()`, before `_inflight` registration; cleanup via
    `add_done_callback` after insertion survives that (see `coalesce`'s comment).
    """
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:

        async def instant_compute() -> str:
            return "eager-result"  # never awaits/suspends

        result = await coalesce("eager", instant_compute)
        assert result == "eager-result"
        await asyncio.sleep(0)  # let the done-callback's call_soon fire
        assert "eager" not in singleflight._inflight
    finally:
        loop.set_task_factory(previous_factory)


async def test_drain_cancels_every_in_flight_computation() -> None:
    """The lifespan-shutdown hook: a computation nobody is waiting on any
    more must not be left running against clients `drain()`'s caller is
    about to close.
    """
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def compute() -> str:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "result"

    task = asyncio.create_task(coalesce("drain-me", compute))
    await started.wait()

    await drain()

    assert cancelled.is_set()
    assert "drain-me" not in singleflight._inflight
    with pytest.raises(asyncio.CancelledError):
        await task
