"""`app.infrastructure.providers.oneview.provider`.

The orchestration, against a mocked transport: three bulk calls for the
whole appliance, the profile join that decides which hardware is
collected at all, and the one genuinely per-server call — power
supplies — staying bounded and switchable.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.oneview.client import OneViewClient, OneViewConnectionError
from app.infrastructure.providers.oneview.provider import OneViewProvider

pytestmark = pytest.mark.unit

_ENDPOINT = "ov-1.example.net"
_HARDWARE_A = "/rest/server-hardware/a"
_HARDWARE_B = "/rest/server-hardware/b"
_PROFILE_A = {
    "uri": "/rest/server-profiles/a",
    "name": "ocp4-tlv-prod-worker-01",
    "serverProfileTemplateUri": "/rest/server-profile-templates/t",
}
_PROFILE_B = {"uri": "/rest/server-profiles/b", "name": "ocp4-nyc-prod-worker-02"}
_TEMPLATE = {"uri": "/rest/server-profile-templates/t", "name": "worker-template"}


def _manager(endpoint: str = _ENDPOINT) -> Manager:
    """
    The manager projection a run reports under.

    Args:
        endpoint (str): The appliance.

    Returns:
        Manager: The manager document.
    """
    return Manager(
        id="mgr_oneview",
        name="oneview",
        type=ManagerType.ONEVIEW,
        endpoint=endpoint,
        enabled=True,
        audit=AuditFields.new(),
    )


def _hardware(uri: str, *, profile_uri: str | None, name: str = "Encl1, bay 3") -> dict[str, Any]:
    """
    One server-hardware member as `expand=all` reports it.

    Args:
        uri (str): Its canonical URI.
        profile_uri (str | None): The assigned profile, or `None`.
        name (str): OneView's own name — a bay location, deliberately.

    Returns:
        dict[str, Any]: The member.
    """
    return {
        "uri": uri,
        "name": name,
        "serverProfileUri": profile_uri,
        "serialNumber": uri.rsplit("/", 1)[-1].upper(),
        "model": "ProLiant DL380 Gen10",
        "mpModel": "iLO5",
        "memoryMb": 262144,
        "processorCount": 2,
        "processorCoreCount": 16,
        "mpHostInfo": {"mpIpAddresses": [{"address": "10.0.0.1", "type": "Static"}]},
        "subResources": {
            "Devices": {"name": "Devices", "collectionState": "Collected", "data": []}
        },
    }


def _appliance(
    *,
    profiles: list[dict[str, Any]],
    hardware: list[dict[str, Any]],
    templates: list[dict[str, Any]] | None = None,
    power_supplies: httpx.Response | None = None,
    processors: httpx.Response | None = None,
    fail: bool = False,
    seen: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """
    A transport handler for the appliance.

    Args:
        profiles (list[dict[str, Any]]): Its server profiles.
        hardware (list[dict[str, Any]]): Its server hardware.
        templates (list[dict[str, Any]] | None): Its profile templates.
        power_supplies (httpx.Response | None): What every
            `/powerSupplies` call answers. `None` answers 404.
        processors (httpx.Response | None): What every `/processors` call
            answers. `None` answers 404.
        fail (bool): When true, every request is refused at the socket.
        seen (list[httpx.Request] | None): Collects every request made.

    Returns:
        Callable[[httpx.Request], httpx.Response]: The handler.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if fail:
            raise httpx.ConnectError("no route to host")
        path = request.url.path
        if path == "/rest/version":
            return httpx.Response(200, json={"currentVersion": 8000, "minimumVersion": 1})
        if path == "/rest/login-sessions":
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, json={"sessionID": "token"})
        if path.endswith("/powerSupplies"):
            return power_supplies or httpx.Response(404, json={})
        if path.endswith("/processors"):
            return processors or httpx.Response(404, json={})
        members = {
            "/rest/server-profiles": profiles,
            "/rest/server-profile-templates": templates or [],
            "/rest/server-hardware": hardware,
        }.get(path)
        if members is None:
            return httpx.Response(404, json={})
        return httpx.Response(
            200,
            json={
                "start": 0,
                "count": len(members),
                "total": len(members),
                "members": members,
                "uri": path,
                "nextPageUri": None,
            },
        )

    return handle


