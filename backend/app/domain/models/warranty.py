"""A server's warranty, copied from the operator's warranty collection (ADR-0070)."""

from __future__ import annotations

from pydantic import BaseModel


class Warranty(BaseModel):
    """
    What the warranty collection said about this server's serial when it was last looked up.

    Whether it is expired is never stored: the UI compares `expires_on` with today, so it is
    right on the day it expires, not on the next sync.

    Attributes:
        found (bool): False when no document has this serial.
        expires_on (str | None): The warranty's end date, ISO `YYYY-MM-DD`; None when the
            document's date was empty or unreadable ("not read").
    """

    found: bool
    expires_on: str | None = None
