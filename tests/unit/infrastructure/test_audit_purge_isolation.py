"""The audit purge is reachable only from `tools/`, never from `backend/app` (ADR-0045)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_APP = Path(__file__).resolve().parents[3] / "backend" / "app"
_REPOSITORY = _APP / "infrastructure" / "mongodb" / "audit_event_repository.py"
_FORBIDDEN = ("purge_before", "preview_before", "PurgeProgress")


def test_nothing_in_the_app_references_the_purge_but_its_own_repository() -> None:
    offenders = [
        str(path.relative_to(_APP))
        for path in _APP.rglob("*.py")
        if path != _REPOSITORY and any(name in path.read_text() for name in _FORBIDDEN)
    ]
    assert offenders == []
