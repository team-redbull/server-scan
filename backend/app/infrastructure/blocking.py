"""Run a blocking third-party call without mortgaging interpreter shutdown.

`ucscsdk` accepts no timeout at all (`UcscHandle.__init__(self, ip,
username, password, port=443, proxy=None)`, `UcscSession.__init__` the
same; its driver calls `opener.open(request, timeout=None)`), and
`ucsmsdk`'s `timeout=` reaches only `urllib`'s per-socket-operation
deadline — one `connect`/`recv`, not the call, and not DNS resolution,
which `socket.create_connection` performs *before* that timeout is even
applied. Neither SDK's blocking call can be cancelled once it has started.

`asyncio.wait_for`/`asyncio.timeout` abandon the *await*; the OS thread
underneath keeps running. Which is where `asyncio.to_thread` becomes a
liability rather than a convenience: it routes unconditionally through
`loop.run_in_executor(None, ...)` — the event loop's own default
`ThreadPoolExecutor` — and an abandoned worker there costs, in order:
`asyncio.Runner.close()` calling `loop.shutdown_default_executor(
constants.THREAD_JOIN_TIMEOUT)`, i.e. 300 seconds of waiting (verified
against this project's own installed CPython 3.13.15); and then, because
`concurrent.futures.thread` registers every worker thread — from *any*
`ThreadPoolExecutor`, not only the default one — in a module-global
`_threads_queues`, joined with **no timeout at all** by `_python_exit()`
(hooked via `threading._register_atexit`, which runs during interpreter
shutdown, after `asyncio.run()` has already returned), an unbounded hang
on top of that. One wedged UCS domain therefore stalled the collector
pod forever, *after* it had already logged the timeout and moved on.

Giving the client its own dedicated `ThreadPoolExecutor` does not help —
this was tried and measured, not assumed. Its worker threads are still
plain `threading.Thread(...)` with no `daemon=True` (there is no
constructor argument for it: `ThreadPoolExecutor.__init__(self,
max_workers=None, thread_name_prefix='', initializer=None,
initargs=())`), and they land in the exact same `_threads_queues`
registry. Measured on this project's interpreter: both the default
executor and a dedicated one hang indefinitely once a call wedges; only a
daemon thread exits promptly.

So: a plain daemon thread, created directly with `threading.Thread`, which
`concurrent.futures` never sees and the interpreter never joins for —
"The entire Python program exits when only daemon threads are left";
"Daemon threads are abruptly stopped at shutdown"
(docs.python.org/3/library/threading.html). Its result is bridged back
into the event loop with the public `asyncio.wrap_future`, so the *await*
is still governed normally by `asyncio.wait_for`/`asyncio.timeout` even
though the thread itself, if wedged, is simply never joined.

This is deliberately not a general-purpose off-loop helper — it leaks a
thread on purpose if `func` never returns. Every caller must impose its
own deadline (this module imposes none), and should track that it happened
at all (see `docs/cisco-collectors.md`'s "poisoned handle" note): a client
whose SDK handle a wedged thread may still be mutating must not be reused
for a second call, or two threads can touch the same session at once.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from typing import Any


async def run_abandonable[T](func: Callable[..., T], /, *args: Any, name: str) -> T:
    """
    Run `func(*args)` on a daemon thread and await its result.

    Args:
        func (Callable[..., T]): The blocking, uncancellable callable.
        *args (Any): Positional arguments forwarded to `func`.
        name (str): Thread name, for `py-spy`/`faulthandler` output when a
            call really does wedge — the whole point is that this thread
            can outlive the `await` below, so it has to be identifiable.

    Returns:
        T: Whatever `func` returned.

    Raises:
        BaseException: Whatever `func` raised, re-raised on the loop.
    """
    bridge: concurrent.futures.Future[T] = concurrent.futures.Future()

    def _runner() -> None:
        # As a real `Executor` does: if the caller already cancelled
        # `bridge` (a `wait_for` deadline fired first), skip the call.
        if not bridge.set_running_or_notify_cancel():
            return
        try:
            bridge.set_result(func(*args))
        except BaseException as exc:
            bridge.set_exception(exc)

    threading.Thread(target=_runner, name=name, daemon=True).start()
    return await asyncio.wrap_future(bridge)