def _psu_response(state: str = "Ok") -> httpx.Response:
    """
    A `/powerSupplies` subresource envelope.

    Args:
        state (str): `Oem.Hpe.PowerSupplyStatus.State`.

    Returns:
        httpx.Response: A 200 carrying one power supply.
    """
    return httpx.Response(
        200,
        json={
            "collectionState": "Collected",
            "data": {
                "Members": [
                    {
                        "MemberId": "0",
                        "Model": "865414-B21",
                        "PowerCapacityWatts": 800,
                        "Status": {"Health": "OK", "State": "Enabled"},
                        "Oem": {"Hpe": {"PowerSupplyStatus": {"State": state}}},
                    }
                ]
            },
        },
    )


def _processors_response(*totals: int) -> httpx.Response:
    """
    A `/processors` subresource envelope, one socket per given total.

    Args:
        *totals: Each socket's `TotalThreads`.

    Returns:
        httpx.Response: A 200 carrying one row per socket.
    """
    return httpx.Response(
        200,
        json={
            "collectionState": "Collected",
            "data": {
                "Members": [
                    {"Id": str(i), "TotalCores": total // 2, "TotalThreads": total}
                    for i, total in enumerate(totals)
                ]
            },
        },
    )


def _provider(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> OneViewProvider:
    """
    A provider whose client is wired to a scripted transport.

    Args:
        handler (Callable[[httpx.Request], httpx.Response]): The
            appliance's transport handler.
        **kwargs: Overrides for the provider constructor.

    Returns:
        OneViewProvider: The provider under test.
    """
    defaults: dict[str, Any] = {
        "manager": _manager(),
        "credentials": ManagerConnection(
            endpoint=_ENDPOINT, username="collector", password="secret"
        ),
        "timeout_seconds": 5.0,
        # Off by default so every other test measures the cheap path.
        "collect_psus": False,
        "collect_cpu_threads": False,
        "client_factory": lambda: OneViewClient(
            endpoint=_ENDPOINT,
            username="collector",
            password="secret",
            timeout_seconds=5.0,
            transport=httpx.MockTransport(handler),
        ),
    }
    defaults.update(kwargs)
    return OneViewProvider(**defaults)


async def _collect(provider: OneViewProvider) -> list[ProviderServer]:
    """
    Drain a provider.

    Args:
        provider (OneViewProvider): The provider to run.

    Returns:
        list[ProviderServer]: Everything it yielded.
    """
    async with contextlib.aclosing(provider.collect()) as servers:
        return [server async for server in servers]


class TestCollection:
    async def test_the_whole_appliance_costs_three_bulk_calls(self) -> None:
        """`GET /rest/server-hardware` returns the full object per member and
        `expand=all` folds in the subresources, so nothing here is per-server
        (ADR-0022).
        """
        seen: list[httpx.Request] = []
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A, _PROFILE_B],
                templates=[_TEMPLATE],
                hardware=[
                    _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a"),
                    _hardware(_HARDWARE_B, profile_uri="/rest/server-profiles/b"),
                ],
                seen=seen,
            )
        )

        servers = await _collect(provider)

        assert sorted(s.name for s in servers) == [
            "ocp4-nyc-prod-worker-02",
            "ocp4-tlv-prod-worker-01",
        ]
        assert servers[0].manager_id == "mgr_oneview"
        assert [r.url.path for r in seen if r.url.path.startswith("/rest/server-")] == [
            "/rest/server-profiles",
            "/rest/server-profile-templates",
            "/rest/server-hardware",
        ]

    async def test_the_hardware_sweep_asks_for_the_expanded_payload(self) -> None:
        seen: list[httpx.Request] = []
        await _collect(_provider(_appliance(profiles=[], hardware=[], seen=seen)))

        sweep = next(r for r in seen if r.url.path == "/rest/server-hardware")
        assert sweep.url.params["expand"] == "all"

    async def test_the_external_id_and_template_come_through(self) -> None:
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                templates=[_TEMPLATE],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
            )
        )

        servers = await _collect(provider)

        assert servers[0].external_id == _HARDWARE_A
        assert servers[0].profile_template_name == "worker-template"

    async def test_hardware_with_no_profile_is_skipped_and_counted(self) -> None:
        """Unassigned hardware has no operator-assigned name — OneView's
        own is a bay location — so ingesting it would create a server
        that parses to no site and matches no classification rule.
        """
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[
                    _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a"),
                    _hardware("/rest/server-hardware/spare", profile_uri=None),
                ],
            )
        )

        with capture_logs() as events:
            servers = await _collect(provider)

        assert len(servers) == 1
        skipped = next(e for e in events if e["event"] == "oneview.hardware_without_profile")
        assert skipped["servers"] == 1

    async def test_the_name_pattern_filters_on_the_profile_name(self) -> None:
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A, {"uri": "/rest/server-profiles/x", "name": "esx-host-9"}],
                hardware=[
                    _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a"),
                    _hardware("/rest/server-hardware/x", profile_uri="/rest/server-profiles/x"),
                ],
            ),
            name_pattern="^ocp",
        )

        servers = await _collect(provider)

        assert [s.name for s in servers] == ["ocp4-tlv-prod-worker-01"]

    async def test_an_unreachable_appliance_fails_the_run(self) -> None:
        """One appliance, so its failure is the run's failure — there is
        no partial-success accounting to do.
        """
        provider = _provider(_appliance(profiles=[], hardware=[], fail=True))

        with pytest.raises(OneViewConnectionError):
            await _collect(provider)


