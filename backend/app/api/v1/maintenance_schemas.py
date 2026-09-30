"""Request schema for `PUT /api/v1/servers/{server_id}/maintenance`.

Response reuses `ServerDetail` (the server itself, with the updated
`maintenance` block visible) rather than a bespoke maintenance-only
response — a caller enabling maintenance almost always wants to see the
resulting server state, not just the maintenance sub-document in
isolation.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class MaintenanceEnableRequest(BaseModel):
    """The request body for enabling maintenance mode on a server."""

    reason: str | None = None
    expected_end: datetime | None = None
