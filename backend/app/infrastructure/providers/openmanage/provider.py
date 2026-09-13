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
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any

import structlog

from app.domain.enums import ManagerType, Vendor
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer, ServerInventoryProvider
from app.domain.value_objects.bmc_address import parse_bmc_address
from app.infrastructure.providers.openmanage.client import OmeClient
from app.infrastructure.providers.openmanage.mapping import (
    OmeIdentity,
    dell_port_nics,
    identity_from_profile,
)
from app.infrastructure.providers.redfish.provider import (
    AUTH_REJECTED_MARKER,
    UNREACHABLE_MARKER,
)
from app.infrastructure.providers.redfish.targets import RedfishCredential, RedfishTarget

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.OPENMANAGE.value


# Both leave a server unmeasured — docs/dell-collectors.md, "Collection flow".
_UNCOLLECTED_MARKERS = (UNREACHABLE_MARKER, AUTH_REJECTED_MARKER)


def _is_uncollected(message: str, host: str) -> bool:
    """
    Whether a `collection_errors` entry means `host` was not read at all.

    Args:
        message (str): One entry from `redfish.collection_errors`.
        host (str): A candidate host to test the message against.

    Returns:
        bool: `True` when `message` is that host's connection failure or
            its rejected login.
    """
    return any(message.startswith(f"{host}{marker}") for marker in _UNCOLLECTED_MARKERS)


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
        identities = await self._discover()
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
        uncollected: set[str] = set()
        try:
            async with contextlib.aclosing(redfish.collect()) as servers:
                async for server in servers:
                    parsed = parse_bmc_address(server.bmc_address_raw)
                    if parsed is not None and parsed.host is not None:
                        reached.add(parsed.host)
                    yield self._merged(server, identities)
        finally:
            # Runs on early close too — docs/dell-collectors.md, "Collection flow".
            for message in redfish.collection_errors:
                host = next(
                    (h for h in identities if h not in reached and _is_uncollected(message, h)),
                    None,
                )
                if host is not None:
                    uncollected.add(host)
                else:
                    self._record_error(message)

        for host in uncollected:
            yield self._unreachable_server(identities[host])

    async def _discover(self) -> dict[str, OmeIdentity]:
        """
        Enumerate the appliance in two bulk calls and keep the matched profiles.

        See docs/dell-collectors.md, "Collection flow".

        Returns:
            dict[str, OmeIdentity]: Matched identities, keyed by BMC
                address (`OmeIdentity.idrac_ip`). Keyed by address because
                that is what the Redfish pass reports back and what joins
                the two halves.
        """
        async with self._new_client() as client:
            profiles = await client.get_all("/ProfileService/Profiles")
            devices = await client.get_all("/DeviceService/Devices")

        # Joined on display name, not address — docs/dell-collectors.md, 2026-09-10.
        device_by_ip = {
            str(device.get("DeviceName")): device
            for device in devices
            if device.get("DeviceName") is not None
        }
        logger.info(
            "ome.enumerated",
            endpoint=self._endpoint,
            profiles=len(profiles),
            devices=len(devices),
        )

        identities: dict[str, OmeIdentity] = {}
        unassigned = 0
        for profile in profiles:
            if not self._matches(profile):
                continue
            display_name = str(profile.get("TargetName") or "")
            identity = identity_from_profile(
                profile=profile, device=device_by_ip.get(display_name, {})
            )
            host = identity.idrac_ip
            if not host:
                unassigned += 1
                continue
            identities[host] = identity

        if unassigned:
            logger.info(
                "ome.profile_without_address",
                endpoint=self._endpoint,
                profiles=unassigned,
                hint=(
                    "Profiles with no target device are undeployed templates "
                    "with no server behind them and were not collected."
                ),
            )
        return identities

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
            host=str(identity.idrac_ip),
            port=self._bmc_port,
            credential=self._bmc_credential,
            verify_tls=self._bmc_verify_tls,
            verify_tls_reason=self._bmc_verify_tls_reason,
            ca_bundle=self._bmc_ca_bundle,
            name=identity.name,
        )

    def _unreachable_server(self, identity: OmeIdentity) -> ProviderServer:
        """
        Build the placeholder for a profile OME knows but whose iDRAC gave nothing.

        See docs/dell-collectors.md's "Collection flow" update.

        Args:
            identity (OmeIdentity): The uncollected profile's OME identity.

        Returns:
            ProviderServer: `reachable=False`, every hardware/network field
                `None`. `IngestService` carries the rest forward.
        """
        logger.error("ome.server_unreachable", host=identity.idrac_ip, name=identity.name)
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