class TestCollectionErrors:
    """`collection_errors` — before it, `oneview.collection_truncated` logged
    at ERROR and the run still exited 0, indistinguishable from a healthy
    run against a smaller estate. See ADR-0023.
    """

    async def test_a_truncated_page_becomes_a_collection_error(self) -> None:
        """`/rest/server-profiles`' documented 256-per-request ceiling: the
        appliance reports more members than the one page returned, and
        `nextPageUri` is null past it.
        """

        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/rest/version":
                return httpx.Response(200, json={"currentVersion": 8000, "minimumVersion": 1})
            if path == "/rest/login-sessions":
                if request.method == "DELETE":
                    return httpx.Response(204)
                return httpx.Response(200, json={"sessionID": "token"})
            if path == "/rest/server-profiles":
                return httpx.Response(
                    200,
                    json={
                        "start": 0,
                        "count": 1,
                        "total": 300,
                        "members": [_PROFILE_A],
                        "uri": path,
                        "nextPageUri": None,
                    },
                )
            if path == "/rest/server-profile-templates":
                return httpx.Response(
                    200,
                    json={
                        "start": 0,
                        "count": 0,
                        "total": 0,
                        "members": [],
                        "uri": path,
                        "nextPageUri": None,
                    },
                )
            if path == "/rest/server-hardware":
                hardware = [_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")]
                return httpx.Response(
                    200,
                    json={
                        "start": 0,
                        "count": len(hardware),
                        "total": len(hardware),
                        "members": hardware,
                        "uri": path,
                        "nextPageUri": None,
                    },
                )
            return httpx.Response(404, json={})

        provider = _provider(handle)

        servers = await _collect(provider)

        # A paging ceiling still collects what it reached.
        assert len(servers) == 1
        assert provider.collection_errors
        assert "/rest/server-profiles" in provider.collection_errors[0]
        assert "300" in provider.collection_errors[0]

    async def test_a_complete_run_reports_no_errors(self) -> None:
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
            )
        )

        await _collect(provider)

        assert provider.collection_errors == ()


class TestSubresources:
    async def test_subresources_an_ilo4_cannot_report_are_logged_once_aggregated(self) -> None:
        """One line, not per host: on a mixed estate every iLO-4 server
        answers `InsufficientFirmware` for every subresource, and a
        per-host line would bury the run's output.
        """
        old = _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")
        old["mpModel"] = "iLO4"
        old["subResources"]["Devices"]["collectionState"] = "InsufficientFirmware"
        older = _hardware(_HARDWARE_B, profile_uri="/rest/server-profiles/b")
        older["mpModel"] = "iLO4"
        older["subResources"]["Devices"]["collectionState"] = "InsufficientFirmware"
        provider = _provider(_appliance(profiles=[_PROFILE_A, _PROFILE_B], hardware=[old, older]))

        with capture_logs() as events:
            servers = await _collect(provider)

        assert len(servers) == 2
        assert all(s.gpus is None for s in servers)
        unreadable = [e for e in events if e["event"] == "oneview.subresources_unreadable"]
        assert len(unreadable) == 1
        assert unreadable[0]["servers"] == 2
        assert unreadable[0]["by_state_and_generation"] == {"InsufficientFirmware/iLO4": 2}

    async def test_a_fully_collected_appliance_logs_nothing_unreadable(self) -> None:
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
            )
        )

        with capture_logs() as events:
            await _collect(provider)

        assert not [e for e in events if e["event"] == "oneview.subresources_unreadable"]


