"""The collector seam every vendor provider implements.

`ServerInventoryProvider` and `ProviderServer` are the interface all eight
providers (`fake`, `ucs_manager`, `ucs_central`, `intersight`,
`openmanage`, `oneview`, `redfish`, `kubevirt`) implement/produce.
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

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field

import structlog

from app.domain.enums import ManagerType, UnreachableReason

logger = structlog.get_logger(__name__)

_FAILURE_TEXT_LIMIT = 400

SERVER_FAILED_MARKER = " could not be collected at "

_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_SECRET_PARAM = re.compile(r"(?i)([?&](?:password|passwd|pwd|token|secret|api[_-]?key)=)[^&\s]*")


def redact_secrets(text: str) -> str:
    """
    Mask credentials a vendor or HTTP error may have echoed into its message.

    Args:
        text (str): An exception message or a stored failure detail.

    Returns:
        str: The text with URL user-info and secret-looking query values replaced by `***`.
    """
    return _SECRET_PARAM.sub(r"\1***", _URL_USERINFO.sub(r"\1***@", text))


class NotThisManagersServerError(Exception):
    """
    `get_one` was handed a server another manager of the same type collects.

    Not "gone": the caller keeps trusting its stored copy (a KubeVirt VM on another cluster).
    """


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
    # False: `link_state` is a placeholder; ingest keeps the stored one (ONEVIEW, ADR-0022).
    link_state_read: bool = True


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
    unreachable_reason: UnreachableReason | None = None
    unreachable_detail: str | None = None  # operator-readable cause; set with `reachable=False`

    nic_macs: tuple[str, ...] | None = None

    bmc_address_raw: str | None = None
    bmc_mac: str | None = None

    nics: tuple[ProviderNic, ...] = ()  # populates NetworkInfo.interfaces; empty = MACs only

    manager_id: str | None = None  # no site_id: the site is parsed from the name at ingest

    profile_dn: str | None = None  # the profile instance's own DN; not persisted
    # None = could not read (stored value kept); "" = read, and there is none (cleared).
    profile_template_name: str | None = None
    profile_template_external_id: str | None = None
    # OPENMANAGE only: the profile's last deployment job result; None = not read (ADR-0066).
    profile_last_run_status: int | None = None
    profile_last_run_status_name: str | None = None

    cpu_sockets: int | None = None
    cpu_cores: int | None = None
    cpu_threads: int | None = None
    cpu_model: str | None = None

    memory_total_bytes: int | None = None

    storage_total_bytes: int | None = None
    storage_drives: tuple[dict[str, object], ...] | None = None
    # Keys mirror `hardware.StorageController`; `volumes` is a tuple of `StorageVolume` dicts.
    storage_controllers: tuple[dict[str, object], ...] | None = None
    # Keys mirror `hardware.RaidCheck`; None = the RAID sources could not be read this run.
    raid_check: dict[str, object] | None = None

    # Keys mirror `hardware.Gpu` / `Psu` / `MemoryModule`; units already normalized.
    gpus: tuple[dict[str, object], ...] | None = None
    psus: tuple[dict[str, object], ...] | None = None
    memory_modules: tuple[dict[str, object], ...] | None = None

    attachments: tuple[ProviderAttachment, ...] = ()

    tags: tuple[str, ...] = field(default_factory=tuple)

    # A VM's host (ADR-0059); both None for a physical server. Node: None unread, "" not running.
    vm_host_cluster: str | None = None
    vm_host_node: str | None = None


SYSTEMIC_FAILURE_MIN = 5
SYSTEMIC_FAILURE_RATIO = 0.2


def is_systemic_failure(failed: int, seen: int) -> bool:
    """
    Whether so many servers failed that the collector, not the servers, is at fault.

    Args:
        failed (int): Servers a run could not map (each became a stub or was dropped).
        seen (int): Servers the run yielded, stubs included.

    Returns:
        bool: True when at least `SYSTEMIC_FAILURE_MIN` failed and they exceed
            `SYSTEMIC_FAILURE_RATIO` of the fleet (ADR-0062).
    """
    return failed >= SYSTEMIC_FAILURE_MIN and failed > seen * SYSTEMIC_FAILURE_RATIO


def failed_server(
    *,
    external_id: str,
    vendor: str,
    name: str,
    detail: str,
    serial: str | None = None,
    model: str | None = None,
    manager_id: str | None = None,
    bmc_address_raw: str | None = None,
    system_uuid: str | None = None,
) -> ProviderServer:
    """
    Build the `reachable=False` stub for a server whose record could not be mapped.

    Args:
        external_id (str): The manager's id for the server.
        vendor (str): A `Vendor` value.
        name (str): The server's name as far as it could be read.
        detail (str): Why, in words, shown to the operator.
        serial (str | None): The hardware serial; ingest correlates on it.
        model (str | None): The model, when readable.
        manager_id (str | None): The owning manager.
        bmc_address_raw (str | None): The BMC address, when readable.
        system_uuid (str | None): The system UUID, when readable.

    Returns:
        ProviderServer: Identity only; every other field is `None`, so ingest
            carries the stored values forward.
    """
    return ProviderServer(
        external_id=external_id,
        vendor=vendor,
        name=name,
        model=model,
        serial=serial,
        system_uuid=system_uuid,
        reachable=False,
        unreachable_reason=UnreachableReason.COLLECTION_ERROR,
        unreachable_detail=detail,
        bmc_address_raw=bmc_address_raw,
        manager_id=manager_id,
    )


class ServerInventoryProvider(ABC):
    """
    The lifecycle every vendor collector implements.

    `collect()` drives a run; a subclass fills in `_list_servers()` and `health_check()`
    and calls `_record_error()` per lost fleet slice (ADR-0023). Call `super().__init__()`.
    """

    provider_type: str

    def __init__(self) -> None:
        """Start this run's failure list and server counters empty."""
        self._collection_errors: list[str] = []
        self._servers_seen = 0
        self._server_failures = 0

    @property
    def servers_seen(self) -> int:
        """
        Servers this run yielded, failed-mapping stubs included.

        Returns:
            int: The count since `collect()` last started.
        """
        return self._servers_seen

    @property
    def server_failures(self) -> int:
        """
        Servers this run could not map, whether they became a stub or were dropped.

        Returns:
            int: The count since `collect()` last started.
        """
        return self._server_failures

    def _contain_server_failure(
        self,
        exc: Exception,
        *,
        event: str,
        stage: str,
        vendor: str,
        external_id: str | None,
        name: str | None,
        serial: str | None = None,
        model: str | None = None,
        manager_id: str | None = None,
        bmc_address_raw: str | None = None,
        system_uuid: str | None = None,
        **context: object,
    ) -> ProviderServer | None:
        """
        Log, record and count one server's failure, and build its stub.

        Call it from an `except Exception` around one server's mapping and `yield`
        the result unless it is `None` (no safe identity; ADR-0062).

        Args:
            exc (Exception): What the mapping raised.
            event (str): The log event, `<vendor>.server_failed`.
            stage (str): What was being done, e.g. `mapping hardware`.
            vendor (str): A `Vendor` value for the stub.
            external_id (str | None): The manager's id for the server, read from the raw record.
            name (str | None): The server's name, when readable.
            serial (str | None): The serial; without it only `REDFISH_STANDALONE` gets a stub.
            model (str | None): The model, when readable.
            manager_id (str | None): The owning manager.
            bmc_address_raw (str | None): The BMC address, when readable.
            system_uuid (str | None): The system UUID, when readable.
            **context (object): Extra log fields (endpoint, dn, uri, host).

        Returns:
            ProviderServer | None: A `reachable=False` stub carrying the cause, or `None`
                when no safe identity exists.
        """
        error = redact_secrets(f"{type(exc).__name__}: {exc}")[:_FAILURE_TEXT_LIMIT]
        label = name or external_id or "<unidentified server>"
        logger.error(
            event,
            exc_info=exc,
            stage=stage,
            vendor=vendor,
            external_id=external_id,
            name=name,
            serial=serial,
            manager_id=manager_id,
            error=error,
            **context,
        )
        self._server_failures += 1
        self._record_error(
            f"{label} (id {external_id}, serial {serial}){SERVER_FAILED_MARKER}{stage}: {error}"
        )
        if not external_id or (
            not serial and self.provider_type != ManagerType.REDFISH_STANDALONE.value
        ):
            self._servers_seen += 1  # nothing is yielded, but the unit was attempted
            return None
        return failed_server(
            external_id=external_id,
            vendor=vendor,
            name=label,
            detail=f"{stage}: {error}",
            serial=serial,
            model=model,
            manager_id=manager_id,
            bmc_address_raw=bmc_address_raw,
            system_uuid=system_uuid,
        )

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
        self._servers_seen = 0
        self._server_failures = 0
        async with aclosing(self._list_servers()) as stream:
            async for server in stream:
                self._servers_seen += 1
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

        Raises:
            NotThisManagersServerError: The identity belongs to another manager of
                this type, so this one cannot say whether it still exists.
        """
        ...
