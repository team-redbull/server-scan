"""Seeded warranty documents for the fake fleet, in the operator's own shape (ADR-0070)."""

from __future__ import annotations

import zlib
from datetime import date, timedelta
from typing import Any

# Marks a seeded document, so a re-seed deletes only its own and never a real record.
SEED_MARKER = "_server_scan_seed"


def _bucket(serial: str, salt: str) -> int:
    """
    A stable 0-99 draw for one serial.

    Args:
        serial (str): The server's serial.
        salt (str): Separates independent draws.

    Returns:
        int: 0-99.
    """
    return zlib.crc32(f"{salt}/{serial}".encode()) % 100


def _doc(serial: str, expires: date | None, today: date) -> dict[str, Any]:
    """
    One document as the writer stores it (`DD-MM-YYYY`, an `Expired` text).

    Args:
        serial (str): The server's serial.
        expires (date | None): The end date; None writes an empty date.
        today (date): For the writer's own `Expired` text.

    Returns:
        dict[str, Any]: The document.
    """
    return {
        "Serial": serial,
        "Warranty": expires.strftime("%d-%m-%Y") if expires else "",
        "Expired": "Expired" if expires and expires < today else "Valid",
        "Network": "lab",
        SEED_MARKER: True,
    }


def fake_warranty_docs(serials: list[str], today: date) -> list[dict[str, Any]]:
    """
    Documents for the fleet: ~4% missing, ~4% empty date, ~15% expired, ~12% expiring soon.

    Args:
        serials (list[str]): Every physical server's serial.
        today (date): Dates are relative to it, so the mix holds on any day.

    Returns:
        list[dict[str, Any]]: The documents; a few serials get an older duplicate.
    """
    docs: list[dict[str, Any]] = []
    for serial in serials:
        draw = _bucket(serial, "kind")
        spread = zlib.crc32(f"days/{serial}".encode())
        if draw < 4:
            continue
        if draw < 8:
            docs.append(_doc(serial, None, today))
            continue
        if draw < 23:
            expires = today - timedelta(days=1 + spread % 900)
        elif draw < 35:
            expires = today + timedelta(days=spread % 90)
        else:
            expires = today + timedelta(days=91 + spread % 1400)
        docs.append(_doc(serial, expires, today))
        if _bucket(serial, "dup") < 3:
            docs.append(_doc(serial, expires - timedelta(days=365), today))
    return docs