class TestPowerSupplies:
    async def test_the_per_server_call_populates_psus(self) -> None:
        """The one field no provider in this repo has ever populated,
        while the health engine has carried `power.failed_psu_count` the
        whole time.
        """
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
                power_supplies=_psu_response("Failed"),
            ),
            collect_psus=True,
        )

        servers = await _collect(provider)

        assert servers[0].psus is not None
        assert servers[0].psus[0]["health"] == "DOWN"
        assert servers[0].psus[0]["capacity_watts"] == 800

    async def test_switching_it_off_makes_no_per_server_call_at_all(self) -> None:
        """`INVENTORY_ONEVIEW_COLLECT_PSUS` is the difference between a
        ~15-request sweep and a ~2500-request one.
        """
        seen: list[httpx.Request] = []
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
                power_supplies=_psu_response(),
                seen=seen,
            ),
            collect_psus=False,
        )

        servers = await _collect(provider)

        assert not [r for r in seen if r.url.path.endswith("/powerSupplies")]
        assert servers[0].psus is None

    async def test_a_psu_fetch_that_raises_does_not_abort_the_appliance(self) -> None:
        """One malformed `/powerSupplies` response must not cost every other
        server its PSU data, or the run. `get_json` is monkeypatched to return
        `None` — unreachable through the real client, so defense in depth only.
        """
        hardware_ok = _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")
        hardware_bad = _hardware(_HARDWARE_B, profile_uri="/rest/server-profiles/b")
        appliance = _appliance(
            profiles=[_PROFILE_A, _PROFILE_B],
            hardware=[hardware_ok, hardware_bad],
            power_supplies=_psu_response(),
        )

        def client_factory() -> OneViewClient:
            client = OneViewClient(
                endpoint=_ENDPOINT,
                username="collector",
                password="secret",
                timeout_seconds=5.0,
                transport=httpx.MockTransport(appliance),
            )
            real_get_json = client.get_json

            async def flaky_get_json(path: str, **kwargs: Any) -> Any:
                if path == f"{_HARDWARE_B}/powerSupplies":
                    return None  # violates the real client's own return type
                return await real_get_json(path, **kwargs)

            client.get_json = flaky_get_json  # ty: ignore[invalid-assignment]
            return client

        provider = _provider(appliance, collect_psus=True, client_factory=client_factory)

        with capture_logs() as events:
            servers = await _collect(provider)

        by_uri = {s.external_id: s for s in servers}
        assert by_uri[_HARDWARE_A].psus is not None
        assert by_uri[_HARDWARE_B].psus is None
        unreadable = [e for e in events if e["event"] == "oneview.power_supplies_unreadable"]
        assert unreadable and unreadable[0]["servers"] == 1

    async def test_only_matched_servers_cost_a_call(self) -> None:
        """The name filter runs before the per-server pass, so a
        datacenter of non-`ocp` HPE servers costs nothing.
        """
        seen: list[httpx.Request] = []
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A, {"uri": "/rest/server-profiles/x", "name": "esx-host-9"}],
                hardware=[
                    _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a"),
                    _hardware("/rest/server-hardware/x", profile_uri="/rest/server-profiles/x"),
                ],
                power_supplies=_psu_response(),
                seen=seen,
            ),
            collect_psus=True,
            name_pattern="^ocp",
        )

        await _collect(provider)

        assert [r.url.path for r in seen if r.url.path.endswith("/powerSupplies")] == [
            f"{_HARDWARE_A}/powerSupplies"
        ]

    async def test_an_expanded_payload_that_carries_them_costs_nothing(self) -> None:
        """`/powerSupplies` has no `SubResourceName` value, so whether
        `expand=all` includes it is undetermined in HPE's docs. If it
        does, the per-server call must not happen anyway.
        """
        seen: list[httpx.Request] = []
        member = _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")
        member["subResources"]["PowerSupplies"] = {
            "name": "PowerSupplies",
            "collectionState": "Collected",
            "data": [{"MemberId": "0", "Oem": {"Hpe": {"PowerSupplyStatus": {"State": "Ok"}}}}],
        }
        provider = _provider(
            _appliance(profiles=[_PROFILE_A], hardware=[member], seen=seen), collect_psus=True
        )

        with capture_logs() as events:
            servers = await _collect(provider)

        assert not [r for r in seen if r.url.path.endswith("/powerSupplies")]
        assert servers[0].psus is not None
        source = next(e for e in events if e["event"] == "oneview.power_supply_source")
        assert source["from_expand"] == 1
        assert source["per_server_calls"] == 0

    async def test_a_failed_call_reports_unread_and_never_aborts_the_run(self) -> None:
        """A PSU sub-fetch that 404s must not erase a server's stored
        power supplies, and must not lose the rest of the fleet.
        """
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A, _PROFILE_B],
                hardware=[
                    _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a"),
                    _hardware(_HARDWARE_B, profile_uri="/rest/server-profiles/b"),
                ],
                power_supplies=httpx.Response(404, json={}),
            ),
            collect_psus=True,
        )

        with capture_logs() as events:
            servers = await _collect(provider)

        assert len(servers) == 2
        assert all(s.psus is None for s in servers)
        failed = next(e for e in events if e["event"] == "oneview.power_supplies_unreadable")
        assert failed["servers"] == 2

    async def test_a_non_collected_state_is_unread_not_empty(self) -> None:
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
                power_supplies=httpx.Response(
                    200, json={"collectionState": "InsufficientFirmware", "data": None}
                ),
            ),
            collect_psus=True,
        )

        servers = await _collect(provider)

        assert servers[0].psus is None

    async def test_the_chosen_route_is_logged_with_its_cost(self) -> None:
        """The undocumented gap between the two routes is ~15 requests
        versus ~2500, so which one ran has to be visible.
        """
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
                power_supplies=_psu_response(),
            ),
            collect_psus=True,
        )

        with capture_logs() as events:
            await _collect(provider)

        source = next(e for e in events if e["event"] == "oneview.power_supply_source")
        assert source["from_expand"] == 0
        assert source["per_server_calls"] == 1


