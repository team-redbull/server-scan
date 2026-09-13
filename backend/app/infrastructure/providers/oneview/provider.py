"""`ServerInventoryProvider` for HPE servers — the OneView entry point.

**OneView is the only source for every HPE server, whatever its iLO
generation.** This collector never contacts a BMC, has no BMC
credentials, and has no per-generation branch in its collection path.
That is a deliberate user decision for a mixed iLO 4/5/6 estate — one
collection standard for all HP hardware — and what it costs on Gen10 and
Gen11 is written down in
docs/adr/0022-oneview-only-hpe-collector.md rather than worked around.

**The whole appliance costs a handful of requests.**
`GET /rest/server-hardware` returns the complete object per member, not a
summary, and `expand=all` folds each server's DIMMs, drives and PCI
devices into that same response. So three paginated calls — profiles,
profile templates, expanded hardware — cover everything except power
supplies and, for some servers, processors.

Power supplies and processors are the two potentially-per-server calls,
each tried the cheap way first (many servers' `expand=all` response
already carries both — confirmed on a live appliance 2026-09-07). PSUs
are worth it because `IngestService` had no provider populating `psus`
before OneView shipped, while the health engine had carried
`power.psu_count`/`power.failed_psu_count` the whole time. Processors are
the only source for `cpu_threads` — `server-hardware`'s own fields carry
`processorCount`/`processorCoreCount` but no thread count. Each fan-out
is bounded by its own semaphore and can be switched off independently
(`INVENTORY_ONEVIEW_COLLECT_PSUS`, `INVENTORY_ONEVIEW_COLLECT_CPU_THREADS`),
because either one is the difference between ~15 requests and ~2500 for
whatever fraction of the estate its bulk sweep didn't already cover.

One appliance, one endpoint, exactly like every other vendor here — see
docs/adr/0012. The 2500-server-per-appliance ceiling is documented in
docs/hpe-collectors.md, not coded around.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import AsyncGenerator, Callable
from typing import Any

import structlog

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.infrastructure.providers.oneview.client import (
    DEFAULT_PAGE_SIZE,
    EXPANDED_PAGE_SIZE,
    OneViewClient,
    OneViewConnectionError,
)
from app.infrastructure.providers.oneview.mapping import (
    DEVICES,
    POWER_SUPPLIES,
    PROCESSORS,
    OneViewProfile,
    ilo_generation,
    profile_from,
    server_from,
    subresource,
    subresource_data,
)

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.ONEVIEW.value


async def _fetch_subresource(
    client: OneViewClient, uri: str, resource: str
) -> list[dict[str, Any]] | None:
    """
    Fetch one server's own per-server subresource collection, containing its failure.

    Shared by `_power_supplies`, `_processors` and `get_one` (docs/hpe-collectors.md).

    Args:
        client (OneViewClient): The logged-in client.
        uri (str): The owning server-hardware URI.
        resource (str): `"powerSupplies"` or `"processors"`.

    Returns:
        list[dict[str, Any]] | None: The rows, or `None` if the call
            failed or reported a state other than `Collected`.
    """
    try:
        body = await client.get_json(f"{uri}/{resource}")
        data = body.get("data")
        rows = data.get("Members") if isinstance(data, dict) else data
        if body.get("collectionState") != "Collected" or not isinstance(rows, list):
            return None
        return [row for row in rows if isinstance(row, dict)]
    except Exception:
        # Parse included on purpose — docs/hpe-collectors.md, "Power supplies".
        return None


_SERVER_PROFILES = "/rest/server-profiles"
_SERVER_PROFILE_TEMPLATES = "/rest/server-profile-templates"
_SERVER_HARDWARE = "/rest/server-hardware"


class OneViewProvider(ServerInventoryProvider):
    """
    Collects one HPE OneView appliance's inventory.

    See docs/adr/0022-oneview-only-hpe-collector.md.
    """

    provider_type = _PROVIDER_TYPE

    def __init__(
        self,
        *,
        manager: Manager,
        credentials: ManagerConnection,
        timeout_seconds: float,
        name_pattern: str = "",
        page_size: int = DEFAULT_PAGE_SIZE,
        collect_psus: bool = True,
        psu_concurrency: int = 8,
        collect_cpu_threads: bool = True,
        cpu_threads_concurrency: int = 8,
        api_version: int = 0,
        verify_tls: bool = False,
        client_factory: Callable[[], OneViewClient] | None = None,
    ) -> None:
        """
        Bind a provider to one appliance.

        Args:
            manager (Manager): The manager this run reports under. Its
                `id` becomes each server's `manager_id`; its `endpoint`
                is the appliance.
            credentials (ManagerConnection): The OneView login
                (`INVENTORY_ONEVIEW_IP`/`_USERNAME`/`_PASSWORD`).
            timeout_seconds (float): Per-request timeout.
            name_pattern (str): Regex; a profile whose name does not
                match is dropped before its hardware is mapped and before
                its power supplies are fetched. Empty collects
                everything. Only an efficiency gate — the authoritative
                filter is `tools.run_collector`'s wrapper.
            page_size (int): Explicit `count` for the plain collection
                GETs. Never `-1`, which means 64 on the profiles
                resource.
            collect_psus (bool): Whether to make the per-server
                `/powerSupplies` call for servers the expanded payload
                did not already cover. Off turns a ~2500-request run
                back into a ~15-request one, at the cost of every
                server's `psus` being unread.
            psu_concurrency (int): How many of those calls run at once.
            collect_cpu_threads (bool): Whether to make the per-server
                `/processors` call for servers the expanded payload did
                not already cover. This is the only source OneView has
                for `cpu_threads` — `server-hardware`'s own fields carry
                no thread count. Off leaves every such server's
                `cpu_threads` unread, the same trade `collect_psus` makes
                for `psus`.
            cpu_threads_concurrency (int): How many of those calls run at
                once.
            api_version (int): `X-Api-Version` override; `0` discovers
                and clamps it.
            verify_tls (bool): Whether to verify the appliance's TLS
                certificate.
            client_factory (Callable[[], OneViewClient] | None): Builds
                the client. Injected so a test can substitute the
                transport; `None` builds the real one.

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
        self._pattern = re.compile(name_pattern) if name_pattern else None
        self._page_size = page_size
        self._collect_psus = collect_psus
        self._psu_concurrency = max(1, psu_concurrency)
        self._collect_cpu_threads = collect_cpu_threads
        self._cpu_threads_concurrency = max(1, cpu_threads_concurrency)
        self._api_version = api_version
        self._verify_tls = verify_tls
        self._client_factory = client_factory or self._new_client

    def _new_client(self) -> OneViewClient:
        """
        Build a client for this appliance. Sessions are never shared.

        Returns:
            OneViewClient: A fresh, not-yet-logged-in client.
        """
        return OneViewClient(
            endpoint=self._endpoint,
            username=self._credentials.username,
            password=self._credentials.password,
            timeout_seconds=self._timeout_seconds,
            api_version=self._api_version,
            verify_tls=self._verify_tls,
        )

    async def health_check(self) -> None:
        """
        Verify the appliance is reachable and the credentials accepted.

        Raises:
            OneViewConnectionError: If version discovery, login or the
                connection fails.
        """
        async with self._client_factory():
            return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """
        Fetch one server directly by its hardware URI (ADR-0032).

        Three direct-by-URI fetches — hardware, profile, template — never
        the bulk `get_all` paths `_list_servers` uses.

        Args:
            identity (ServerIdentity): `external_id` is this server's
                `/rest/server-hardware/{id}` URI.

        Returns:
            ProviderServer | None: The current state, or `None` when the
                URI is missing, no longer resolves, or carries no profile.
        """
        if not identity.external_id:
            return None
        async with self._client_factory() as client:
            try:
                hardware = await client.get_json(identity.external_id)
            except OneViewConnectionError:
                return None
            hardware_uri = str(hardware.get("uri") or "")
            profile_uri = str(hardware.get("serverProfileUri") or "")
            if not hardware_uri or not profile_uri:
                return None
            try:
                raw_profile = await client.get_json(profile_uri)
            except OneViewConnectionError:
                return None

            template_names: dict[str, str] = {}
            template_uri = raw_profile.get("serverProfileTemplateUri")
            if template_uri:
                try:
                    template = await client.get_json(str(template_uri))
                except OneViewConnectionError:
                    template = {}
                name = template.get("name")
                if name:
                    template_names[str(template_uri)] = str(name)
            profile = profile_from(raw_profile, template_names=template_names)
            if profile is None:
                return None

            power_supplies = subresource_data(hardware, POWER_SUPPLIES)
            if power_supplies is None and self._collect_psus:
                power_supplies = await _fetch_subresource(client, hardware_uri, "powerSupplies")
            processors = subresource_data(hardware, PROCESSORS)
            if processors is None and self._collect_cpu_threads:
                processors = await _fetch_subresource(client, hardware_uri, "processors")

        return server_from(
            hardware=hardware,
            profile=profile,
            manager_id=self._manager.id,
            power_supplies=power_supplies,
            processors=processors,
        )

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Yield every matched HPE server on the appliance.

        `collect()` (the base class) resets `collection_errors` before
        calling this.

        Yields:
            ProviderServer: One HPE server, named by its server profile
                and measured by OneView.

        Raises:
            OneViewConnectionError: On login failure or a failure of any
                bulk enumeration call. There is one appliance, so its
                failure is the run's failure.
        """
        async with self._client_factory() as client:
            profiles = await client.get_all(_SERVER_PROFILES, page_size=self._page_size)
            templates = await client.get_all(_SERVER_PROFILE_TEMPLATES, page_size=self._page_size)
            # `expand=all` inlines the subresources — docs/hpe-collectors.md, "Subresources".
            hardware = await client.get_all(
                _SERVER_HARDWARE, page_size=EXPANDED_PAGE_SIZE, params={"expand": "all"}
            )
            matched = self._matched(profiles=profiles, templates=templates, hardware=hardware)
            power_supplies = await self._power_supplies(client, [member for member, _ in matched])
            processors = await self._processors(client, [member for member, _ in matched])
            # Truncation is PARTIAL — docs/hpe-collectors.md, "Pagination".
            for message in client.truncations:
                self._record_error(message)

        logger.info(
            "oneview.collected",
            endpoint=self._endpoint,
            profiles=len(profiles),
            hardware=len(hardware),
            servers=len(matched),
        )
        for member, profile in matched:
            yield server_from(
                hardware=member,
                profile=profile,
                manager_id=self._manager.id,
                power_supplies=power_supplies.get(str(member.get("uri") or "")),
                processors=processors.get(str(member.get("uri") or "")),
            )

    def _matched(
        self,
        *,
        profiles: list[dict[str, Any]],
        templates: list[dict[str, Any]],
        hardware: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], OneViewProfile]]:
        """
        Join hardware to profiles in memory and keep what this run collects.

        Args:
            profiles (list[dict[str, Any]]): `/rest/server-profiles`.
            templates (list[dict[str, Any]]): profile templates.
            hardware (list[dict[str, Any]]): `/rest/server-hardware`.

        Returns:
            list[tuple[dict[str, Any], OneViewProfile]]: Each matched
                server-hardware member with the profile that names it.
        """
        template_names = {
            uri: name
            for uri, name in (
                (str(t.get("uri") or ""), str(t.get("name") or "")) for t in templates
            )
            if uri and name
        }
        by_uri: dict[str, OneViewProfile] = {}
        for raw in profiles:
            parsed = profile_from(raw, template_names=template_names)
            if parsed is not None:
                by_uri[parsed.uri] = parsed

        matched: list[tuple[dict[str, Any], OneViewProfile]] = []
        unassigned = 0
        filtered = 0
        unreadable: Counter[str] = Counter()
        for member in hardware:
            profile = by_uri.get(str(member.get("serverProfileUri") or ""))
            if profile is None:
                # No profile, no usable name — docs/hpe-collectors.md, "The name trap".
                unassigned += 1
                continue
            if self._pattern is not None and not self._pattern.search(profile.name):
                filtered += 1
                continue
            self._count_unreadable(member, unreadable)
            matched.append((member, profile))

        if unassigned:
            logger.info(
                "oneview.hardware_without_profile",
                endpoint=self._endpoint,
                servers=unassigned,
                hint=(
                    "Server hardware with no assigned server profile has no "
                    "operator-assigned name and was not collected."
                ),
            )
        if filtered:
            logger.info("oneview.profiles_filtered", endpoint=self._endpoint, servers=filtered)
        if unreadable:
            # One aggregated line, not one per host — "Subresources and `collectionState`".
            logger.warning(
                "oneview.subresources_unreadable",
                endpoint=self._endpoint,
                servers=sum(unreadable.values()),
                by_state_and_generation=dict(unreadable),
                hint=(
                    "These servers report drives and PCI devices as unread (None), not "
                    "as zero — the stored values are carried forward. iLO 4 cannot "
                    "report any subresource; HPE's documented minimum is iLO 5 v1.20."
                ),
            )
        return matched

    async def _power_supplies(
        self, client: OneViewClient, hardware: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Collect each matched server's power supplies, the cheap way first.

        The expanded payload where it carried them, a bounded per-server
        call for the rest (docs/hpe-collectors.md, "Power supplies").

        Args:
            client (OneViewClient): The logged-in client.
            hardware (list[dict[str, Any]]): The matched members.

        Returns:
            dict[str, list[dict[str, Any]]]: Server-hardware URI -> its
                `PowerSupplies` rows. A URI absent from this map is
                reported as unread, never as no power supplies.
        """
        collected: dict[str, list[dict[str, Any]]] = {}
        to_fetch: list[str] = []
        for member in hardware:
            uri = str(member.get("uri") or "")
            rows = subresource_data(member, POWER_SUPPLIES)
            if rows is None:
                to_fetch.append(uri)
            else:
                collected[uri] = rows

        logger.info(
            "oneview.power_supply_source",
            endpoint=self._endpoint,
            from_expand=len(collected),
            per_server_calls=len(to_fetch) if self._collect_psus else 0,
            collect_psus=self._collect_psus,
        )
        if not self._collect_psus or not to_fetch:
            return collected

        semaphore = asyncio.Semaphore(self._psu_concurrency)

        async def fetch(uri: str) -> tuple[str, list[dict[str, Any]] | None]:
            async with semaphore:
                return uri, await _fetch_subresource(client, uri, "powerSupplies")

        failures = 0
        results = await asyncio.gather(*(fetch(uri) for uri in to_fetch), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                failures += 1
                continue
            uri, rows = result
            if rows is None:
                failures += 1
            else:
                collected[uri] = rows
        if failures:
            logger.warning(
                "oneview.power_supplies_unreadable",
                endpoint=self._endpoint,
                servers=failures,
                of=len(to_fetch),
            )
        return collected

    async def _processors(
        self, client: OneViewClient, hardware: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Collect each matched server's processors, the cheap way first.

        Same shape as `_power_supplies`; `cpu_threads`' only source
        (docs/hpe-collectors.md, "CPU threads").

        Args:
            client (OneViewClient): The logged-in client.
            hardware (list[dict[str, Any]]): The matched members.

        Returns:
            dict[str, list[dict[str, Any]]]: Server-hardware URI -> its
                `Processors` rows. A URI absent from this map is reported
                as unread, never as zero threads.
        """
        collected: dict[str, list[dict[str, Any]]] = {}
        to_fetch: list[str] = []
        for member in hardware:
            uri = str(member.get("uri") or "")
            rows = subresource_data(member, PROCESSORS)
            if rows is None:
                to_fetch.append(uri)
            else:
                collected[uri] = rows

        logger.info(
            "oneview.processor_source",
            endpoint=self._endpoint,
            from_expand=len(collected),
            per_server_calls=len(to_fetch) if self._collect_cpu_threads else 0,
            collect_cpu_threads=self._collect_cpu_threads,
        )
        if not self._collect_cpu_threads or not to_fetch:
            return collected

        semaphore = asyncio.Semaphore(self._cpu_threads_concurrency)

        async def fetch(uri: str) -> tuple[str, list[dict[str, Any]] | None]:
            async with semaphore:
                return uri, await _fetch_subresource(client, uri, "processors")

        failures = 0
        results = await asyncio.gather(*(fetch(uri) for uri in to_fetch), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                failures += 1
                continue
            uri, rows = result
            if rows is None:
                failures += 1
            else:
                collected[uri] = rows
        if failures:
            logger.warning(
                "oneview.processors_unreadable",
                endpoint=self._endpoint,
                servers=failures,
                of=len(to_fetch),
            )
        return collected

    @staticmethod
    def _count_unreadable(member: dict[str, Any], tally: Counter[str]) -> None:
        """
        Record one server whose subresources could not be read.

        Keyed by `<collectionState>/iLO<generation>` — "why" and "which hardware".

        Args:
            member (dict[str, Any]): One `/rest/server-hardware` member.
            tally (Counter[str]): The running tally.
        """
        envelope = subresource(member, DEVICES)
        state = str(envelope.get("collectionState") or "absent")
        if state == "Collected":
            return
        generation = ilo_generation(member.get("mpModel"))
        tally[f"{state}/iLO{generation if generation is not None else '?'}"] += 1
