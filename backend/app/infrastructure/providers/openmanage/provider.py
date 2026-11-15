"""`ServerInventoryProvider` for Dell servers — the OpenManage entry point.

OME discovers, Redfish collects. Two bulk REST calls against the one
OpenManage Enterprise appliance enumerate every server profile and every
managed device, which yields each server's name, deployment template,
service tag and iDRAC address. The hardware behind those addresses is then
read from each server's own BMC over Redfish, by
`app.infrastructure.providers.redfish` unchanged.

The split is not arbitrary: each side supplies exactly what the other
cannot see. Only OME knows a server's name, and the name is what site
parsing and classification key off — an iDRAC has never heard of
`ocp4-nyc-prod-worker-03`. Only the BMC reports hardware as measured
values, which is what removes the capacity and thread heuristics OME's
`InventoryDetails` forced.

The cost is real and inverts the old shape: this is ~25 HTTPS round trips
against every collected server, not four cheap calls against one
appliance. `name_pattern` is therefore applied before a single BMC is
contacted, and the CronJob runs on the Redfish collector's cadence rather
than hourly.

See docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md, and
docs/dell-collectors.md for the OME field/endpoint facts.
"""

from __future__ import annotations

import contextlib
import dataclasses
import ipaddress
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any

import structlog

from app.domain.enums import ManagerType, UnreachableReason, Vendor
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.domain.value_objects.bmc_address import parse_bmc_address
from app.infrastructure.providers.openmanage.client import OmeClient
from app.infrastructure.providers.openmanage.mapping import (
    OmeIdentity,
    dell_port_nics,
    identity_from_profile,
    idrac_bmc_address,
)
from app.infrastructure.providers.redfish.provider import reason_from_error
from app.infrastructure.providers.redfish.targets import RedfishCredential, RedfishTarget

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.OPENMANAGE.value


def _odata_literal(value: str) -> str:
    """
    Escape a string for use inside an OData `eq '...'` filter literal.

    Args:
        value (str): The raw value (a service tag or a profile/device name).

    Returns:
        str: `value` with every single quote doubled, OData's own escape.
    """
    return value.replace("'", "''")


# A dead host or a rejected login is routine; the rest stay PARTIAL (ADR-0037).
_BENIGN_REASONS = frozenset(
    {UnreachableReason.NETWORK_UNREACHABLE, UnreachableReason.AUTH_REJECTED}
)


_HOSTNAME_RE = re.compile(r"[a-z0-9]([a-z0-9._-]*[a-z0-9])?")
_SAMPLE_LIMIT = 10


def _usable_host(raw: str | None) -> str | None:
    """
    Reduce an OME-reported iDRAC address to the key it is joined on, or reject it.

    Args:
        raw (str | None): `DeviceManagement[0].NetworkAddress` as OME gave it.

    Returns:
        str | None: Lowercased, unbracketed IP or hostname; `None` for whitespace or garbage
            that would make `httpx` raise `InvalidURL`.
    """
    text = (raw or "").strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(text)
        return text
    return text if _HOSTNAME_RE.fullmatch(text) else None


def _target_host(key: str) -> str:
    """
    The host a `RedfishTarget` dials for a `_usable_host` key (IPv6 bracketed).

    Args:
        key (str): A key from `_usable_host`.

    Returns:
        str: `key`, in brackets when it is an IPv6 literal.
    """
    return f"[{key}]" if ":" in key else key


def _is_uncollected(message: str, host: str) -> bool:
    """
    Whether a `collection_errors` entry means `host` was not read at all.

    Args:
        message (str): One entry from `redfish.collection_errors`.
        host (str): A candidate host to test the message against.

    Returns:
        bool: `True` when `message` is a failure recorded against that
            host, which left it unmeasured (docs/dell-collectors.md, "Collection flow").
    """
    return message.startswith((f"{host}:", f"[{host}]:"))


def redfish_failures(redfish: object) -> int:
    """
    Read the wrapped Redfish provider's contained-failure count.

    Args:
        redfish (object): The Redfish collector, or a test double without the property.

    Returns:
        int: Its `server_failures`, else 0.
    """
    count = getattr(redfish, "server_failures", 0)
    return count if isinstance(count, int) else 0


