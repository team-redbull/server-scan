"""`ServerInventoryProvider` for Cisco UCS Central, the only Cisco entry point.

Central is asked which domains are registered and which service-profile
names live in each; every field of every `ProviderServer` then comes from
that domain's own UCS Manager via `..ucs_manager.provider.UcsManagerProvider`.
See docs/cisco-collectors.md, "UCS Central domain discovery and pruning".
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncGenerator, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

import structlog

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.infrastructure.providers.ucs_central.client import UcsCentralClient
from app.infrastructure.providers.ucs_common import TEMPLATE_TYPES
from app.infrastructure.providers.ucs_manager.provider import UcsManagerProvider

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.UCS_CENTRAL.value

_UCSM_ROOT = "sys/"
_CENTRAL_ROOT = "compute/sys-{domain_id}/"


def domain_id_from_dn(dn: str) -> str | None:
    """
    Extract the registered domain from a UCS Central distinguished name.

    See docs/cisco-collectors.md, "UCS Central domain discovery and pruning".

    Args:
        dn (str): A UCS Central DN, e.g.
            `"compute/sys-1009/chassis-1/blade-1"`.

    Returns:
        str | None: The domain id, or None for anything not under a
            `computeSystem` — which is how global objects such as an org's
            service profiles are told apart from per-domain inventory.
    """
    parts = str(dn).split("/")
    if len(parts) < 2 or not parts[1].startswith("sys-"):
        return None
    return parts[1][len("sys-") :] or None


@dataclass(frozen=True, slots=True)
class DomainTarget:
    """
    One registered UCS Manager domain, as UCS Central describes it.

    Attributes:
        domain_id (str): Central's `ComputeSystem.id` for the domain.
        name (str): The domain's registered name.
        endpoint (str): Address to open a UCS Manager session against.
    """

    domain_id: str
    name: str
    endpoint: str


def central_external_id(external_id: str, *, domain_id: str) -> str:
    """
    Re-root a UCS Manager DN into the UCS Central DN for the same object.

    See docs/cisco-collectors.md, "UCS Central domain discovery and pruning".

    Args:
        external_id (str): The DN as UCS Manager reported it.
        domain_id (str): The owning domain's Central id.

    Returns:
        str: The domain-qualified DN, or `external_id` unchanged if it is
            not rooted at `sys/`.
    """
    if not external_id.startswith(_UCSM_ROOT):
        return external_id
    return _CENTRAL_ROOT.format(domain_id=domain_id) + external_id[len(_UCSM_ROOT) :]


def _profiles_by_key(ls_servers: Iterable[Any]) -> dict[str, list[str]]:
    """
    Group service-profile names by the domain UCS Central says they are in.

    See docs/cisco-collectors.md, "Service profiles and server names".

    Args:
        ls_servers (Iterable[Any]): The full result of one `lsServer` query
            against Central, carrying both profiles and templates.

    Returns:
        dict[str, list[str]]: Profile names keyed by `LsServer.domain`.
            Templates and profiles with no domain are excluded.
    """
    grouped: dict[str, list[str]] = {}
    for mo in ls_servers:
        if str(getattr(mo, "type", "") or "") in TEMPLATE_TYPES:
            continue
        key = str(getattr(mo, "domain", "") or "").strip()
        if not key:
            continue
        name = str(getattr(mo, "name", "") or "")
        if name:
            grouped.setdefault(key, []).append(name)
    return grouped


def domains_to_collect(
    domains: Iterable[Any],
    ls_servers: Iterable[Any],
    *,
    name_pattern: str,
) -> tuple[list[DomainTarget], list[DomainTarget]]:
    """
    Split UCS Central's registered domains into those worth contacting and those to skip.

    Skipping means "do not open a session", never a deletion — see
    docs/cisco-collectors.md, "The pruning rules".

    Args:
        domains (Iterable[Any]): `computeSystem` managed objects from
            Central.
        ls_servers (Iterable[Any]): `lsServer` managed objects from Central.
        name_pattern (str): Regex applied with `re.search`; empty collects
            every domain.

    Returns:
        tuple[list[DomainTarget], list[DomainTarget]]: The domains to
            collect, and the domains skipped.
    """
    profiles = _profiles_by_key(ls_servers)
    pattern = re.compile(name_pattern) if name_pattern else None

    to_collect: list[DomainTarget] = []
    skipped: list[DomainTarget] = []
    for mo in domains:
        name = str(getattr(mo, "name", "") or "").strip()
        endpoint = str(getattr(mo, "address", "") or "").strip() or name
        target = DomainTarget(
            domain_id=str(getattr(mo, "id", "") or ""),
            name=name,
            endpoint=endpoint,
        )
        if not endpoint:
            logger.warning(
                "ucs_central.domain_without_address",
                domain_id=target.domain_id,
                domain_name=target.name,
            )
            skipped.append(target)
            continue

        known = profiles.get(name) or profiles.get(endpoint) or profiles.get(target.domain_id) or []
        if pattern is not None and known and not any(pattern.search(n) for n in known):
            skipped.append(target)
            continue
        to_collect.append(target)

    return to_collect, skipped


class UcsCentralProvider(ServerInventoryProvider):
    """
    Collect every registered UCS Manager domain in one run.

    Central is a directory; each domain's own UCS Manager is the source.
    See docs/cisco-collectors.md, "UCS Central domain discovery and pruning".
    """

    provider_type = _PROVIDER_TYPE

    def __init__(
        self,
        *,
        manager: Manager,
        credentials: ManagerConnection,
        timeout_seconds: float,
        domain_login: tuple[str, str],
        name_pattern: str = "",
        concurrency: int = 4,
        client_factory: Callable[[], Any] | None = None,
        domain_provider_factory: Callable[[DomainTarget], Any] | None = None,
    ) -> None:
        """
        Build a collector for one UCS Central instance.

        Args:
            manager (Manager): The `Manager` document this run collects for;
                its endpoint addresses UCS Central.
            credentials (ManagerConnection): UCS Central endpoint and login.
            timeout_seconds (float): Deadline applied to each SDK call.
            domain_login (tuple[str, str]): UCS Manager username and
                password, valid on every registered domain.
            name_pattern (str): Regex used to skip domains holding no
                matching service profile. Empty collects every domain.
            concurrency (int): Maximum domains contacted at once; values
                below 1 are raised to 1.
            client_factory (Callable[[], Any] | None): Test seam returning a
                UCS Central client. Defaults to a real one.
            domain_provider_factory (Callable[[DomainTarget], Any] | None):
                Test seam returning a per-domain provider. Defaults to a
                real `UcsManagerProvider`.

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
        self._domain_login = domain_login
        self._name_pattern = name_pattern
        self._concurrency = max(1, concurrency)
        self._client_factory: Callable[[], Any] = client_factory or self._new_client
        self._domain_provider_factory: Callable[[DomainTarget], Any] = (
            domain_provider_factory or self._new_domain_provider
        )

    def _new_client(self) -> UcsCentralClient:
        """
        Build a UCS Central client from this collector's configuration.

        Returns:
            UcsCentralClient: An unauthenticated client for the configured
                endpoint.
        """
        return UcsCentralClient(
            endpoint=self._endpoint,
            username=self._credentials.username,
            password=self._credentials.password,
            timeout_seconds=self._timeout_seconds,
        )

    def _new_domain_provider(self, target: DomainTarget) -> UcsManagerProvider:
        """
        Build a UCS Manager collector for one registered domain.

        Reuses this collector's `Manager` with only the endpoint swapped, so
        every server keeps the single `mgr_ucs_central` manager id.

        Args:
            target (DomainTarget): The domain to collect from.

        Returns:
            UcsManagerProvider: A collector bound to that domain's address.
        """
        username, password = self._domain_login
        return UcsManagerProvider(
            manager=self._manager.model_copy(update={"endpoint": target.endpoint}),
            credentials=ManagerConnection(
                endpoint=target.endpoint, username=username, password=password
            ),
            timeout_seconds=self._timeout_seconds,
        )

    async def health_check(self) -> None:
        """
        Verify UCS Central accepts this collector's login.

        Touches Central only; per-domain logins happen during collection.

        Raises:
            UcsCentralConnectionError: If the login fails.
        """
        client = self._client_factory()
        try:
            await client.login()
        finally:
            await client.logout()

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """
        Find one service profile in Central, then recheck only its own domain (ADR-0032).

        Mirrors `BareMetalHostUCS`'s own `ucs_server_strategy.get_server_info`.

        Args:
            identity (ServerIdentity): `name` is the service profile name.

        Returns:
            ProviderServer | None: The current state, with a
                domain-qualified `external_id`, or `None` when the name
                no longer resolves to a domain and physical node.
        """
        if not identity.name:
            return None
        client = self._client_factory()
        try:
            await client.login()
            ls_servers = await client.query_classid("lsServer")
            domains = await client.query_classid("computeSystem")
        finally:
            await client.logout()

        match = next(
            (
                mo
                for mo in ls_servers
                if str(getattr(mo, "type", "") or "") not in TEMPLATE_TYPES
                and str(getattr(mo, "name", "") or "").upper() == identity.name.upper()
            ),
            None,
        )
        if match is None:
            return None
        domain_key = str(getattr(match, "domain", "") or "").strip()
        pn_dn = str(getattr(match, "pn_dn", "") or "").strip()
        if not domain_key or not pn_dn:
            return None

        domain_mo = next(
            (
                mo
                for mo in domains
                if domain_key
                in (
                    str(getattr(mo, "name", "") or ""),
                    str(getattr(mo, "id", "") or ""),
                    str(getattr(mo, "address", "") or ""),
                )
            ),
            None,
        )
        if domain_mo is None:
            return None
        endpoint = (
            str(getattr(domain_mo, "address", "") or "").strip()
            or str(getattr(domain_mo, "name", "") or "").strip()
        )
        if not endpoint:
            return None

        target = DomainTarget(
            domain_id=str(getattr(domain_mo, "id", "") or ""),
            name=str(getattr(domain_mo, "name", "") or ""),
            endpoint=endpoint,
        )
        domain_provider = self._domain_provider_factory(target)
        fresh = await domain_provider.get_one(
            ServerIdentity(external_id=pn_dn, serial=identity.serial)
        )
        if fresh is None:
            return None
        return replace(
            fresh, external_id=central_external_id(fresh.external_id, domain_id=target.domain_id)
        )

    async def _plan(self) -> tuple[list[DomainTarget], dict[str, Any]]:
        """
        Run the two UCS Central queries and decide which domains to contact.

        See docs/cisco-collectors.md, "UCS Central domain discovery and
        pruning".

        Returns:
            tuple[list[DomainTarget], dict[str, Any]]: The domains to
                collect, and every registered `computeSystem` MO keyed by
                domain id for later reporting.

        Raises:
            UcsCentralConnectionError: If either Central query fails.
        """
        client = self._client_factory()
        try:
            await client.login()
            domains = await client.query_classid("computeSystem")
            ls_servers = await client.query_classid("lsServer")
        finally:
            await client.logout()

        if not domains:
            logger.warning(
                "ucs_central.no_domains",
                endpoint=self._endpoint,
                hint=(
                    "UCS Central reported no registered UCS Manager domains, so there is "
                    "nothing to collect from. Check that domains are registered and that "
                    "this account can read them."
                ),
            )

        to_collect, skipped = domains_to_collect(
            domains, ls_servers, name_pattern=self._name_pattern
        )
        logger.info(
            "ucs_central.domain_plan",
            registered=len(domains),
            collecting=len(to_collect),
            skipping=len(skipped),
            name_pattern=self._name_pattern or None,
            concurrency=self._concurrency,
        )
        # No address is a fault, not pruning — docs, "The pruning rules".
        for t in skipped:
            if not t.endpoint:
                self._record_error(
                    f"domain {t.name or t.domain_id!r} is registered with UCS Central but "
                    "reports no address to connect to"
                )
        for target in skipped:
            logger.info(
                "ucs_central.domain_skipped",
                domain_id=target.domain_id,
                domain_name=target.name,
                endpoint=target.endpoint or None,
                reason=(
                    "no address"
                    if not target.endpoint
                    else f"no service profile matches {self._name_pattern!r}"
                ),
            )

        known_keys = {t.name for t in (*to_collect, *skipped)}
        known_keys |= {t.endpoint for t in (*to_collect, *skipped)}
        known_keys |= {t.domain_id for t in (*to_collect, *skipped)}
        unmatched = sorted(set(_profiles_by_key(ls_servers)) - known_keys)
        if unmatched:
            logger.warning(
                "ucs_central.profiles_in_unregistered_domain",
                domains=unmatched,
                hint=(
                    "UCS Central reports service profiles whose domain matches no "
                    "registered computeSystem, so those servers cannot be collected."
                ),
            )

        domain_mo_by_id = {str(getattr(mo, "id", "") or ""): mo for mo in domains}
        return to_collect, domain_mo_by_id

    async def _collect_domain(
        self, target: DomainTarget, sem: asyncio.Semaphore
    ) -> list[ProviderServer]:
        """
        Collect one domain's whole inventory from its own UCS Manager.

        Failure is contained here so an unreachable or slow domain never
        costs the fleet its run.

        Args:
            target (DomainTarget): The domain to collect from.
            sem (asyncio.Semaphore): Limits how many domains run at once.

        Returns:
            list[ProviderServer]: Every server in the domain, with
                domain-qualified external ids, or an empty list if the
                domain failed.
        """
        async with sem:
            provider = self._domain_provider_factory(target)
            collected: list[ProviderServer] = []
            try:
                # Not a comprehension: the `except` reports partial progress.
                async with contextlib.aclosing(provider.collect()) as servers:
                    async for provider_server in servers:
                        collected.append(  # noqa: PERF401
                            replace(
                                provider_server,
                                external_id=central_external_id(
                                    provider_server.external_id, domain_id=target.domain_id
                                ),
                            )
                        )
            except Exception as exc:
                logger.exception(
                    "ucs_central.domain_failed",
                    domain_id=target.domain_id,
                    domain_name=target.name,
                    endpoint=target.endpoint,
                    collected_before_failure=len(collected),
                )
                self._record_error(
                    f"domain {target.name or target.domain_id!r} ({target.endpoint}) failed "
                    f"after {len(collected)} server(s): {exc}"
                )
                return []
            logger.info(
                "ucs_central.domain_collected",
                domain_id=target.domain_id,
                domain_name=target.name,
                endpoint=target.endpoint,
                servers=len(collected),
            )
            return collected

    async def _collect_domain_result(
        self, target: DomainTarget, sem: asyncio.Semaphore
    ) -> tuple[DomainTarget, list[ProviderServer]]:
        """
        `_collect_domain`, paired with the target that produced it.

        `asyncio.as_completed` yields results in completion order, so each
        must carry its own domain — ADR-0014's 2026-09-02 update.

        Args:
            target (DomainTarget): The domain to collect from.
            sem (asyncio.Semaphore): Limits how many domains run at once.

        Returns:
            tuple[DomainTarget, list[ProviderServer]]: The target and
                whatever `_collect_domain` produced for it.
        """
        return target, await self._collect_domain(target, sem)

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Collect every domain worth contacting and yield their servers.

        Each domain is yielded as it finishes, so a killed run has persisted what
        completed (ADR-0014); early close: docs/cisco-collectors.md, "Sessions".

        Yields:
            ProviderServer: One server, with a domain-qualified external id.

        Raises:
            UcsCentralConnectionError: If UCS Central itself is unreachable.
                A single failing domain is logged and skipped instead.
        """
        targets, domain_mo_by_id = await self._plan()
        sem = asyncio.Semaphore(self._concurrency)

        collected_ids = {t.domain_id for t in targets}
        for domain_id, mo in domain_mo_by_id.items():
            if domain_id not in collected_ids:
                self._log_one_domain(mo, collected=None)

        tasks = [
            asyncio.create_task(self._collect_domain_result(target, sem)) for target in targets
        ]
        try:
            for finished in asyncio.as_completed(tasks):
                target, servers = await finished
                self._log_one_domain(domain_mo_by_id.get(target.domain_id), collected=len(servers))
                for provider_server in servers:
                    yield provider_server
        finally:
            # Cancel *and* drain so every domain still logs out — docs, "Sessions".
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _log_one_domain(self, mo: Any | None, *, collected: int | None) -> None:
        """
        Emit one domain's coverage: what UCS Central believes it holds vs. what came back.

        Called per domain as soon as its result is known, not at run end
        (ADR-0014, 2026-09-02). See docs/cisco-collectors.md, "`_log_one_domain`".

        Args:
            mo (Any | None): The registered `computeSystem` MO for this
                domain, or `None` if UCS Central never listed it at all
                (should not happen in practice — every `DomainTarget`
                comes from a `computeSystem` row — but guarded rather
                than assumed).
            collected (int | None): Servers collected from this domain,
                or `None` for a domain that was never contacted (skipped
                by pruning, or missing an address).
        """
        if mo is None:
            return
        domain_id = str(getattr(mo, "id", "") or "")
        reported = getattr(mo, "total_physical_cnt", None)
        logger.info(
            "ucs_central.domain_summary",
            domain_id=domain_id,
            domain_name=getattr(mo, "name", None),
            address=getattr(mo, "address", None),
            inventory_status=getattr(mo, "inventory_status", None),
            last_refreshed=getattr(mo, "last_refreshed_ts", None),
            reported_servers=reported,
            collected_servers=collected,
        )
        if collected == 0 and str(reported or "0") not in ("0", ""):
            logger.warning(
                "ucs_central.domain_collected_nothing",
                domain_id=domain_id,
                domain_name=getattr(mo, "name", None),
                address=getattr(mo, "address", None),
                reported_servers=reported,
                hint=(
                    "UCS Central reports this domain holds servers, but its own UCS "
                    "Manager returned none. Check that the address is reachable and "
                    "that INVENTORY_UCS_MANAGER_USERNAME/_PASSWORD is valid on this "
                    "domain — see the ucs_central.domain_failed entry if there is one."
                ),
            )
