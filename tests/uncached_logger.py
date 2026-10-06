"""A module-level logger stand-in that `structlog.testing.capture_logs()` can always see.

`create_app()` configures `cache_logger_on_first_use=True`, which pins the processors a
module's logger had when an earlier test first used it; this resolves them on every call.
"""

from __future__ import annotations

import structlog


class UncachedLogger:
    """Delegates every attribute to a fresh `structlog.get_logger()`."""

    def __getattr__(self, name: str) -> object:
        return getattr(structlog.get_logger(), name)
