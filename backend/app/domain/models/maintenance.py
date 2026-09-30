"""Maintenance state, embedded on `Server`.

Deliberately independent of both classification and health: a server can
simultaneously be HOSTED_CLUSTER, CRITICAL, and in maintenance. Full CRUD
for maintenance (with its own audit trail) is a slice-4 concern; the shape
is declared now so it's part of the document from the start.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Maintenance(BaseModel):
    """The maintenance-mode state embedded on a `Server` document."""

    enabled: bool = False
    reason: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expected_end: datetime | None = None
