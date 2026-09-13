"""`_carry_forward`'s second job: naming what this run could not read.

The resolved value can never answer the question on its own — a carried
`[]` and a genuinely-empty `[]` are the same list — so the accumulator is
the only place the distinction survives to the document.
"""

from __future__ import annotations

import pytest

from app.application.services.ingest import _carry_forward

pytestmark = pytest.mark.unit


def test_a_reported_value_is_not_recorded() -> None:
    unread: list[str] = []
    assert _carry_forward(7, 3, default=0, unread=unread, name="hardware.cpu.cores") == 7
    assert unread == []


def test_a_reported_empty_value_is_not_recorded() -> None:
    """ "Read it, there are none" is a real answer, not a failure to read."""
    unread: list[str] = []
    assert _carry_forward([], ["a"], default=[], unread=unread, name="hardware.gpus") == []
    assert unread == []


def test_an_unread_field_is_recorded_even_though_a_value_is_carried() -> None:
    unread: list[str] = []
    resolved = _carry_forward(
        None, ["a"], default=[], unread=unread, name="hardware.storage.drives"
    )
    assert resolved == ["a"]
    assert unread == ["hardware.storage.drives"]


def test_an_unread_field_on_a_new_server_is_recorded_with_its_default() -> None:
    unread: list[str] = []
    assert _carry_forward(None, None, default=0, unread=unread, name="hardware.cpu.cores") == 0
    assert unread == ["hardware.cpu.cores"]


def test_the_accumulator_holds_only_what_was_passed_to_it() -> None:
    """A fresh list per ingest keeps the record per-run: `_build_server` hands
    one to every field, so the stored list is replaced, never merged.
    """
    first: list[str] = []
    _carry_forward(None, None, default=0, unread=first, name="hardware.cpu.cores")

    second: list[str] = []
    _carry_forward(2, None, default=0, unread=second, name="hardware.cpu.cores")

    assert first == ["hardware.cpu.cores"]
    assert second == []
