from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

# Point the app at the CI/dev-up Mongo+Redis before any app module (which
# reads Settings at import/dependency time) is imported.
os.environ.setdefault("INVENTORY_MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("INVENTORY_REDIS_URI", "redis://localhost:6379/0")
os.environ.setdefault("INVENTORY_ENVIRONMENT", "test")
os.environ.setdefault("INVENTORY_MONGO_DB", "server-scan-test")


def _isolate_xdist_worker() -> None:
    """
    Give each pytest-xdist worker its own Mongo and Redis database (ADR-0049).

    A no-op without xdist (no `PYTEST_XDIST_WORKER`).
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker or not worker.startswith("gw"):
        return
    number = int(worker[2:])
    os.environ["INVENTORY_MONGO_DB"] = f"{os.environ['INVENTORY_MONGO_DB']}-{worker}"
    uri = urlsplit(os.environ["INVENTORY_REDIS_URI"])
    # Redis has 16 databases: /0 stays free for a plain run, workers 15+ would share one.
    os.environ["INVENTORY_REDIS_URI"] = urlunsplit(uri._replace(path=f"/{1 + number % 15}"))


_isolate_xdist_worker()

# asyncio_mode = "auto" in pyproject.toml means pytest-asyncio picks up
# every `async def test_...` automatically — no per-test marker needed.
