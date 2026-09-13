"""The `ServerRepository` port.

`domain/` declares this Protocol; `infrastructure/mongodb/` implements it.
Nothing in `application/` or `api/` talks to PyMongo directly — every
server read/write goes through this interface, which is what makes the
Mongo-specific cursor/query mechanics (`app.domain.services.search`,
`app.domain.services.cursor`) swappable in tests without a real database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.models.server import Server


@dataclass(frozen=True, slots=True)
class SiteBreakdownRow:
    """
    One `$group` bucket from `ServerRepository.site_breakdown`.

    Raw stored strings, not enums, so a value a previous schema wrote still counts.
    """

    site_id: str | None
    vendor: str | None
    health: str | None
    maintenance: bool
    installation_type: str | None
    openshift_state: str | None
    count: int


@dataclass(frozen=True, slots=True)
class Page:
    """
    One page of a keyset-paginated `Server` listing.

    `next_cursor` is opaque and HMAC-signed; callers pass it back verbatim.
    """

    items: list[Server]
    next_cursor: str | None
    has_more: bool
    total_count: int | None  # only populated when the caller asked for it


@dataclass(frozen=True, slots=True)
class ProviderSnapshotRow:
    """One collector's slice of `ServerRepository.fleet_snapshot`.

    Attributes:
        source_provider (str | None): The raw stored `source_provider`.
        total (int): Servers this collector owns.
        stale (int): Of those, not seen since the caller's cutoff — a
            server never seen at all counts here too.
        unreachable (int): Of those, currently `reachable=False`.
        partial (int): Servers whose most recent collection left
            `unread_fields` non-empty — read, but not all of it.
        last_seen_at (str | None): The newest stored `last_seen_at`, as
            the raw ISO string, or `None` if no server was ever seen.
    """

    source_provider: str | None
    total: int
    stale: int
    unreachable: int
    partial: int
    last_seen_at: str | None


@dataclass(frozen=True, slots=True)
class ClusterSnapshotRow:
    """One cluster's slice of `ServerRepository.fleet_snapshot`.

    Attributes:
        cluster_name (str): The stored `openshift.cluster_name`.
        held (int): Servers the cluster currently holds.
        last_reported_at (str | None): The newest `openshift.
            last_reported_at` across them, raw ISO string.
    """

    cluster_name: str
    held: int
    last_reported_at: str | None


@dataclass(frozen=True, slots=True)
class FleetSnapshot:
    """What `/metrics` reports about the fleet — see ADR-0029.

    Attributes:
        by_provider (list[ProviderSnapshotRow]): One row per collector.
        by_cluster (list[ClusterSnapshotRow]): One row per cluster that
            holds at least one server.
        by_health (dict[str, int]): Servers per stored `health.overall`.
        by_policy (dict[str, int]): Servers each health `policy_key` is
            firing on.
        in_maintenance (int): Servers with maintenance enabled.
    """

    by_provider: list[ProviderSnapshotRow]
    by_cluster: list[ClusterSnapshotRow]
    by_health: dict[str, int]
    by_policy: dict[str, int]
    in_maintenance: int


class ServerRepository(Protocol):
    """The persistence operations the application layer depends on, implemented by MongoDB."""

    async def upsert(self, server: Server) -> Server:
        """
        Insert or update a server document by `_id`.

        Overwrites; the caller keeps user-owned fields (tags/notes) intact — see
        `app.application.services.ingest`.

        Args:
            server (Server): The document to write.

        Returns:
            Server: The written document.
        """
        ...

    async def upsert_with_revision_check(self, server: Server, *, expected_revision: int) -> Server:
        """
        Replace an existing server document, only if its stored revision still matches.

        Optimistic concurrency for read-modify-write cycles. Never inserts:
        a missing document is a conflict, not a create.

        Args:
            server (Server): The document to write, with its new field values.
            expected_revision (int): The `revision` the caller last read.

        Returns:
            Server: The written document.

        Raises:
            RevisionConflictError: The document's stored revision has
                already moved past `expected_revision`, or the document no
                longer exists.
        """
        ...

    async def get_by_id(self, server_id: str) -> Server | None:
        """
        Look up a server by its `_id`.

        Args:
            server_id (str): The server's `_id`.

        Returns:
            Server | None: The matching document, or `None` if it doesn't exist.
        """
        ...

    async def list_page(
        self,
        *,
        filters: dict[str, object],
        search: str | None,
        sort: str,
        sort_desc: bool,
        cursor: str | None,
        page_size: int,
        with_count: bool,
    ) -> Page:
        """
        Fetch one keyset-paginated page of servers.

        `filters` is already whitelisted by `app.domain.services.search`, never raw input.

        Args:
            filters (dict[str, object]): Whitelisted Mongo field/value filters.
            search (str | None): A free-text search term, or `None`.
            sort (str): The field to sort by.
            sort_desc (bool): Whether to sort descending.
            cursor (str | None): An opaque cursor from a previous page, or
                `None` for the first page.
            page_size (int): The maximum number of items to return.
            with_count (bool): Whether to populate `Page.total_count`, which
                costs an extra query.

        Returns:
            Page: The matching page.
        """
        ...

    async def count(self, filters: dict[str, object]) -> int:
        """
        Count servers matching whitelisted filters.

        Args:
            filters (dict[str, object]): Whitelisted Mongo field/value filters.

        Returns:
            int: The number of matching servers.
        """
        ...

    async def site_breakdown(self) -> list[SiteBreakdownRow]:
        """
        Count servers grouped by site, vendor, health, maintenance and installation type.

        Returns:
            list[SiteBreakdownRow]: One row per distinct combination found.
        """
        ...

    async def fleet_snapshot(self, *, stale_before: datetime) -> FleetSnapshot:
        """
        Summarise the fleet for the Prometheus gauges (ADR-0029).

        Args:
            stale_before (datetime): A server whose `last_seen_at` is older
                than this — or absent — counts as stale.

        Returns:
            FleetSnapshot: Per-collector, per-cluster and per-health counts.
        """
        ...