class OpenManageProvider(ServerInventoryProvider):
    """
    Collects one OpenManage Enterprise appliance's Dell inventory.

    One instance covers one OME appliance and every Dell server it manages.

    See docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md.
    """

    provider_type = _PROVIDER_TYPE

    def __init__(
        self,
        *,
        manager: Manager,
        credentials: ManagerConnection,
        timeout_seconds: float,
        bmc_credential: RedfishCredential,
        redfish_provider_factory: Callable[[list[RedfishTarget]], Any],
        name_pattern: str = "",
        bmc_port: int = 443,
        bmc_verify_tls: bool = False,
        bmc_verify_tls_reason: str | None = None,
        bmc_ca_bundle: str | None = None,
        verify_tls: bool = False,
    ) -> None:
        """
        Bind a provider to one OME appliance and its fleet's BMCs.

        Args:
            manager (Manager): The manager this run reports under. Its `id`
                becomes each server's `manager_id`; its `endpoint` is the
                OME appliance to connect to.
            credentials (ManagerConnection): OME appliance login
                (`INVENTORY_OME_IP`/`_USERNAME`/`_PASSWORD`).
            timeout_seconds (float): Per-request timeout for OME. BMC
                timeouts are the Redfish collector's own.
            bmc_credential (RedfishCredential): The one iDRAC account used
                for every Dell BMC (`INVENTORY_OME_BMC_USERNAME`/
                `_PASSWORD`).
            redfish_provider_factory (Callable[[list[RedfishTarget]], Any]):
                Builds the Redfish collector for the discovered targets.
                Injected rather than constructed here so this provider
                carries no BMC tuning knobs, and so a test can substitute
                the whole hardware pass.
            name_pattern (str): Regex; profiles whose name does not match
                are dropped before their BMC is contacted. Empty collects
                every profile. Unlike the standalone Redfish collector —
                where the filter is deliberately disabled because a BMC
                does not know the server's name — the filter applies here,
                because OME supplies the name. It is still only an
                efficiency gate; the authoritative filter is
                `tools.run_collector`'s wrapper.
            bmc_port (int): HTTPS port every iDRAC answers on.
            bmc_verify_tls (bool): Whether to verify each BMC's certificate.
            bmc_verify_tls_reason (str | None): Why verification is off,
                recorded on every target when it is.
            bmc_ca_bundle (str | None): PEM bundle trusted in addition to
                the system store.
            verify_tls (bool): Whether to verify the OME appliance's own
                certificate; defaults to `False` for self-signed appliances.

        Raises:
            ValueError: If `manager` has no endpoint configured.
        """
        if not manager.endpoint:
            raise ValueError(f"Manager {manager.id!r} has no endpoint configured.")
        super().__init__()
        self._endpoint: str = manager.endpoint
        self._manager = manager
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds
        self._bmc_credential = bmc_credential
        self._redfish_provider_factory = redfish_provider_factory
        self._pattern = re.compile(name_pattern) if name_pattern else None
        self._bmc_port = bmc_port
        self._bmc_verify_tls = bmc_verify_tls
        self._bmc_verify_tls_reason = bmc_verify_tls_reason
        self._bmc_ca_bundle = bmc_ca_bundle
        self._verify_tls = verify_tls
        self._unusable: list[OmeIdentity] = []

    def _new_client(self) -> OmeClient:
        """
        Build a client for this appliance — sessions are never shared across calls.

        Returns:
            OmeClient: A fresh, not-yet-logged-in client.
        """
        return OmeClient(
            endpoint=self._endpoint,
            username=self._credentials.username,
            password=self._credentials.password,
            timeout_seconds=self._timeout_seconds,
            verify_tls=self._verify_tls,
        )

    async def health_check(self) -> None:
        """
        Verify the OME appliance is reachable and the credentials are accepted.

        Checks OME only: a BMC that rejects the iDRAC account is one server's failure.

        Raises:
            OmeConnectionError: If login fails for any reason.
        """
        async with self._new_client():
            return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """
        Locate one device via OME, then measure only its own iDRAC (ADR-0032).

        Args:
            identity (ServerIdentity): `serial` (the Dell service tag) is
                tried first, falling back to `name` (the profile name).

        Returns:
            ProviderServer | None: The current state, `reachable=False`
                when OME knows the profile but its iDRAC gave nothing, or
                `None` when neither `serial` nor `name` resolves a profile.
        """
        async with self._new_client() as client:
            identity_ome = await self._discover_one(client, identity)
        if identity_ome is None:
            return None
        if not identity_ome.idrac_ip:
            return self._not_an_idrac_server(identity_ome) if identity_ome.refused_target else None

        key = _usable_host(identity_ome.idrac_ip)
        if key is None:
            return self._unusable_address_server(identity_ome)
        identity_ome = self._with_host(identity_ome, key)
        target = self._target_for(identity_ome)
        redfish = self._redfish_provider_factory([target])
        server = await redfish.get_one(ServerIdentity(host=target.host, serial=identity_ome.serial))
        if server is None:
            logger.warning(
                "ome.get_one_failed",
                host=target.host,
                name=identity_ome.name,
                serial=identity_ome.serial,
                hint="The cause is in the redfish.get_one_failed line for this host.",
            )
            return self._unreachable_server(
                identity_ome,
                detail=f"live recheck of the iDRAC at {target.host} returned no server",
            )
        if not server.reachable:
            return self._unreachable_server(
                identity_ome, server.unreachable_reason, server.unreachable_detail
            )
        return self._merged(server, {key: identity_ome})

    async def _discover_one(
        self, client: OmeClient, identity: ServerIdentity
    ) -> OmeIdentity | None:
        """
        Resolve one profile+device pair by service tag, falling back to profile name.

        Args:
            client (OmeClient): The logged-in OME client.
            identity (ServerIdentity): `serial`/`name` to search by.

        Returns:
            OmeIdentity | None: The joined identity, or `None` when
                neither field resolves a profile.
        """
        device: dict[str, Any] = {}
        if identity.serial:
            tag = _odata_literal(identity.serial)
            devices = await client.get_all(
                f"/DeviceService/Devices?$filter=DeviceServiceTag eq '{tag}'"
            )
            device = devices[0] if devices else {}

        profile: dict[str, Any] | None = None
        display_name = str(device.get("DeviceName") or "")
        if display_name:
            profiles = await client.get_all(
                f"/ProfileService/Profiles?$filter=TargetName eq '{_odata_literal(display_name)}'"
            )
            profile = profiles[0] if profiles else None

        if profile is None and identity.name:
            profiles = await client.get_all(
                f"/ProfileService/Profiles?$filter=ProfileName eq '{_odata_literal(identity.name)}'"
            )
            profile = profiles[0] if profiles else None
            if profile is not None and not device:
                display_name = str(profile.get("TargetName") or "")
                if display_name:
                    devices = await client.get_all(
                        "/DeviceService/Devices?$filter="
                        f"DeviceName eq '{_odata_literal(display_name)}'"
                    )
                    device = devices[0] if devices else {}

        if profile is None:
            return None
        return identity_from_profile(profile=profile, device=device)

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield every matched Dell server, named by OME and measured by its BMC.

        `collect()` (the base class) resets `collection_errors` before
        calling this.

        Yields:
            ProviderServer: One Dell server: hardware as its iDRAC reports
                it, identity as OME does. A profile OME knows whose iDRAC
                did not answer or rejected the login still yields,
                `reachable=False` and every hardware field `None` — see
                `_unreachable_server`.

        Raises:
            OmeConnectionError: On login failure against the appliance
                itself, or a failure of either bulk enumeration call. One
                iDRAC that is dead or rejects its credential is recorded
                as a `reachable=False` server instead and never aborts
                the run.
        """
        identities, refused = await self._discover()
        for identity in refused:
            yield self._not_an_idrac_server(identity)
        for identity in self._unusable:
            yield self._unusable_address_server(identity)
        if not identities:
            logger.info("ome.no_matching_profiles", endpoint=self._endpoint)
            return

        targets = [self._target_for(identity) for identity in identities.values()]
        logger.info(
            "ome.collecting_over_redfish",
            endpoint=self._endpoint,
            targets=len(targets),
        )

        redfish = self._redfish_provider_factory(targets)
        reached: set[str] = set()
        uncollected: dict[str, UnreachableReason | None] = {}
        failed: dict[str, tuple[UnreachableReason | None, str | None]] = {}
        try:
            async with contextlib.aclosing(redfish.collect()) as servers:
                async for server in servers:
                    parsed = parse_bmc_address(server.bmc_address_raw)
                    host = parsed.host if parsed is not None else None
                    if not server.reachable:
                        if host is not None:
                            failed.setdefault(
                                host, (server.unreachable_reason, server.unreachable_detail)
                            )
                        continue
                    if host is not None:
                        reached.add(host)
                    try:
                        merged = self._merged(server, identities)
                    except Exception as exc:
                        stub = self._contain_server_failure(
                            exc,
                            event="ome.server_failed",
                            stage="merging its OME identity",
                            vendor=Vendor.DELL.value,
                            external_id=server.external_id,
                            name=server.name,
                            serial=server.serial,
                            model=server.model,
                            manager_id=self._manager.id,
                            bmc_address_raw=server.bmc_address_raw,
                            host=host,
                        )
                        if stub is not None:
                            yield stub
                        continue
                    yield merged
        finally:
            self._server_failures += redfish_failures(redfish)
            # Runs on early close too — docs/dell-collectors.md, "Collection flow".
            for message in redfish.collection_errors:
                host = next(
                    (h for h in identities if h not in reached and _is_uncollected(message, h)),
                    None,
                )
                if host is not None:
                    reason = reason_from_error(message)
                    uncollected[host] = reason
                    if reason not in _BENIGN_REASONS:
                        self._record_error(message)
                else:
                    self._record_error(message)

        for host, (reason, _detail) in failed.items():
            if host in identities and host not in reached:
                uncollected.setdefault(host, reason)
        for host, reason in uncollected.items():
            known_reason, detail = failed.get(host, (None, None))
            yield self._unreachable_server(identities[host], reason or known_reason, detail)

    async def _discover(self) -> tuple[dict[str, OmeIdentity], list[OmeIdentity]]:
        """
        Enumerate the appliance in two bulk calls and keep the matched profiles.

        See docs/dell-collectors.md, "Collection flow".

        Returns:
            tuple[dict[str, OmeIdentity], list[OmeIdentity]]: Matched identities keyed by BMC
                address (`OmeIdentity.idrac_ip`; keyed by address because that is what the
                Redfish pass reports back and what joins the two halves), and the matched
                profiles whose target is not an address and was refused (never dialled).
        """
        async with self._new_client() as client:
            profiles = await client.get_all("/ProfileService/Profiles")
            devices = await client.get_all("/DeviceService/Devices")

        # Joined on display name, not address — docs/dell-collectors.md, 2026-09-10.
        device_by_ip: dict[str, dict[str, Any]] = {}
        for device in devices:
            if device.get("DeviceName") is None:
                continue
            device_name = str(device.get("DeviceName"))
            kept = device_by_ip.setdefault(device_name, device)
            if kept is not device:
                self._log_duplicate("device name", device_name, kept, device)
        self._unusable = []
        logger.info(
            "ome.enumerated",
            endpoint=self._endpoint,
            profiles=len(profiles),
            devices=len(devices),
        )

        identities: dict[str, OmeIdentity] = {}
        refused: list[OmeIdentity] = []
        unassigned: list[str] = []
        for profile in profiles:
            if not self._matches(profile):
                continue
            display_name = str(profile.get("TargetName") or "")
            identity = identity_from_profile(
                profile=profile, device=device_by_ip.get(display_name, {})
            )
            host = identity.idrac_ip
            if not host:
                if identity.refused_target:
                    refused.append(identity)
                else:
                    unassigned.append(identity.name or str(profile.get("Id") or ""))
                continue
            key = _usable_host(host)
            if key is None:
                self._unusable.append(identity)
                continue
            identity = self._with_host(identity, key)
            kept_identity = identities.setdefault(key, identity)
            if kept_identity is not identity:
                self._log_duplicate("iDRAC address", key, kept_identity, identity)

        if unassigned:
            logger.info(
                "ome.profile_without_address",
                endpoint=self._endpoint,
                profiles=len(unassigned),
                sample=unassigned[:_SAMPLE_LIMIT],
                hint=(
                    "Profiles with no target device are undeployed templates "
                    "with no server behind them and were not collected."
                ),
            )
        return identities, refused

    @staticmethod
    def _with_host(identity: OmeIdentity, key: str) -> OmeIdentity:
        """
        Store the normalized address on an identity, with a dialable BMC URI.

        Args:
            identity (OmeIdentity): The discovered identity.
            key (str): Its `_usable_host` key.

        Returns:
            OmeIdentity: The identity with `idrac_ip` and `bmc_address_raw` normalized.
        """
        return dataclasses.replace(
            identity, idrac_ip=key, bmc_address_raw=idrac_bmc_address(_target_host(key))
        )

    def _log_duplicate(self, kind: str, key: str, kept: object, ignored: object) -> None:
        """
        Warn that two OME records share a join key and the first one won.

        Args:
            kind (str): `device name` or `iDRAC address`.
            key (str): The shared key.
            kept (object): The OME identity or device that is used.
            ignored (object): The one dropped.
        """

        def label(item: object) -> dict[str, object]:
            """
            Describe one record for the log.

            Args:
                item (object): An `OmeIdentity` or a raw OME device.

            Returns:
                dict[str, object]: Its name and id.
            """
            if isinstance(item, OmeIdentity):
                return {"name": item.name, "id": item.profile_id}
            raw = item if isinstance(item, dict) else {}
            return {"name": raw.get("DeviceName"), "id": raw.get("Id")}

        logger.warning(
            "ome.duplicate_identity",
            endpoint=self._endpoint,
            kind=kind,
            key=key,
            kept=label(kept),
            ignored=label(ignored),
            hint="First record wins; the ignored one is not collected under this key.",
        )

    def _matches(self, profile: dict[str, Any]) -> bool:
        """
        Whether a profile's name passes the pre-filter.

        Args:
            profile (dict[str, Any]): One `/ProfileService/Profiles` entry.

        Returns:
            bool: `True` when there is no pattern or the profile's
                `ProfileName` matches it.
        """
        if self._pattern is None:
            return True
        return bool(self._pattern.search(str(profile.get("ProfileName") or "")))

    def _target_for(self, identity: OmeIdentity) -> RedfishTarget:
        """
        Build the Redfish target for one discovered server.

        `name` carries OME's profile name through to `override_name`
        (docs/dell-collectors.md, "Collection flow").

        Args:
            identity (OmeIdentity): One matched, addressable profile.

        Returns:
            RedfishTarget: The BMC to collect, with its login and TLS policy.
        """
        return RedfishTarget(
            host=_target_host(_usable_host(identity.idrac_ip) or str(identity.idrac_ip)),
            port=self._bmc_port,
            credential=self._bmc_credential,
            verify_tls=self._bmc_verify_tls,
            verify_tls_reason=self._bmc_verify_tls_reason,
            ca_bundle=self._bmc_ca_bundle,
            name=identity.name,
        )

    def _unusable_address_server(self, identity: OmeIdentity) -> ProviderServer:
        """
        Build the stub for a profile whose OME-reported iDRAC address cannot be dialled.

        Args:
            identity (OmeIdentity): The identity, `idrac_ip` as OME wrote it.

        Returns:
            ProviderServer: A `COLLECTION_ERROR` stub; the run is recorded PARTIAL.
        """
        detail = f"OME reports an unusable iDRAC address {identity.idrac_ip!r}"
        self._server_failures += 1
        self._record_error(f"{identity.name or identity.serial}: {detail}")
        return self._unreachable_server(identity, UnreachableReason.COLLECTION_ERROR, detail)

    def _unreachable_server(
        self,
        identity: OmeIdentity,
        reason: UnreachableReason | None = None,
        detail: str | None = None,
    ) -> ProviderServer:
        """
        Build the placeholder for a profile OME knows but whose iDRAC gave nothing.

        See docs/dell-collectors.md's "Collection flow" update.

        Args:
            identity (OmeIdentity): The uncollected profile's OME identity.
            reason (UnreachableReason | None): Why the iDRAC gave nothing,
                or `None` when the caller cannot tell (`get_one`).
            detail (str | None): The cause in words, from the Redfish pass.

        Returns:
            ProviderServer: `reachable=False`, every hardware/network field
                `None`. `IngestService` carries the rest forward.
        """
        logger.error(
            "ome.server_unreachable",
            host=identity.idrac_ip,
            name=identity.name,
            reason=reason.value if reason else None,
            error=detail,
        )
        return ProviderServer(
            external_id=f"ome-unreachable:{identity.idrac_ip}",
            vendor=Vendor.DELL.value,
            name=identity.name,
            model=identity.model,
            serial=identity.serial,
            bmc_address_raw=identity.bmc_address_raw,
            manager_id=self._manager.id,
            profile_template_name=identity.profile_template_name,
            profile_template_external_id=identity.profile_template_external_id,
            reachable=False,
            unreachable_reason=reason,
            unreachable_detail=detail,
        )

    def _not_an_idrac_server(self, identity: OmeIdentity) -> ProviderServer:
        """
        Build the CRITICAL placeholder for a profile whose OME target is not an address.

        OME has no iDRAC address for the device and its display name is something like an OS
        hostname, so nothing is dialled (docs/dell-collectors.md, 2026-11-05).

        Args:
            identity (OmeIdentity): The profile's identity, with `refused_target` set.

        Returns:
            ProviderServer: `reachable=False`, `not_an_idrac`, no BMC address, every hardware
                field `None`. Logged, not counted as a failed run: it repeats until OME is fixed.
        """
        logger.error("ome.target_not_an_idrac", target=identity.refused_target, name=identity.name)
        return ProviderServer(
            external_id=(
                "ome-not-an-idrac:"
                + (
                    identity.name
                    or (identity.profile_id and f"profile-{identity.profile_id}")
                    or (identity.device_id and f"device-{identity.device_id}")
                    or identity.serial
                    or "unnamed"
                )
            ),
            vendor=Vendor.DELL.value,
            name=identity.name,
            model=identity.model,
            serial=identity.serial,
            bmc_address_raw=None,
            manager_id=self._manager.id,
            profile_template_name=identity.profile_template_name,
            profile_template_external_id=identity.profile_template_external_id,
            reachable=False,
            unreachable_reason=UnreachableReason.NOT_AN_IDRAC,
        )

    def _merged(self, server: ProviderServer, by_host: dict[str, OmeIdentity]) -> ProviderServer:
        """
        Put OME's identity back onto one Redfish-collected server, joined on host.

        Measured values win; OME's address and NIC reduction apply regardless
        (docs/dell-collectors.md, "BMC address" and "NICs").

        Args:
            server (ProviderServer): One server as Redfish collected it.
            by_host (dict[str, OmeIdentity]): Identities by iDRAC IP.

        Returns:
            ProviderServer: The same server, carrying its OME identity.
                Returned untouched if its address matches nothing OME
                reported, which should not happen — every target came from
                this dict — but is a silent no-op rather than a crash.
        """
        parsed = parse_bmc_address(server.bmc_address_raw)
        host = parsed.host if parsed else None
        identity = by_host.get(host) if host else None
        if identity is None:
            return server
        return dataclasses.replace(
            server,
            manager_id=self._manager.id,
            # `nic_macs` stays whole on purpose — docs/dell-collectors.md, "NICs".
            nics=dell_port_nics(server.nics),
            profile_template_name=identity.profile_template_name,
            profile_template_external_id=identity.profile_template_external_id,
            bmc_address_raw=identity.bmc_address_raw or server.bmc_address_raw,
            model=server.model or identity.model,
            serial=server.serial or identity.serial,
        )
