"""`ServerInventoryProvider` for a single Cisco UCS Manager domain.

See docs/cisco-collectors.md, "Shared object model and DN joins".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncGenerator
from typing import Any

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.infrastructure.providers.ucs_common import (
    bmc_interface as _bmc_interface,
)
from app.infrastructure.providers.ucs_common import (
    group_by_owning_server_dn as _group_by_owning_server_dn,
)
from app.infrastructure.providers.ucs_common import (
    is_equipped as _is_equipped,
)
from app.infrastructure.providers.ucs_common import (
    management_ip_by_parent_dn as _management_ip_by_parent_dn,
)
from app.infrastructure.providers.ucs_common import (
    partition_profiles as _partition_profiles,
)
from app.infrastructure.providers.ucs_manager.client import UcsManagerClient
from app.infrastructure.providers.ucs_manager.mapping import compute_unit_to_provider_server


class UcsManagerProvider(ServerInventoryProvider):
    """
    Collects one UCS Manager domain's inventory.

    One instance per domain; the UCS Central collector builds one per
    registered domain. See docs/cisco-collectors.md, "Shared object model".
    """

    provider_type = ManagerType.UCS_MANAGER.value

    def __init__(
        self, *, manager: Manager, credentials: ManagerConnection, timeout_seconds: float
    ) -> None:
        """
        Bind a provider to one UCS Manager domain.

        Args:
            manager (Manager): The manager this run reports under. Its `id`
                becomes each server's `manager_id`, its `endpoint` is the
                domain to connect to.
            credentials (ManagerConnection): Login for the domain.
            timeout_seconds (float): Per-socket-operation timeout.

        Raises:
            ValueError: If `manager` has no endpoint configured.
        """
        if not manager.endpoint:
            raise ValueError(f"Manager {manager.id!r} has no endpoint configured.")
        super().__init__()
        self._endpoint: str = manager.endpoint
        self._manager = manager
        self._timeout_seconds = timeout_seconds
        self._credentials = credentials

    def _new_client(self) -> UcsManagerClient:
        """
        Build a client, and so a login session, for this domain.

        Returns:
            UcsManagerClient: A fresh client. Sessions are never shared or
                reused across calls.
        """
        return UcsManagerClient(
            endpoint=self._endpoint,
            username=self._credentials.username,
            password=self._credentials.password,
            timeout_seconds=self._timeout_seconds,
        )

    async def health_check(self) -> None:
        """
        Verify the domain is reachable and the credentials are accepted.

        Raises:
            UcsManagerConnectionError: If login fails for any reason.
        """
        client = self._new_client()
        try:
            await client.login()
        finally:
            await client.logout()

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """
        Fetch one compute unit and its whole subtree via one hierarchical `query_dn` (ADR-0032).

        `lsServer`/`networkElement`/`topSystem` stay whole-domain queries — see ADR-0032.

        Args:
            identity (ServerIdentity): `external_id` is this domain's own
                DN for the `computeBlade`/`computeRackUnit`.

        Returns:
            ProviderServer | None: The current state, or `None` when the
                DN no longer resolves or is no longer equipped.
        """
        if not identity.external_id:
            return None
        client = self._new_client()
        try:
            await client.login()
            mos = await client.query_dn(identity.external_id, hierarchy=True)
            server_mo = next((mo for mo in mos if mo.dn == identity.external_id), None)
            if server_mo is None or not _is_equipped(server_mo):
                return None

            by_class: dict[str, list[Any]] = defaultdict(list)
            for mo in mos:
                by_class[getattr(mo, "_class_id", "")].append(mo)

            ls_servers = await client.query_classid("lsServer")
            profile_by_dn, template_dn_by_name = _partition_profiles(ls_servers)
            network_elements = await client.query_classid("networkElement")
            switches_by_id = {
                str(getattr(mo, "id", "")): mo for mo in network_elements if getattr(mo, "id", "")
            }
            top_system = await client.query_classid("topSystem")
            cluster_name = (
                str(getattr(top_system[0], "name", "") or "") or None if top_system else None
            )

            return compute_unit_to_provider_server(
                server_mo,
                manager_id=self._manager.id,
                profile_by_dn=profile_by_dn,
                template_dn_by_name=template_dn_by_name,
                mgmt_if=_bmc_interface(by_class.get("mgmtIf", []), server_dn=server_mo.dn),
                mgmt_ip_by_parent_dn=_management_ip_by_parent_dn(
                    (
                        *by_class.get("vnicIpV4PooledAddr", []),
                        *by_class.get("vnicIpV4StaticAddr", []),
                    )
                ),
                ext_eth_ifs=by_class.get("adaptorExtEthIf", []),
                host_eth_ifs=by_class.get("adaptorHostEthIf", []),
                cpu_units=by_class.get("processorUnit", []),
                disk_units=by_class.get("storageLocalDisk", []),
                psu_units=by_class.get("equipmentPsu", []),
                card_units=by_class.get("graphicsCard", []),
                switches_by_id=switches_by_id,
                cluster_name=cluster_name,
            )
        finally:
            await client.logout()

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield every physically-present server in the domain.

        `collect()` wraps this in `contextlib.aclosing`, so an abandoned run
        still logs out — see docs/cisco-collectors.md, "Sessions".

        Yields:
            ProviderServer: One equipped compute unit, already normalized.

        Raises:
            UcsManagerConnectionError: On login failure or any query
                failure.

        See docs/cisco-collectors.md, "Shared object model and DN joins"
        and "Adapter interfaces, MACs and fabric attachments".
        """
        client = self._new_client()
        try:
            await client.login()

            blades = await client.query_classid("computeBlade")
            rack_units = await client.query_classid("computeRackUnit")
            ls_servers = await client.query_classid("lsServer")
            mgmt_ifs = await client.query_classid("mgmtIf")
            mgmt_ip_pooled = await client.query_classid("vnicIpV4PooledAddr")
            mgmt_ip_static = await client.query_classid("vnicIpV4StaticAddr")
            ext_eth_ifs_all = await client.query_classid("adaptorExtEthIf")
            host_eth_ifs_all = await client.query_classid("adaptorHostEthIf")
            cpu_units_all = await client.query_classid("processorUnit")
            disk_units_all = await client.query_classid("storageLocalDisk")
            psu_units_all = await client.query_classid("equipmentPsu")
            card_units_all = await client.query_classid("graphicsCard")
            network_elements = await client.query_classid("networkElement")
            switches_by_id = {
                str(getattr(mo, "id", "")): mo for mo in network_elements if getattr(mo, "id", "")
            }
            top_system = await client.query_classid("topSystem")
            cluster_name = (
                str(getattr(top_system[0], "name", "") or "") or None if top_system else None
            )

            profile_by_dn, template_dn_by_name = _partition_profiles(ls_servers)

            servers = [mo for mo in (*blades, *rack_units) if _is_equipped(mo)]
            server_dns = [mo.dn for mo in servers]
            mgmt_ifs_by_server = _group_by_owning_server_dn(mgmt_ifs, server_dns=server_dns)
            mgmt_ip_by_parent_dn = _management_ip_by_parent_dn((*mgmt_ip_pooled, *mgmt_ip_static))
            ext_eth_ifs_by_server = _group_by_owning_server_dn(
                ext_eth_ifs_all, server_dns=server_dns
            )
            host_eth_ifs_by_server = _group_by_owning_server_dn(
                host_eth_ifs_all, server_dns=server_dns
            )
            cpu_units_by_server = _group_by_owning_server_dn(cpu_units_all, server_dns=server_dns)
            disk_units_by_server = _group_by_owning_server_dn(disk_units_all, server_dns=server_dns)
            psu_units_by_server = _group_by_owning_server_dn(psu_units_all, server_dns=server_dns)
            card_units_by_server = _group_by_owning_server_dn(card_units_all, server_dns=server_dns)

            for server_mo in servers:
                mgmt_if = _bmc_interface(mgmt_ifs_by_server[server_mo.dn], server_dn=server_mo.dn)
                yield compute_unit_to_provider_server(
                    server_mo,
                    manager_id=self._manager.id,
                    profile_by_dn=profile_by_dn,
                    template_dn_by_name=template_dn_by_name,
                    mgmt_if=mgmt_if,
                    mgmt_ip_by_parent_dn=mgmt_ip_by_parent_dn,
                    ext_eth_ifs=ext_eth_ifs_by_server[server_mo.dn],
                    host_eth_ifs=host_eth_ifs_by_server[server_mo.dn],
                    cpu_units=cpu_units_by_server[server_mo.dn],
                    disk_units=disk_units_by_server[server_mo.dn],
                    psu_units=psu_units_by_server[server_mo.dn],
                    card_units=card_units_by_server[server_mo.dn],
                    switches_by_id=switches_by_id,
                    cluster_name=cluster_name,
                )
        finally:
            await client.logout()
