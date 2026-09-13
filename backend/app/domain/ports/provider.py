"""The collector seam every vendor provider implements.

`ServerInventoryProvider` and `ProviderServer` are the interface all seven
providers (`fake`, `ucs_manager`, `ucs_central`, `intersight`,
`openmanage`, `oneview`, `redfish`) implement/produce.
`app.application.services.ingest` is the one caller: it drives
`collect()`, then normalizes each `ProviderServer` into a domain `Server`
(correlate -> classify -> health-evaluate -> upsert).

`ProviderServer` is intentionally flatter and less structured than the
domain `Server` model: it's the raw-ish shape a collector naturally
produces (already vendor-normalized, but not yet correlated against
existing records or run through the search/cursor/health/classification
machinery). The field-level contract — what `None` means, why there is
no `site_id`, what each tuple's keys mirror — is in docs/architecture.md,
"The provider contract".

`ServerInventoryProvider` is an `ABC`, not a `Protocol` — see ADR-0023 for
why, and for the mechanism behind `_list_servers`' exact signature.

`get_one()` is the sixth method, added by ADR-0032 for a live single-server
recheck: it takes a `ServerIdentity` and fetches that one server directly,
never by re-running `_list_servers()` and filtering.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProviderAttachment:
    """Provider-neutral DTO for one fabric/uplink connection a server reports."""

    type: str
    provider: str | None
    fabric: str | None
    fabric_name: str | None
    fabric_id: str | None
    fabric_model: str | None
    fabric_serial: str | None
    server_interface: str | None
    server_port: str | None
    fabric_port: str | None
    admin_state: str
    oper_state: str
    speed_mbps: int | None
    interface_kind: str = "PHYSICAL"  # or "VNIC"; only PHYSICAL counts as a fabric path


@dataclass(frozen=True, slots=True)
class ProviderNic:
    """
    Provider-neutral DTO for one host network interface, as an OS would see it.

    `link_state` is a plain string in `LinkState`'s closed set; ingest maps it.
    """

    name: str
    mac: str | None
    speed_mbps: int | None
    link_state: str
    location: str | None = None  # the BMC's own placement id (an iDRAC FQDD), or None


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """
    Whatever `get_one` needs to re-locate one already-ingested server.

    Built from a stored `Server` document; each provider reads only the
    field it correlates on (docs/adr/0032-available-server-lookup-api.md).
    """

    serial: str | None = None
    external_id: str | None = None
    host: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderServer:
    """
    Provider-neutral DTO for one server as reported by a collector (real or fake).

    Identity/MAC/BMC values arrive already normalized; nothing downstream
    re-parses a vendor format. `None` means "not read this run", never zero.
    """

    external_id: str
    vendor: str
    name: str
    model: str | None = None

    serial: str | None = None
    system_uuid: str | None = None

    reachable: bool = True  # False: identity known, BMC unreachable; every field below is None

    nic_macs: tuple[str, ...] | None = None

    bmc_address_raw: str | None = None
    bmc_mac: str | None = None

    nics: tuple[ProviderNic, ...] = ()  # populates NetworkInfo.interfaces; empty = MACs only

    manager_id: str | None = None  # no site_id: the site is parsed from the name at ingest

    profile_dn: str | None = None  # the profile instance's own DN; not persisted
    profile_template_name: str | None = None
    profile_template_external_id: str | None = None

    cpu_sockets: int | None = None
    cpu_cores: int | None = None
    cpu_threads: int | None = None
    cpu_model: str | None = None

    memory_total_bytes: int | None = None

    storage_total_bytes: int | None = None
    storage_drives: tuple[dict[str, object], ...] | None = None

    # Keys mirror `hardware.Gpu` / `Psu` / `MemoryModule`; units already normalized.
    gpus: tuple[dict[str, object], ...] | None = None
    psus: tuple[dict[str, object], ...] | None = None
    memory_modules: tuple[dict[str, object], ...] | None = None

    attachments: tuple[ProviderAttachment, ...] = ()

    tags: tuple[str, ...] = field(default_factory=tuple)


class ServerInventoryProvider(ABC):
    """
    The lifecycle every vendor collector implements.

    `collect()` drives a run; a subclass fills in `_list_servers()` and `health_check()`
    and calls `_record_error()` per lost fleet slice (ADR-0023). Call `super().__init__()`.
    """

    provider_type: str

    def __init__(self) -> None:
        """Start this run's failure list empty."""
        self._collection_errors: list[str] = []

    @property
    def collection_errors(self) -> tuple[str, ...]:
        """
        Partial failures this run recorded; non-empty makes `tools.run_collector` exit 3.

        Returns:
            tuple[str, ...]: One message per failure, empty for a run
                that collected everything it planned to.
        """
        return tuple(self._collection_errors)

    def _record_error(self, message: str) -> None:
        """
        Record one failure that cost this run part of the fleet.

        Args:
            message (str): A human-readable description, naming the
                endpoint/domain/host and, where relevant, the numbers
                (e.g. how many were expected versus fetched) — this is
                what an operator reads to tell a lost connection from a
                paging ceiling.
        """
        self._collection_errors.append(message)

    async def collect(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Run this collector once, yielding every server it can reach.

        Resets `collection_errors`, then wraps `_list_servers()` in
        `aclosing` so an abandoned run still closes what it opened.

        Yields:
            ProviderServer: Each server `_list_servers()` produces.
        """
        self._collection_errors = []
        async with aclosing(self._list_servers()) as stream:
            async for server in stream:
                yield server

    @abstractmethod
    async def health_check(self) -> None:
        """
        Verify this collector is reachable and its credentials work.

        Raises:
            Exception: A vendor-specific connection or authentication
                error, on any failure.
        """
        ...

    @abstractmethod
    def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield every server this collector can reach, one pass.

        A plain `def` returning `AsyncGenerator`, never `async def` — ADR-0023
        explains both halves.

        Yields:
            ProviderServer: One server, already vendor-normalized.
        """
        ...

    @abstractmethod
    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """
        Fetch one server's current state by identity, never by re-running `_list_servers()`.

        For a live single-server recheck; see ADR-0032.

        Args:
            identity (ServerIdentity): Whichever field this provider
                correlates on is set; the rest may be `None`.

        Returns:
            ProviderServer | None: The current state, or `None` if the
                server can no longer be found.
        """
        ...
