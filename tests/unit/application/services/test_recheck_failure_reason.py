"""A vendor failure's 503 reason is bounded at its source (ADR-0002, 2026-11-09 addendum)."""

from __future__ import annotations

from app.application.services.available_servers import recheck_failure_reason


def test_a_short_failure_is_named_with_its_type_unchanged() -> None:
    assert recheck_failure_reason(TimeoutError("timed out after 15.0s")) == (
        "TimeoutError: timed out after 15.0s"
    )


def test_a_multi_mb_failure_is_cut_and_says_where_the_rest_is() -> None:
    reason = recheck_failure_reason(RuntimeError("x" * (3 * 1024 * 1024)))

    assert reason.startswith("RuntimeError: xxx")
    assert len(reason) < 600
    assert "more characters in the available.recheck_failed log line" in reason
