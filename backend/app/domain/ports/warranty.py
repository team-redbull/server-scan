"""Where server warranties are read from: an outside collection, never written (ADR-0070)."""

from __future__ import annotations

from typing import Protocol

from app.domain.models.warranty import Warranty


class WarrantySource(Protocol):
    """Read-only access to the warranty collection, keyed by normalized serial."""

    async def all(self) -> dict[str, Warranty] | None:
        """
        Every serial's warranty, for the monthly sync.

        Returns:
            dict[str, Warranty] | None: Normalized serial -> warranty (`found=True`); None when
                the collection could not be read at all.
        """
        ...

    async def one(self, serial: str) -> Warranty | None:
        """
        One serial's warranty, for a newly created server and "Fetch now".

        Args:
            serial (str): The server's serial as collected.

        Returns:
            Warranty | None: `found=False` when no document has it; None when the
                collection could not be read.
        """
        ...