class TestProcessors:
    """Same shape as `TestPowerSupplies` — `/processors` is the only
    source `cpu_threads` has, since `server-hardware`'s own fields carry
    `processorCount`/`processorCoreCount` but no thread count.
    """

    async def test_the_per_server_call_populates_cpu_threads(self) -> None:
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
                processors=_processors_response(52, 52),
            ),
            collect_cpu_threads=True,
        )

        servers = await _collect(provider)

        assert servers[0].cpu_threads == 104

    async def test_switching_it_off_makes_no_per_server_call_at_all(self) -> None:
        """`INVENTORY_ONEVIEW_COLLECT_CPU_THREADS` is the same trade
        `INVENTORY_ONEVIEW_COLLECT_PSUS` makes for power supplies.
        """
        seen: list[httpx.Request] = []
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
                processors=_processors_response(52, 52),
                seen=seen,
            ),
            collect_cpu_threads=False,
        )

        servers = await _collect(provider)

        assert not [r for r in seen if r.url.path.endswith("/processors")]
        assert servers[0].cpu_threads is None

    async def test_a_processors_fetch_that_raises_does_not_abort_the_appliance(self) -> None:
        """One malformed `/processors` response must not cost every
        other server its thread count, or the run itself — same
        reasoning as `TestPowerSupplies`'s equivalent test.
        """
        hardware_ok = _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")
        hardware_bad = _hardware(_HARDWARE_B, profile_uri="/rest/server-profiles/b")
        appliance = _appliance(
            profiles=[_PROFILE_A, _PROFILE_B],
            hardware=[hardware_ok, hardware_bad],
            processors=_processors_response(52, 52),
        )

        def client_factory() -> OneViewClient:
            client = OneViewClient(
                endpoint=_ENDPOINT,
                username="collector",
                password="secret",
                timeout_seconds=5.0,
                transport=httpx.MockTransport(appliance),
            )
            real_get_json = client.get_json

            async def flaky_get_json(path: str, **kwargs: Any) -> Any:
                if path == f"{_HARDWARE_B}/processors":
                    return None  # violates the real client's own return type
                return await real_get_json(path, **kwargs)

            client.get_json = flaky_get_json  # ty: ignore[invalid-assignment]
            return client

        provider = _provider(appliance, collect_cpu_threads=True, client_factory=client_factory)

        with capture_logs() as events:
            servers = await _collect(provider)

        by_uri = {s.external_id: s for s in servers}
        assert by_uri[_HARDWARE_A].cpu_threads == 104
        assert by_uri[_HARDWARE_B].cpu_threads is None
        unreadable = [e for e in events if e["event"] == "oneview.processors_unreadable"]
        assert unreadable and unreadable[0]["servers"] == 1

    async def test_only_matched_servers_cost_a_call(self) -> None:
        """The name filter runs before the per-server pass, so a
        datacenter of non-`ocp` HPE servers costs nothing.
        """
        seen: list[httpx.Request] = []
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A, {"uri": "/rest/server-profiles/x", "name": "esx-host-9"}],
                hardware=[
                    _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a"),
                    _hardware("/rest/server-hardware/x", profile_uri="/rest/server-profiles/x"),
                ],
                processors=_processors_response(52, 52),
                seen=seen,
            ),
            collect_cpu_threads=True,
            name_pattern="^ocp",
        )

        await _collect(provider)

        assert [r.url.path for r in seen if r.url.path.endswith("/processors")] == [
            f"{_HARDWARE_A}/processors"
        ]

    async def test_an_expanded_payload_that_carries_them_costs_nothing(self) -> None:
        """`/processors` has no `SubResourceName` value either, so
        whether `expand=all` includes it is undetermined in HPE's docs.
        If it does, the per-server call must not happen anyway.
        """
        seen: list[httpx.Request] = []
        member = _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")
        member["subResources"]["Processors"] = {
            "name": "Processors",
            "collectionState": "Collected",
            "data": [
                {"Id": "0", "TotalCores": 26, "TotalThreads": 52},
                {"Id": "1", "TotalCores": 26, "TotalThreads": 52},
            ],
        }
        provider = _provider(
            _appliance(profiles=[_PROFILE_A], hardware=[member], seen=seen),
            collect_cpu_threads=True,
        )

        with capture_logs() as events:
            servers = await _collect(provider)

        assert not [r for r in seen if r.url.path.endswith("/processors")]
        assert servers[0].cpu_threads == 104
        source = next(e for e in events if e["event"] == "oneview.processor_source")
        assert source["from_expand"] == 1
        assert source["per_server_calls"] == 0

    async def test_a_failed_call_reports_unread_and_never_aborts_the_run(self) -> None:
        """A processors sub-fetch that 404s must not erase a server's
        stored thread count, and must not lose the rest of the fleet.
        """
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A, _PROFILE_B],
                hardware=[
                    _hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a"),
                    _hardware(_HARDWARE_B, profile_uri="/rest/server-profiles/b"),
                ],
                processors=httpx.Response(404, json={}),
            ),
            collect_cpu_threads=True,
        )

        with capture_logs() as events:
            servers = await _collect(provider)

        assert len(servers) == 2
        assert all(s.cpu_threads is None for s in servers)
        failed = next(e for e in events if e["event"] == "oneview.processors_unreadable")
        assert failed["servers"] == 2

    async def test_a_non_collected_state_is_unread_not_zero(self) -> None:
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
                processors=httpx.Response(
                    200, json={"collectionState": "InsufficientFirmware", "data": None}
                ),
            ),
            collect_cpu_threads=True,
        )

        servers = await _collect(provider)

        assert servers[0].cpu_threads is None

    async def test_the_chosen_route_is_logged_with_its_cost(self) -> None:
        provider = _provider(
            _appliance(
                profiles=[_PROFILE_A],
                hardware=[_hardware(_HARDWARE_A, profile_uri="/rest/server-profiles/a")],
                processors=_processors_response(52, 52),
            ),
            collect_cpu_threads=True,
        )

        with capture_logs() as events:
            await _collect(provider)

        source = next(e for e in events if e["event"] == "oneview.processor_source")
        assert source["from_expand"] == 0
        assert source["per_server_calls"] == 1


class TestConfiguration:
    def test_a_manager_with_no_endpoint_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no endpoint"):
            OneViewProvider(
                manager=_manager(""),
                credentials=ManagerConnection(endpoint="", username="u", password="p"),
                timeout_seconds=5.0,
            )

    def test_the_provider_type_is_the_manager_type(self) -> None:
        assert OneViewProvider.provider_type == ManagerType.ONEVIEW.value

    async def test_health_check_logs_in_and_out(self) -> None:
        seen: list[httpx.Request] = []
        await _provider(_appliance(profiles=[], hardware=[], seen=seen)).health_check()

        methods = [(r.method, r.url.path) for r in seen]
        assert ("POST", "/rest/login-sessions") in methods
        assert ("DELETE", "/rest/login-sessions") in methods

    async def test_health_check_surfaces_an_unreachable_appliance(self) -> None:
        with pytest.raises(OneViewConnectionError):
            await _provider(_appliance(profiles=[], hardware=[], fail=True)).health_check()
