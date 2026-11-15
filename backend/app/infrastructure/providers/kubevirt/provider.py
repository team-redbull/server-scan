"""`ServerInventoryProvider` for OpenShift Virtualization VMs behind kubevirt-redfish.

One facade serves every VM, so failures split two ways (docs/adr/0058): the
Systems listing failing (unreachable, rejected login, TLS) fails the whole run,
while one VM failing is recorded and the run moves on. A VM is a
`COLLECTION_ERROR` stub only when its record was read but could not be mapped
(ADR-0062); a facade fault is never a VM's "BMC" fault.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

import structlog

from app.domain.enums import ManagerType, Vendor
from app.domain.models.manager import Manager
from app.domain.ports.provider import (
    NotThisManagersServerError,
    ProviderServer,
    ServerIdentity,
    ServerInventoryProvider,
)
from app.infrastructure.providers.kubevirt.client import (
    SERVICE_ROOT,
    SYSTEMS,
    KubeVirtClient,
    NotFoundError,
    build_verify,
    member_links,
    origin_of,
)
from app.infrastructure.providers.kubevirt.mapping import (
    EXTERNAL_ID_SCHEME,
    external_id_for,
    serial_of,
    system_to_provider_server,
)
from app.infrastructure.providers.redfish.client import (
    RedfishAuthError,
    RedfishError,
    RedfishForbiddenError,
    RedfishProtocolError,
    RedfishUnreachableError,
    validate_odata_id,
)

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.KUBEVIRT.value
_SYSTEMS_PREFIX = f"{SYSTEMS}/"
_MAX_LISTING_PAGES = 100
_UNFINISHED_SAMPLE = 10


class KubeVirtProvider(ServerInventoryProvider):
    """Collects every VM one kubevirt-redfish user can see (docs/adr/0058)."""

    provider_type = _PROVIDER_TYPE

    def __init__(
        self,
        *,
        manager: Manager,
        url: str,
        username: str,
        password: str,
        verify_tls: bool,
        ca_bundle: str,
        connect_timeout: float,
        read_timeout: float,
        vm_budget_seconds: float,
        run_budget_seconds: float,
        concurrency: int,
        cluster_name: str = "",
        debug_http: bool = False,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        """
        Build the collector. No I/O happens here.

        Args:
            manager (Manager): The manager this run reports under.
            url (str): `INVENTORY_KUBEVIRT_URL`; only its origin is used.
            username (str): The facade user.
            password (str): Its password.
            verify_tls (bool): False is the logged opt-out.
            ca_bundle (str): A PEM to trust instead of the system store, or "".
            connect_timeout (float): Seconds to connect.
            read_timeout (float): Seconds to wait for one response.
            vm_budget_seconds (float): Wall clock allowed per VM.
            run_budget_seconds (float): Wall clock allowed for the run.
            concurrency (int): VMs read at once.
            cluster_name (str): The cluster the facade serves, stored on each VM's host (ADR-0059).
            debug_http (bool): Emit one line per request.
            client_factory (Callable[[], Any] | None): Test seam returning a client.

        Raises:
            ValueError: If `url` is not an http(s) URL.
        """
        super().__init__()
        self._manager = manager
        self._origin, self._netloc = origin_of(url)
        self._username = username
        self._password = password
        self._verify_tls = verify_tls
        self._ca_bundle = ca_bundle
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._vm_budget = vm_budget_seconds
        self._run_budget = run_budget_seconds
        self._concurrency = max(1, concurrency)
        self._cluster_name = cluster_name.strip()
        self._debug_http = debug_http
        self._client_factory: Callable[[], Any] = client_factory or self._new_client
        self._auth_rejected = False
        self._vanished = 0
        self._bad_links = 0

    def _new_client(self) -> KubeVirtClient:
        """
        Build a client for the configured facade.

        Returns:
            KubeVirtClient: A client whose pool fits this run's concurrency.
        """
        if not self._verify_tls:
            logger.warning(
                "kubevirt.tls_verification_disabled",
                origin=self._origin,
                hint="The facade password is sent to whatever answers. Set "
                "INVENTORY_KUBEVIRT_CA_BUNDLE to the ingress CA and remove the opt-out.",
            )
        elif self._origin.startswith("http://"):
            logger.warning("kubevirt.plaintext_http", origin=self._origin)
        return KubeVirtClient(
            origin=self._origin,
            username=self._username,
            password=self._password,
            verify=build_verify(verify_tls=self._verify_tls, ca_bundle=self._ca_bundle),
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
            max_connections=self._concurrency,
            debug_http=self._debug_http,
        )

    async def health_check(self) -> None:
        """
        Confirm the facade answers, without spending a login attempt.

        The service root is unauthenticated; the credential is first tested by
        `_list_servers`' Systems GET, so a wrong password costs one 401 per run.

        Raises:
            RedfishError: If the facade is unreachable, fails TLS or answers an error.
        """
        async with self._client_factory() as client:
            await client.get(SERVICE_ROOT, authenticated=False)

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """
        Re-read one VM by its `external_id`, against the configured facade (ADR-0032).

        Args:
            identity (ServerIdentity): `external_id` must be a `kubevirt://` id.

        Returns:
            ProviderServer | None: The VM's current state, or None when the id is
                malformed, the VM is gone, or the facade could not be read.

        Raises:
            NotThisManagersServerError: The id names another cluster's facade.
        """
        path = _system_path(identity.external_id)
        if path is None:
            return None
        netloc = _netloc_of(identity.external_id or "")
        if netloc.lower() != self._netloc.lower():
            logger.info("kubevirt.recheck_foreign", netloc=netloc, facade=self._netloc)
            raise NotThisManagersServerError(
                f"{identity.external_id} is served by {netloc}, not this facade ({self._netloc})"
            )
        try:
            async with asyncio.timeout(self._vm_budget), self._client_factory() as client:
                return await self._collect_system(client, path)
        except RedfishAuthError as exc:
            logger.error(  # noqa: TRY400
                "kubevirt.recheck_login_rejected", origin=self._origin, system=path, error=str(exc)
            )
            return None
        except (RedfishError, TimeoutError) as exc:
            logger.warning(
                "kubevirt.recheck_failed",
                origin=self._origin,
                system=path,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        except Exception as exc:
            logger.exception(
                "kubevirt.recheck_failed_unexpected",
                exc_info=exc,
                origin=self._origin,
                system=path,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise  # a bug is not "server gone": the caller reports a recheck failure

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        """
        Read the Systems collection once, then every VM in it concurrently.

        Yields:
            ProviderServer: One per VM read, as each finishes.

        Raises:
            RedfishAuthError: The credential was rejected (listing or mid-run).
            RedfishError: The Systems collection itself could not be read.
        """
        self._auth_rejected = False
        async with self._client_factory() as client:
            paths = await self._system_paths(client)
            semaphore = asyncio.Semaphore(self._concurrency)
            tasks = [asyncio.create_task(self._collect_vm(client, p, semaphore)) for p in paths]
            collected = 0
            done = 0
            self._vanished = 0
            try:
                for finished in asyncio.as_completed(tasks, timeout=self._run_budget):
                    try:
                        server = await finished
                    except TimeoutError:
                        unfinished = [p for t, p in zip(tasks, paths, strict=True) if not t.done()]
                        shown = [p.removeprefix(_SYSTEMS_PREFIX) for p in unfinished]
                        sample = ", ".join(shown[:_UNFINISHED_SAMPLE])
                        if len(shown) > _UNFINISHED_SAMPLE:
                            sample += f" and {len(shown) - _UNFINISHED_SAMPLE} more"
                        self._record_error(
                            f"run budget of {self._run_budget:g}s expired with "
                            f"{len(tasks) - done} VM(s) not yet collected: {sample}"
                        )
                        logger.error(  # noqa: TRY400
                            "kubevirt.run_budget_exceeded",
                            origin=self._origin,
                            budget_seconds=self._run_budget,
                            vms_unfinished=len(tasks) - done,
                            unfinished_systems=shown[:_UNFINISHED_SAMPLE],
                            hint="Raise INVENTORY_KUBEVIRT_RUN_BUDGET_SECONDS or "
                            "INVENTORY_KUBEVIRT_CONCURRENCY.",
                        )
                        break
                    done += 1
                    if server is not None:
                        if server.reachable:
                            collected += 1
                        yield server
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(
            "kubevirt.run_summary",
            vms_listed=len(paths),
            vms_collected=collected,
            vms_failed=len(paths) - collected - self._vanished,
            vms_vanished=self._vanished,
            vms_failed_stubs=self.server_failures,
            bad_member_links=self._bad_links,
        )

    async def _system_paths(self, client: Any) -> list[str]:
        """
        Read the Systems collection, following `Members@odata.nextLink` pages.

        Args:
            client (Any): An open client.

        Returns:
            list[str]: Every valid System path, deduplicated, in listing order.

        Raises:
            RedfishError: The first page could not be read.
        """
        self._bad_links = 0
        paths: dict[str, None] = {}
        page_path: str | None = SYSTEMS
        seen_pages: set[str] = set()
        for _ in range(_MAX_LISTING_PAGES):
            if page_path is None:
                break
            if page_path in seen_pages:
                self._record_error(
                    f"Systems listing repeats page {page_path!r}: the facade loops, "
                    "so the VMs after it were not listed"
                )
                logger.error(
                    "kubevirt.listing_cycle",
                    origin=self._origin,
                    page=page_path,
                    pages=len(seen_pages),
                )
                break
            seen_pages.add(page_path)
            page = await client.get(page_path)
            paths.update(dict.fromkeys(member_links(page, self._bad_link)))
            page_path = self._next_page(page)
        else:
            if page_path is not None:
                self._record_error(f"Systems listing capped at {_MAX_LISTING_PAGES} pages")
                logger.error(
                    "kubevirt.listing_capped", origin=self._origin, pages=_MAX_LISTING_PAGES
                )
        return list(paths)

    def _bad_link(self, link: object, reason: str) -> None:
        """
        Record one Systems member whose link was rejected.

        Args:
            link (object): The raw `@odata.id`.
            reason (str): Why it was rejected.
        """
        self._bad_links += 1
        self._record_error(f"Systems member link {str(link)[:200]!r} skipped: {reason}")

    def _next_page(self, page: dict[str, Any]) -> str | None:
        """
        The validated next page of a collection, or None.

        Args:
            page (dict[str, Any]): A collection payload.

        Returns:
            str | None: A path under `/redfish/v1`, or None when absent or unusable.
        """
        link = page.get("Members@odata.nextLink")
        if not isinstance(link, str) or not link:
            return None
        try:
            return validate_odata_id(link.removeprefix(self._origin))
        except RedfishProtocolError as exc:
            self._bad_link(link, str(exc))
            return None

    async def _collect_vm(
        self, client: Any, path: str, semaphore: asyncio.Semaphore
    ) -> ProviderServer | None:
        """
        Read one VM, containing every failure but a rejected login.

        Args:
            client (Any): The shared client.
            path (str): The System's `@odata.id`.
            semaphore (asyncio.Semaphore): Limits VMs in flight.

        Returns:
            ProviderServer | None: The VM, or None when it vanished or failed.

        Raises:
            RedfishAuthError: The credential was rejected mid-run; the run stops.
        """
        system_id = path.removeprefix(_SYSTEMS_PREFIX)
        seen: dict[str, Any] = {}
        async with semaphore:
            if self._auth_rejected:
                return None
            try:
                async with asyncio.timeout(self._vm_budget):
                    return await self._collect_system(client, path, seen)
            except RedfishAuthError:
                self._auth_rejected = True
                raise
            except TimeoutError:
                self._record_error(f"{system_id}: exceeded its {self._vm_budget:g}s budget")
                logger.warning("kubevirt.vm_budget_exceeded", system=system_id, path=path)
                return None
            except NotFoundError:
                self._vanished += 1
                logger.info("kubevirt.vm_vanished", system=system_id, path=path)
                return None
            except RedfishError as exc:
                return self._contain_vm(exc, "kubevirt.vm_failed", path, seen)
            except Exception as exc:
                return self._contain_vm(exc, "kubevirt.vm_failed_unexpected", path, seen)

    def _contain_vm(
        self, exc: Exception, event: str, path: str, system: dict[str, Any]
    ) -> ProviderServer | None:
        """
        Record one VM's failure; stub it only when its System was read.

        Without the System there is no firmware serial, and a stub keyed on the
        System `Id` would split a VM that normally correlates by firmware serial.

        Args:
            exc (Exception): The failure.
            event (str): The log event.
            path (str): The System's `@odata.id`.
            system (dict[str, Any]): The System payload if it was read, else empty.

        Returns:
            ProviderServer | None: A `COLLECTION_ERROR` stub, or None.
        """
        system_id = path.removeprefix(_SYSTEMS_PREFIX)
        name = str(system.get("Name") or system.get("Id") or system_id).strip() if system else None
        model = system.get("Model")
        uuid = system.get("UUID")
        return self._contain_server_failure(
            exc,
            event=event,
            stage="reading the VM" if not system else "mapping the VM",
            vendor=Vendor.KUBEVIRT.value,
            external_id=external_id_for(self._netloc, str(system.get("@odata.id") or path)),
            name=name or system_id,
            serial=serial_of(system) if system else None,
            model=model.strip() if isinstance(model, str) and model.strip() else None,
            manager_id=self._manager.id,
            bmc_address_raw=f"redfish://{self._netloc}{path}",
            system_uuid=uuid.strip() if isinstance(uuid, str) and uuid.strip() else None,
            system=system_id,
            path=path,
        )

    async def _collect_system(
        self, client: Any, path: str, seen: dict[str, Any] | None = None
    ) -> ProviderServer:
        """
        Read one System and its sub-resources and map them.

        Args:
            client (Any): An open client.
            path (str): The System's `@odata.id`.
            seen (dict[str, Any] | None): Receives the System payload once read, so a
                caller can identify the VM when a later step fails.

        Returns:
            ProviderServer: The VM.

        Raises:
            NotFoundError: The System is gone.
            RedfishError: The System itself could not be read.
        """
        system = await client.get(path)
        if seen is not None:
            seen.update(system)
        processors = None
        if not isinstance(system.get("ProcessorSummary"), dict):
            processors = await self._optional(client, system, "Processors")
        storage = await self._optional(client, system, "Storage")
        return system_to_provider_server(
            system,
            netloc=self._netloc,
            manager_id=self._manager.id,
            cluster_name=self._cluster_name,
            processors=processors,
            memory=await self._optional(client, system, "Memory"),
            storage=storage,
            drives=await self._drives(client, storage, system),
            interfaces=await self._optional(client, system, "EthernetInterfaces"),
        )

    async def _optional(
        self, client: Any, system: dict[str, Any], key: str
    ) -> list[dict[str, Any]] | None:
        """
        Read a linked sub-collection, None (never `[]`) on failure.

        Args:
            client (Any): An open client.
            system (dict[str, Any]): The owning System.
            key (str): The link property.

        Returns:
            list[dict[str, Any]] | None: The members, or None when absent or unread.
        """
        link = system.get(key)
        path = link.get("@odata.id") if isinstance(link, dict) else None
        if not path:
            return None
        try:
            return await client.get_collection(validate_odata_id(path))
        except (RedfishForbiddenError, RedfishProtocolError, RedfishUnreachableError) as exc:
            logger.warning(
                "kubevirt.resource_skipped",
                system=system.get("Id"),
                system_path=system.get("@odata.id"),
                resource=key,
                path=str(path)[:200],
                error=str(exc),
            )
            return None

    async def _drives(
        self, client: Any, storage: list[dict[str, Any]] | None, system: dict[str, Any]
    ) -> list[dict[str, Any]] | None:
        """
        Read every Drive the Storage members link to.

        Args:
            client (Any): An open client.
            storage (list[dict[str, Any]] | None): `Storage` members, or None.
            system (dict[str, Any]): The owning System, for log fields.

        Returns:
            list[dict[str, Any]] | None: Every drive, or None when storage was
                unread or any one drive failed, so a disk never silently drops out.
        """
        if storage is None:
            return None
        drives: dict[str, dict[str, Any]] = {}
        for member in storage:
            links = member.get("Drives")
            if links is not None and not isinstance(links, list):
                logger.warning(
                    "kubevirt.drive_skipped",
                    system=system.get("Id"),
                    storage=member.get("Id"),
                    path=system.get("@odata.id"),
                    error="Drives is not a list",
                )
                return None
            for link in links or []:
                if not isinstance(link, dict):
                    continue
                try:
                    path = validate_odata_id(link.get("@odata.id"))
                    if path not in drives:
                        drives[path] = await client.get(path)
                except (
                    RedfishForbiddenError,
                    RedfishProtocolError,
                    RedfishUnreachableError,
                ) as exc:
                    logger.warning(
                        "kubevirt.drive_skipped",
                        system=system.get("Id"),
                        storage=member.get("Id"),
                        path=str(link.get("@odata.id"))[:200],
                        error=str(exc),
                    )
                    return None
        return list(drives.values())


def _netloc_of(external_id: str) -> str:
    """
    The facade `host[:port]` inside a `kubevirt://` external id.

    Args:
        external_id (str): An id `_system_path` accepted.

    Returns:
        str: The netloc, possibly `""`.
    """
    netloc, _, _ = external_id.removeprefix(EXTERNAL_ID_SCHEME).partition("/")
    return netloc


def _system_path(external_id: str | None) -> str | None:
    """
    The System path inside a `kubevirt://` external id.

    Args:
        external_id (str | None): The stored id.

    Returns:
        str | None: `/redfish/v1/Systems/<Id>`, or None for a foreign or malformed id.
    """
    if not external_id or not external_id.startswith(EXTERNAL_ID_SCHEME):
        return None
    _, _, path = external_id.removeprefix(EXTERNAL_ID_SCHEME).partition("/")
    path = f"/{path}"
    system_id = path.removeprefix(_SYSTEMS_PREFIX)
    if not path.startswith(_SYSTEMS_PREFIX) or not system_id or "/" in system_id:
        return None
    try:
        return validate_odata_id(path)
    except RedfishProtocolError:
        return None
