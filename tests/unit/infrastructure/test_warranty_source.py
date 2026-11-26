"""The warranty collection's dates and duplicates (ADR-0070)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.domain.models.warranty import Warranty
from app.infrastructure.mongodb.warranty_source import MongoWarrantySource, parse_warranty_date


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("29-01-2025", "2025-01-29"),
        (" 01-12-2027 ", "2027-12-01"),
        ("", None),
        ("   ", None),
        (None, None),
        ("2025-01-29", None),
        ("31-02-2025", None),
        (20250129, None),
        (datetime(2025, 1, 29, 10, 0), "2025-01-29"),
        (date(2025, 1, 29), "2025-01-29"),
    ],
)
def test_the_writer_dd_mm_yyyy_date_is_read_and_anything_else_is_not(
    value: object, expected: str | None
) -> None:
    """Only `DD-MM-YYYY` (or a stored date) counts; an empty or other value is "not read"."""
    assert parse_warranty_date(value) == expected


def _fold(docs: list[dict[str, object]]) -> dict[str, Warranty]:
    """Group documents the way the source does."""
    return MongoWarrantySource("mongodb://unused", "db", "c")._fold(docs)


def test_serials_are_matched_case_and_space_insensitively() -> None:
    """The key is the same normalization `Server.identity.serial_normalized` uses."""
    assert set(_fold([{"Serial": "  WMP25492815 ", "Warranty": "29-01-2025"}])) == {"wmp25492815"}


def test_the_latest_date_wins_when_a_serial_has_several_documents() -> None:
    """A renewal is a second document; its later date is the warranty."""
    folded = _fold(
        [
            {"Serial": "A1", "Warranty": "29-01-2025"},
            {"Serial": "a1", "Warranty": "29-01-2027"},
            {"Serial": "A1", "Warranty": "01-01-2026"},
        ]
    )
    assert folded["a1"] == Warranty(found=True, expires_on="2027-01-29")


def test_a_readable_date_beats_an_empty_one_for_the_same_serial() -> None:
    """An empty duplicate never hides a real date."""
    folded = _fold([{"Serial": "A1", "Warranty": ""}, {"Serial": "A1", "Warranty": "29-01-2025"}])
    assert folded["a1"].expires_on == "2025-01-29"


def test_an_empty_date_is_found_but_not_read() -> None:
    """The record exists, so it is not "no record"; its date is simply unknown."""
    assert _fold([{"Serial": "A1", "Warranty": ""}])["a1"] == Warranty(found=True, expires_on=None)


def test_a_document_without_a_serial_is_skipped() -> None:
    """It cannot belong to any server."""
    assert _fold([{"Serial": "", "Warranty": "29-01-2025"}, {"Warranty": "29-01-2025"}]) == {}
