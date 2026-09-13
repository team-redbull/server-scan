"""`tools.run_collector` — the collector CLI's own logic.

Everything covered here is pure decision-making (which provider a manager
maps to, what happens when one is missing or misconfigured, whether one
bad manager takes down the rest of the run) and needs neither MongoDB nor
a vendor endpoint.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest
from tools import run_collector
from tools.run_collector import (
    _build_provider,
    _dry_run_one_manager,
    _filtered,
    _format_duration,
    _format_speed,
    _parse_args,
    _run,
    _run_one_manager,
    resolve_name_pattern,
)

from app.application.services.ingest import IngestSummary
from app.config.settings import Settings
from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection, ManagerNotConfiguredError
from app.domain.ports.provider import (
    ProviderAttachment,
    ProviderNic,
    ProviderServer,
    ServerInventoryProvider,
)
from app.infrastructure.providers.oneview.provider import OneViewProvider
from app.infrastructure.providers.openmanage.provider import OpenManageProvider
from app.infrastructure.providers.ucs_central.provider import UcsCentralProvider

pytestmark = pytest.mark.unit


def _settings(**overrides: Any) -> Settings:
    """Settings built from explicit values only — `_env_file=None` keeps a
    developer's real `.env` credentials from deciding which collector is built.
    """
    return Settings(_env_file=None, **overrides)


def _central_settings(**overrides: Any) -> Settings:
    """Settings with the fleet-wide UCS Manager login the Central collector needs."""
    return _settings(
        ucs_manager_username="domain-admin",
        ucs_manager_password="domain-secret",
        **overrides,
    )


def _central_settings_for_run() -> Settings:
    """Everything `_run` needs to reach the exit-code decision."""
    return _central_settings(
        ucs_central_ip="central.lab.example.com",
        ucs_central_username="central-admin",
        ucs_central_password="central-secret",
    )


def _manager(**overrides: Any) -> Manager:
    """A `Manager`, UCS Central by default."""
    defaults: dict[str, Any] = {
        "_id": "mgr-1",
        "name": "ucs-central-lab",
        "type": ManagerType.UCS_CENTRAL,
        "site_id": "site-1",
        "endpoint": "central.lab.example.com",
        "audit": AuditFields.new(),
    }
    defaults.update(overrides)
    return Manager(**defaults)


class FakeCredentialResolver:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.resolved: list[ManagerType] = []

    def resolve(self, manager_type: ManagerType) -> ManagerConnection:
        self.resolved.append(manager_type)
        if self._error is not None:
            raise self._error
        return ManagerConnection(
            endpoint="ucsm.lab.example.com", username="admin", password="secret"
        )


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.4, "0.4s"),
            (59.9, "59.9s"),
            (60.0, "1m 0s"),
            (133.4, "2m 13s"),
        ],
    )
    def test_formats_at_and_around_the_minute_boundary(self, seconds: float, expected: str) -> None:
        assert _format_duration(seconds) == expected


class TestFormatSpeed:
    """The 1000 Mbps boundary is the whole logic: below it, plain Mbps;
    at or above it, Gbps, with `:g` dropping a trailing `.0` but keeping
    a real fraction."""

    @pytest.mark.parametrize(
        ("speed_mbps", "expected"),
        [
            (100, "100 Mbps"),
            (999, "999 Mbps"),
            (1000, "1 Gbps"),
            (2500, "2.5 Gbps"),
            (25000, "25 Gbps"),
            (100000, "100 Gbps"),
        ],
    )
    def test_formats_at_and_around_the_gbps_boundary(self, speed_mbps: int, expected: str) -> None:
        assert _format_speed(speed_mbps) == expected


class TestBuildProvider:
    async def test_builds_a_provider_for_oneview(self) -> None:
        """The HPE entry point: one appliance, one login, no BMC login at all
        (docs/adr/0022-oneview-only-hpe-collector.md).
        """
        resolver = FakeCredentialResolver()
        provider = _build_provider(
            _manager(type=ManagerType.ONEVIEW, endpoint="ov-1.example.net"),
            credential_resolver=resolver,
            timeout_seconds=5.0,
            settings=_settings(),
        )
        assert provider.provider_type == ManagerType.ONEVIEW.value
        assert resolver.resolved == [ManagerType.ONEVIEW]

    async def test_builds_a_provider_for_openmanage(self) -> None:
        """The Dell entry point: one OME login plus a BMC login, since hardware
        is read from each iDRAC (docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md).
        """
        resolver = FakeCredentialResolver()
        provider = _build_provider(
            _manager(type=ManagerType.OPENMANAGE),
            credential_resolver=resolver,
            timeout_seconds=5.0,
            settings=_settings(ome_bmc_username="bmc-admin", ome_bmc_password="bmc-secret"),
        )
        assert provider.provider_type == ManagerType.OPENMANAGE.value
        assert resolver.resolved == [ManagerType.OPENMANAGE]

    async def test_openmanage_without_a_bmc_login_is_rejected_before_connecting(self) -> None:
        with pytest.raises(ManagerNotConfiguredError) as excinfo:
            _build_provider(
                _manager(type=ManagerType.OPENMANAGE),
                credential_resolver=FakeCredentialResolver(),
                timeout_seconds=5.0,
                settings=_settings(),
            )
        message = str(excinfo.value)
        assert "INVENTORY_OME_BMC_USERNAME" in message
        assert "INVENTORY_OME_BMC_PASSWORD" in message

    async def test_builds_a_provider_for_ucs_central(self) -> None:
        """The one Cisco entry point. `settings` is pinned explicitly because the
        collector reads its domain login and name pattern from there.
        """
        resolver = FakeCredentialResolver()
        provider = _build_provider(
            _manager(),
            credential_resolver=resolver,
            timeout_seconds=5.0,
            settings=_central_settings(),
        )
        assert provider.provider_type == ManagerType.UCS_CENTRAL.value
        assert resolver.resolved == [ManagerType.UCS_CENTRAL]

    async def test_pointing_the_tool_at_ucs_manager_says_use_ucs_central(self) -> None:
        """`UcsManagerProvider` still exists as the engine `UCS_CENTRAL` drives per
        domain, so the message must name the replacement, not "not implemented".
        """
        with pytest.raises(NotImplementedError) as excinfo:
            _build_provider(
                _manager(type=ManagerType.UCS_MANAGER),
                credential_resolver=FakeCredentialResolver(),
                timeout_seconds=5.0,
                settings=_central_settings(),
            )
        message = str(excinfo.value)
        assert "No collector implemented" not in message
        assert "--manager-type UCS_CENTRAL" in message
        assert "INVENTORY_UCS_MANAGER_USERNAME" in message

    async def test_without_a_domain_login_the_variables_are_named(self) -> None:
        """A half-configured vendor names the variables to set, before any
        connection, rather than failing a login as "bad credentials".
        """
        with pytest.raises(ManagerNotConfiguredError) as excinfo:
            _build_provider(
                _manager(),
                credential_resolver=FakeCredentialResolver(),
                timeout_seconds=5.0,
                settings=_settings(),
            )
        message = str(excinfo.value)
        assert "INVENTORY_UCS_MANAGER_USERNAME" in message
        assert "INVENTORY_UCS_MANAGER_PASSWORD" in message
        # The IP is genuinely not needed, so demanding it would send the
        # operator to invent a value that is never used.
        assert "INVENTORY_UCS_MANAGER_IP" not in message

    async def test_unconfigured_manager_type_is_rejected_before_connecting(self) -> None:
        resolver = FakeCredentialResolver(error=ManagerNotConfiguredError("not configured"))
        with pytest.raises(ManagerNotConfiguredError):
            _build_provider(_manager(), credential_resolver=resolver, timeout_seconds=5.0)

    async def test_missing_endpoint_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no endpoint"):
            _build_provider(
                _manager(endpoint=None),
                credential_resolver=FakeCredentialResolver(),
                timeout_seconds=5.0,
                settings=_central_settings(),
            )


class FakeIngestService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.ingested = 0
        self.managers: list[Any] = []

    async def ingest(self, provider: Any, *, managers: Any = (), sites: Any = ()) -> Any:
        self.ingested += 1
        self.managers = list(managers)
        if self._error is not None:
            raise self._error
        return "summary"


class TestRunOneManager:
    async def test_returns_the_ingest_summary(self) -> None:
        ingest = FakeIngestService()
        result = await _run_one_manager(
            _manager(),
            ingest_service=ingest,  # ty: ignore[invalid-argument-type]
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            settings=_central_settings(),
        )
        assert result is not None
        assert result.summary == "summary"

    async def test_carries_the_providers_collection_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The summary counts cannot express a partial run — a domain that
        failed contributes no servers and no ingest errors — so the outcome
        has to carry them separately or exit code 3 has nothing to fire on.
        """

        class PartiallyFailedProvider:
            """Duck-typed on purpose: `FakeIngestService` never drives `collect()`,
            so a plain `collection_errors` attribute is the honest double.
            """

            provider_type = "UCS_CENTRAL"
            collection_errors = ("domain 'b' (10.0.0.2) failed: bad credentials",)

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                return
                yield

        monkeypatch.setattr(run_collector, "_build_provider", _factory(PartiallyFailedProvider()))
        result = await _run_one_manager(
            _manager(),
            ingest_service=FakeIngestService(),  # ty: ignore[invalid-argument-type]
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            settings=_central_settings(),
        )
        assert result is not None
        assert result.collection_errors == ("domain 'b' (10.0.0.2) failed: bad credentials",)

    async def test_upserts_the_manager_projection(self) -> None:
        """`IngestService` only writes managers it is handed, so omitting
        `managers=` left every collected server pointing at a
        `manager_id` no document had — see docs/adr/0016.
        """
        ingest = FakeIngestService()
        manager = _manager()
        await _run_one_manager(
            manager,
            ingest_service=ingest,  # ty: ignore[invalid-argument-type]
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            settings=_central_settings(),
        )
        assert [m.id for m in ingest.managers] == [manager.id]

    async def test_does_not_health_check_separately_from_ingest(self) -> None:
        """`IngestService.ingest()` health-checks as its first step, and a
        UCS login is ~4 round trips — collecting the health check here too
        would double that and burn a second session per manager.
        """
        calls: list[str] = []

        class RecordingIngest(FakeIngestService):
            async def ingest(self, provider: Any, *, managers: Any = (), sites: Any = ()) -> Any:
                calls.append("ingest")
                return "summary"

        provider_health_checks: list[str] = []

        async def _fail_if_called() -> None:
            provider_health_checks.append("health_check")

        ingest = RecordingIngest()
        manager = _manager()
        resolver = FakeCredentialResolver()
        provider = _build_provider(
            manager,
            credential_resolver=resolver,
            timeout_seconds=5.0,
            settings=_central_settings(),
        )
        provider.health_check = _fail_if_called  # ty: ignore[invalid-assignment]

        await _run_one_manager(
            manager,
            ingest_service=ingest,  # ty: ignore[invalid-argument-type]
            credential_resolver=resolver,
            timeout_seconds=5.0,
            settings=_central_settings(),
        )
        assert calls == ["ingest"]
        assert provider_health_checks == []

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("unreachable"), ManagerNotConfiguredError("not set"), ValueError("bad")],
    )
    async def test_a_failing_manager_is_isolated_not_propagated(self, error: Exception) -> None:
        """One flaky domain must not abort the run for the other managers
        of the same type — the caller distinguishes failure by `None`.
        """
        result = await _run_one_manager(
            _manager(),
            ingest_service=FakeIngestService(error=error),  # ty: ignore[invalid-argument-type]
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
        )
        assert result is None

    async def test_a_manager_type_with_no_entry_point_is_reported_as_a_failure(self) -> None:
        """`UCS_MANAGER` deliberately has no entry point; pointing the tool at it
        must fail loudly rather than look like a manager with zero servers.
        """
        result = await _run_one_manager(
            _manager(type=ManagerType.UCS_MANAGER),
            ingest_service=FakeIngestService(),  # ty: ignore[invalid-argument-type]
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
        )
        assert result is None


def _factory(provider: Any) -> Any:
    """A stand-in for `_build_provider` that hands back a ready-made
    provider, so the dry-run path can be tested without a UCS domain."""

    def build(_manager: Any, **_kwargs: Any) -> Any:
        return provider

    return build


class TestDryRun:
    async def test_dry_run_reports_servers_without_ingesting(self, capsys: Any) -> None:
        class FakeProvider(ServerInventoryProvider):
            provider_type = "UCS_MANAGER"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                for name in ("ocp4-prod-tlv-infra-01", "ocp4-hypershift-five-01"):
                    yield ProviderServer(external_id=f"dn/{name}", vendor="cisco", name=name)

        count = await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=None,
            provider_factory=_factory(FakeProvider()),
        )
        assert count == 2
        out = capsys.readouterr().out
        # The site is derived at ingest, so the dry run has to show it.
        assert "ocp4-prod-tlv-infra-01" in out
        assert "tlv" in out
        assert "five" in out
        assert "Nothing was written" in out

    async def test_dry_run_falls_back_to_the_org_dn_for_the_site(self, capsys: Any) -> None:
        class FakeProvider(ServerInventoryProvider):
            provider_type = "UCS_CENTRAL"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                yield ProviderServer(
                    external_id="compute/sys-1/blade-1",
                    vendor="cisco",
                    name="blade-1",
                    profile_dn="org-root/org_bat-yam/ls-worker-01",
                )

        await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=None,
            provider_factory=_factory(FakeProvider()),
        )
        assert "bat-yam" in capsys.readouterr().out

    async def test_dry_run_shows_gpu_detail(self, capsys: Any) -> None:
        """The dry-run print has to show every field a collector reports,
        not just count them — this is the one place a naming/data problem
        is visible before a write.
        """

        class FakeProvider(ServerInventoryProvider):
            provider_type = "REDFISH_STANDALONE"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                yield ProviderServer(
                    external_id="redfish://10.0.0.5/redfish/v1/Systems/1",
                    vendor="standalone",
                    name="dgx-h100-01",
                    gpus=(
                        {
                            "vendor": "Nvidia(R) Corporation",
                            "model": "H100",
                            "serial": "GPU-ABC123",
                            "memory_bytes": 80 * 1024**3,
                            "memory_type": "HBM3",
                            "ecc_mode_enabled": True,
                            "correctable_error_count": 2,
                            "uncorrectable_error_count": 0,
                            "temperature_celsius": 58.0,
                            "power_watts": 350.0,
                            "health": "HEALTHY",
                            "health_detail": "OK",
                            "pci_address": None,
                            "firmware_version": "96.00.5E.00.02",
                        },
                    ),
                )

        await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=None,
            provider_factory=_factory(FakeProvider()),
        )
        out = capsys.readouterr().out
        assert "gpus        : 1" in out
        assert "gpu H100  vendor=Nvidia(R) Corporation  serial=GPU-ABC123" in out
        assert "HBM3" in out
        assert "ecc=True" in out
        assert "errors=2c/0u" in out
        assert "temp=58°C" in out
        assert "power=350W" in out
        assert "health=HEALTHY (OK)" in out

    async def test_dry_run_shows_drive_health_detail(self, capsys: Any) -> None:
        class FakeProvider(ServerInventoryProvider):
            provider_type = "UCS_CENTRAL"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                yield ProviderServer(
                    external_id="sys/rack-unit-3",
                    vendor="cisco",
                    name="rack-3",
                    storage_drives=(
                        {
                            "id": "sys/rack-unit-3/board/storage-SAS-1/disk-1",
                            "model": "UCS-HD12TB10K12G",
                            "serial": "S3X0ABCD",
                            "media_type": "HDD",
                            "capacity_bytes": 12_000_000_000_000,
                            "health": "CRITICAL",
                            "health_detail": "self-test-failed",
                        },
                    ),
                )

        await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=None,
            provider_factory=_factory(FakeProvider()),
        )
        out = capsys.readouterr().out
        assert "health=CRITICAL (self-test-failed)" in out

    async def test_dry_run_shows_psu_detail(self, capsys: Any) -> None:
        class FakeProvider(ServerInventoryProvider):
            provider_type = "INTERSIGHT"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                yield ProviderServer(
                    external_id="intersight/moid1",
                    vendor="cisco",
                    name="rack-01",
                    psus=(
                        {
                            "id": "1",
                            "model": "PSU-750W",
                            "serial": "PSU-1",
                            "health": "UP",
                            "health_detail": "operable",
                            "capacity_watts": 750,
                        },
                        {
                            "id": "2",
                            "model": "PSU-750W",
                            "serial": "PSU-2",
                            "health": "DOWN",
                            "capacity_watts": 750,
                        },
                    ),
                )

        await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=None,
            provider_factory=_factory(FakeProvider()),
        )
        out = capsys.readouterr().out
        assert "psus        : 2" in out
        # health_detail (the raw vendor state) prints in parens after health=;
        # a PSU without one dashes like every other unread field here.
        assert "psu 1  PSU-750W  serial=PSU-1  750W  health=UP (operable)  power=—" in out
        assert "psu 2  PSU-750W  serial=PSU-2  750W  health=DOWN (—)  power=—" in out

    async def test_dry_run_shows_the_raw_ucs_power_field_alongside_oper_state(
        self, capsys: Any
    ) -> None:
        """`equipmentPsu.power` is collected beside `oper_state`, not reduced into
        it (docs/cisco-collectors.md, "Power supplies (PSUs)"); Intersight prints "—".
        """

        class FakeProvider(ServerInventoryProvider):
            provider_type = "UCS_CENTRAL"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                yield ProviderServer(
                    external_id="sys/rack-unit-3",
                    vendor="cisco",
                    name="rack-3",
                    psus=(
                        {
                            "id": "1",
                            "model": "UCSC-PSU1-1050W",
                            "serial": "LIT1",
                            "health": "UP",
                            "capacity_watts": 1050,
                            "oper_power": "ok",
                        },
                    ),
                )

        await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=None,
            provider_factory=_factory(FakeProvider()),
        )
        out = capsys.readouterr().out
        assert "health=UP (—)  power=ok" in out

    async def test_dry_run_hides_fi_identity_on_a_vnic_attachment(self, capsys: Any) -> None:
        """A vNIC never carries a fabric relationship (docs/cisco-collectors.md,
        "PHYSICAL versus VNIC"), so an FI line on it would read as missing data.
        """

        class FakeProvider(ServerInventoryProvider):
            provider_type = "INTERSIGHT"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                yield ProviderServer(
                    external_id="intersight/moid1",
                    vendor="cisco",
                    name="standalone-01",
                    attachments=(
                        ProviderAttachment(
                            type="FABRIC_INTERCONNECT",
                            provider="INTERSIGHT",
                            fabric=None,
                            fabric_name=None,
                            fabric_id=None,
                            fabric_model=None,
                            fabric_serial=None,
                            server_interface="eth0",
                            server_port=None,
                            fabric_port=None,
                            admin_state="UP",
                            oper_state="UP",
                            speed_mbps=None,
                            interface_kind="VNIC",
                        ),
                    ),
                )

        await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=None,
            provider_factory=_factory(FakeProvider()),
        )
        out = capsys.readouterr().out
        assert "[VNIC" in out
        assert "if=eth0" in out
        assert "admin=UP oper=UP" in out
        assert "fabric" not in out
        assert "FI model/serial" not in out

    async def test_dry_run_shows_the_fabric_cluster_name_on_a_physical_attachment(
        self, capsys: Any
    ) -> None:
        """`fabric_name` (`topSystem.name`, ADR-0009) prints after `fabric {A|B}`:
        the letter tells a domain's two sides apart, the name tells domains apart.
        """

        class FakeProvider(ServerInventoryProvider):
            provider_type = "UCS_CENTRAL"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                yield ProviderServer(
                    external_id="sys/rack-unit-3",
                    vendor="cisco",
                    name="rack-3",
                    attachments=(
                        ProviderAttachment(
                            type="FABRIC_INTERCONNECT",
                            provider="UCS_CENTRAL",
                            fabric="A",
                            fabric_name="myc-03",
                            fabric_id=None,
                            fabric_model="UCS-FI-6454",
                            fabric_serial="FCH2222A",
                            server_interface="eth0",
                            server_port=None,
                            fabric_port=None,
                            admin_state="ENABLED",
                            oper_state="UP",
                            speed_mbps=None,
                            interface_kind="PHYSICAL",
                        ),
                    ),
                )

        await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=None,
            provider_factory=_factory(FakeProvider()),
        )
        out = capsys.readouterr().out
        assert "fabric A  (myc-03)" in out

    async def test_dry_run_shows_nic_speed_in_gbps_and_dashes_when_unread(
        self, capsys: Any
    ) -> None:
        class FakeProvider(ServerInventoryProvider):
            provider_type = "REDFISH_STANDALONE"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                yield ProviderServer(
                    external_id="redfish://bmc-1/redfish/v1/Systems/1",
                    vendor="dell",
                    name="standalone-01",
                    nics=(
                        ProviderNic(
                            name="NIC.Integrated.1-1-1",
                            mac="aa:bb:cc:dd:ee:01",
                            speed_mbps=25000,
                            link_state="UP",
                            location="NIC.Integrated.1-1-1",
                        ),
                        ProviderNic(
                            name="NIC.Integrated.1-2-1",
                            mac="aa:bb:cc:dd:ee:02",
                            speed_mbps=None,
                            link_state="UNKNOWN",
                            location="NIC.Integrated.1-2-1",
                        ),
                    ),
                )

        await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=None,
            provider_factory=_factory(FakeProvider()),
        )
        out = capsys.readouterr().out
        assert "25 Gbps" in out
        assert "25000mbps" not in out
        assert "UNKNOWN  —" in out

    async def test_dry_run_respects_limit(self, capsys: Any) -> None:
        class FakeProvider(ServerInventoryProvider):
            provider_type = "UCS_MANAGER"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                for i in range(10):
                    yield ProviderServer(external_id=f"dn/{i}", vendor="cisco", name=f"srv-{i}")

        count = await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=3,
            provider_factory=_factory(FakeProvider()),
        )
        assert count == 3
        assert "stopped at --limit 3" in capsys.readouterr().out

    async def test_dry_run_limit_does_not_pull_one_past_it(self, capsys: Any) -> None:
        """`async for` fetches the next item before the body runs, so the limit
        must be checked after processing the Nth item or the (N+1)th is requested.
        """
        requested: list[int] = []

        class FakeProvider(ServerInventoryProvider):
            provider_type = "UCS_MANAGER"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                for i in range(10):
                    requested.append(i)
                    yield ProviderServer(external_id=f"dn/{i}", vendor="cisco", name=f"srv-{i}")

        count = await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=3,
            provider_factory=_factory(FakeProvider()),
        )
        assert count == 3
        assert requested == [0, 1, 2]

    async def test_dry_run_limit_zero_collects_nothing(self, capsys: Any) -> None:
        """The one shape the "check after processing" fix above cannot
        cover on its own — `limit=0` means never entering the loop, and
        therefore the provider, at all.
        """
        requested: list[int] = []

        class FakeProvider(ServerInventoryProvider):
            provider_type = "UCS_MANAGER"

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                requested.append(0)
                yield ProviderServer(external_id="dn/0", vendor="cisco", name="srv-0")

        count = await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=0,
            provider_factory=_factory(FakeProvider()),
        )
        assert count == 0
        assert requested == []
        assert "stopped at --limit 0" in capsys.readouterr().out

    async def test_dry_run_limit_still_closes_the_inner_provider(self) -> None:
        class FakeProvider(ServerInventoryProvider):
            provider_type = "UCS_MANAGER"

            def __init__(self) -> None:
                super().__init__()
                self.torn_down = False

            async def health_check(self) -> None:
                return None

            async def _list_servers(self) -> Any:
                try:
                    for i in range(3):
                        yield ProviderServer(external_id=f"dn/{i}", vendor="cisco", name=f"srv-{i}")
                finally:
                    self.torn_down = True

        provider = FakeProvider()
        await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=1,
            provider_factory=_factory(provider),
        )
        assert provider.torn_down


class TestResolveNamePattern:
    """`resolve_name_pattern` — the one place the global, each collector's
    override and `_UNFILTERED_TYPES` are reconciled (CLAUDE.md, name pattern).
    """

    def test_a_collector_with_no_override_inherits_the_global(self) -> None:
        settings = _settings(collector_name_pattern="^ocp")
        for manager_type in (
            ManagerType.UCS_CENTRAL,
            ManagerType.INTERSIGHT,
            ManagerType.OPENMANAGE,
            ManagerType.ONEVIEW,
        ):
            assert resolve_name_pattern(manager_type, settings) == "^ocp"

    def test_an_override_wins_over_the_global(self) -> None:
        settings = _settings(collector_name_pattern="^ocp", oneview_name_pattern="^hpe")
        assert resolve_name_pattern(ManagerType.ONEVIEW, settings) == "^hpe"
        assert resolve_name_pattern(ManagerType.INTERSIGHT, settings) == "^ocp"

    def test_an_explicitly_empty_override_opts_out_of_a_nonempty_global(self) -> None:
        """The reason the overrides are optional rather than plain `str`:
        `None` inherits, and `""` is the only way to say "collect
        everything here" while the global still filters everyone else.
        """
        settings = _settings(collector_name_pattern="^ocp", ome_name_pattern="")
        assert resolve_name_pattern(ManagerType.OPENMANAGE, settings) == ""
        assert resolve_name_pattern(ManagerType.UCS_CENTRAL, settings) == "^ocp"

    def test_redfish_standalone_is_exempt_from_the_global(self) -> None:
        """A BMC does not know the server's `ocp4-...` name, so applying
        `^ocp` there would discard every host the operator listed.
        """
        settings = _settings(collector_name_pattern="^ocp")
        assert resolve_name_pattern(ManagerType.REDFISH_STANDALONE, settings) == ""

    def test_an_explicit_override_beats_the_redfish_exemption(self) -> None:
        """The exemption suppresses the *global*, not an operator who has
        asked for a filter on this collector by name.
        """
        settings = _settings(collector_name_pattern="^ocp", redfish_name_pattern="^lab")
        assert resolve_name_pattern(ManagerType.REDFISH_STANDALONE, settings) == "^lab"


class TestOverridesReachTheInnerPruningGates:
    """OME, UCS Central and OneView prune on the pattern before `_NameFilteredProvider`
    sees a server; a factory reading `Settings` itself would prune on the global
    and filter on the override, silently collecting the intersection.
    """

    def test_ucs_central_prunes_domains_on_the_override(self) -> None:
        provider = _build_provider(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            settings=_central_settings(
                collector_name_pattern="^ocp", ucs_central_name_pattern="^cisco"
            ),
        )
        assert isinstance(provider, UcsCentralProvider)
        assert provider._name_pattern == "^cisco"

    def test_oneview_gates_its_per_server_calls_on_the_override(self) -> None:
        provider = _build_provider(
            _manager(type=ManagerType.ONEVIEW, endpoint="ov-1.example.net"),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            settings=_settings(collector_name_pattern="^ocp", oneview_name_pattern="^hpe"),
        )
        assert isinstance(provider, OneViewProvider)
        assert provider._pattern is not None
        assert provider._pattern.pattern == "^hpe"

    def test_openmanage_prunes_bmcs_on_the_override(self) -> None:
        provider = _build_provider(
            _manager(type=ManagerType.OPENMANAGE),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            settings=_settings(
                collector_name_pattern="^ocp",
                ome_name_pattern="^dell",
                ome_bmc_username="bmc-admin",
                ome_bmc_password="bmc-secret",
            ),
        )
        assert isinstance(provider, OpenManageProvider)
        assert provider._pattern is not None
        assert provider._pattern.pattern == "^dell"

    def test_an_empty_override_leaves_the_gate_unfiltered(self) -> None:
        """`""` compiles to no pruning at all, not to an empty regex that
        matches everything by accident — same distinction `_filtered`
        makes.
        """
        provider = _build_provider(
            _manager(type=ManagerType.ONEVIEW, endpoint="ov-1.example.net"),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            settings=_settings(collector_name_pattern="^ocp", oneview_name_pattern=""),
        )
        assert isinstance(provider, OneViewProvider)
        assert provider._pattern is None


class TestNameFilter:
    class _Fake(ServerInventoryProvider):
        provider_type = "UCS_MANAGER"

        def __init__(self, *names: str, error: str | None = None) -> None:
            super().__init__()
            self._names = names
            self._error = error
            self.health_checked = 0
            # Set in `_list_servers`'s `finally`, so a test can tell a closed
            # provider apart from one abandoned mid-collection.
            self.torn_down = False

        async def health_check(self) -> None:
            self.health_checked += 1

        async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
            try:
                for name in self._names:
                    yield ProviderServer(external_id=f"dn/{name}", vendor="cisco", name=name)
                if self._error is not None:
                    self._record_error(self._error)
            finally:
                self.torn_down = True

    async def _names_through(self, pattern: str, *names: str) -> list[str]:
        provider = _filtered(self._Fake(*names), pattern)
        return [ps.name async for ps in provider.collect()]

    async def test_keeps_only_matching_servers(self) -> None:
        kept = await self._names_through(
            "^ocp",
            "ocp4-prod-tlv-infra-01",
            "vmhost-two-14",
            "ocp4-hypershift-five-01",
            "db-prod-03",
        )
        assert kept == ["ocp4-prod-tlv-infra-01", "ocp4-hypershift-five-01"]

    async def test_the_anchor_is_the_operators_to_write(self) -> None:
        """`re.search`, not `re.match`: `^ocp` means "starts with" and an
        unanchored pattern stays a substring match the operator asked for.
        """
        assert await self._names_through("^ocp", "legacy-ocp-gateway-01") == []
        assert await self._names_through("ocp", "legacy-ocp-gateway-01") == [
            "legacy-ocp-gateway-01"
        ]

    async def test_an_empty_pattern_collects_everything(self) -> None:
        """Not "matches nothing" — an empty regex matches every string,
        but `_filtered` doesn't even wrap, so the default is unambiguously
        "no filter" rather than an accidental empty inventory.
        """
        fake = self._Fake("srv-1", "srv-2")
        assert _filtered(fake, "") is fake

    async def test_health_check_still_reaches_the_real_provider(self) -> None:
        fake = self._Fake()
        await _filtered(fake, "^ocp").health_check()
        assert fake.health_checked == 1

    async def test_stopping_early_still_closes_the_inner_provider(self) -> None:
        """`GeneratorExit` at the wrapper's own `yield` used to leave the inner
        generator (and its session logout) to the asyncgen finalizer, not "now".
        """
        fake = self._Fake("ocp-1", "ocp-2", "ocp-3")
        async with contextlib.aclosing(_filtered(fake, "^ocp").collect()) as servers:
            async for _ in servers:
                break

        assert fake.torn_down

    async def test_dry_run_shows_only_what_a_real_run_would_write(self, capsys: Any) -> None:
        """--dry-run bypasses `IngestService` on purpose, so the filter
        has to live on the provider side or a dry run would print servers
        a real run silently drops.
        """
        count = await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),
            timeout_seconds=5.0,
            limit=None,
            name_pattern="^ocp",
            provider_factory=_factory(self._Fake("ocp4-prod-tlv-infra-01", "vmhost-two-14")),
        )
        assert count == 1
        out = capsys.readouterr().out
        assert "ocp4-prod-tlv-infra-01" in out
        assert "vmhost-two-14" not in out
        assert "^ocp" in out


class TestNameFilteredProviderCollectionErrors:
    """`_NameFilteredProvider.collection_errors` must delegate to the wrapped
    provider: the wrapper never calls `_record_error`, so the inherited
    bookkeeping would read back empty and every real run would look complete.
    """

    async def test_delegates_to_the_inner_providers_own_errors(self) -> None:
        inner = TestNameFilter._Fake("ocp4-prod-tlv-infra-01", error="domain 'b' (10.0.0.2) failed")
        wrapped = _filtered(inner, "^ocp")

        async for _ in wrapped.collect():
            pass

        assert wrapped.collection_errors == ("domain 'b' (10.0.0.2) failed",)

    async def test_a_provider_with_no_failure_reports_none(self) -> None:
        wrapped = _filtered(TestNameFilter._Fake("ocp4-prod-tlv-infra-01"), "^ocp")

        async for _ in wrapped.collect():
            pass

        assert wrapped.collection_errors == ()


class TestEndpointlessAndUnfilteredTypes:
    """`REDFISH_STANDALONE` is in both `_ENDPOINTLESS_TYPES` and `_UNFILTERED_TYPES`,
    read inside `_run` ahead of `_build_provider`; flipping either silently ingests
    zero BMCs or demands a variable that does not exist (docs/notes/2026-09-audit.md C4/T3).
    """

    class _RaisingResolver:
        """Stands in for `EnvConnectionResolver`: `.resolve()` raises, so a call
        proves `_ENDPOINTLESS_TYPES` was not honoured.
        """

        def __init__(self, _settings: Any) -> None:
            pass

        def resolve(self, manager_type: ManagerType) -> ManagerConnection:
            raise AssertionError(f"resolve() must not be called for {manager_type}")

    async def test_redfish_standalone_never_resolves_an_endpoint(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        settings = _settings(redfish_inventory_file="inventory/standalone.yaml")
        monkeypatch.setattr(run_collector, "EnvConnectionResolver", self._RaisingResolver)
        monkeypatch.setattr(run_collector, "_build_provider", _factory(TestNameFilter._Fake()))
        monkeypatch.setattr(run_collector, "get_settings", lambda: settings)

        code = await _run(manager_type=ManagerType.REDFISH_STANDALONE, dry_run=True)

        assert code == 0
        # The `Manager` projection's endpoint is the inventory file path.
        assert "inventory/standalone.yaml" in capsys.readouterr().out

    async def test_redfish_standalone_ignores_the_name_pattern(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        settings = _settings(
            redfish_inventory_file="inventory/standalone.yaml",
            collector_name_pattern="^ocp",
        )
        monkeypatch.setattr(
            run_collector,
            "_build_provider",
            _factory(TestNameFilter._Fake("ocp4-prod-tlv-infra-01", "vmhost-two-14")),
        )
        monkeypatch.setattr(run_collector, "get_settings", lambda: settings)

        code = await _run(manager_type=ManagerType.REDFISH_STANDALONE, dry_run=True)

        assert code == 0
        out = capsys.readouterr().out
        assert "ocp4-prod-tlv-infra-01" in out
        assert "vmhost-two-14" in out

    async def test_an_explicit_redfish_pattern_overrides_the_exemption(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """`_UNFILTERED_TYPES` suppresses the *global* pattern, not an
        operator who set `INVENTORY_REDFISH_NAME_PATTERN` by name.
        """
        settings = _settings(
            redfish_inventory_file="inventory/standalone.yaml",
            collector_name_pattern="",
            redfish_name_pattern="^ocp",
        )
        monkeypatch.setattr(
            run_collector,
            "_build_provider",
            _factory(TestNameFilter._Fake("ocp4-prod-tlv-infra-01", "vmhost-two-14")),
        )
        monkeypatch.setattr(run_collector, "get_settings", lambda: settings)

        code = await _run(manager_type=ManagerType.REDFISH_STANDALONE, dry_run=True)

        assert code == 0
        out = capsys.readouterr().out
        assert "ocp4-prod-tlv-infra-01" in out
        assert "vmhost-two-14" not in out


class FakeMongo:
    """Stands in for `MongoClientHolder` so `TestRunExitCodes` runs without a
    database; the dry-run path never connects (`TestDryRunNeverTouchesMongo`).
    """

    def __init__(self, _settings: Any) -> None:
        self.db = SimpleNamespace()
        self.closed = 0

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.closed += 1


def _outcome(*, fetched: int = 3, errors: int = 0, collection_errors: tuple[str, ...] = ()) -> Any:
    summary = IngestSummary()
    summary.fetched = fetched
    summary.created = fetched
    summary.errors = errors
    return run_collector._RunOutcome(summary=summary, collection_errors=collection_errors)


class TestRunExitCodes:
    """0 complete, 1 total failure, 2 not configured, 3 partial — without 3, a
    run reaching half the fleet was indistinguishable from a healthy smaller one.
    """

    @pytest.fixture(autouse=True)
    def _no_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_collector, "MongoClientHolder", FakeMongo)

        async def _no_indexes(_db: Any) -> None:
            return None

        monkeypatch.setattr(run_collector, "ensure_indexes", _no_indexes)
        monkeypatch.setattr(run_collector, "get_settings", _central_settings_for_run)

    async def _run_with(self, monkeypatch: pytest.MonkeyPatch, outcome: Any) -> int:
        async def _fake_run_one(*_args: Any, **_kwargs: Any) -> Any:
            return outcome

        monkeypatch.setattr(run_collector, "_run_one_manager", _fake_run_one)
        return await _run(manager_type=ManagerType.UCS_CENTRAL)

    async def test_a_complete_run_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        code = await self._run_with(monkeypatch, _outcome())

        assert code == 0
        assert "PARTIAL" not in capsys.readouterr().out

    async def test_an_unreachable_domain_exits_three(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        code = await self._run_with(
            monkeypatch,
            _outcome(collection_errors=("domain 'b' (10.0.0.2) failed: bad credentials",)),
        )

        assert code == 3
        out = capsys.readouterr().out
        assert "PARTIAL" in out
        # The specific domain is printed, not just a count.
        assert "10.0.0.2" in out

    async def test_a_plain_unreachable_host_alone_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """A down BMC is normal fleet-wide — see `..redfish.provider`'s
        module docstring — so it no longer fails the pod.
        """
        code = await self._run_with(
            monkeypatch,
            _outcome(collection_errors=("10.0.0.9: unreachable — Connection refused",)),
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "PARTIAL —" not in out
        assert "10.0.0.9" in out

    async def test_a_rejected_credential_alone_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """Changed 2026-09-10, at the operator's request: a rejected BMC
        credential no longer fails the pod either, same as an unreachable
        host — both are `_is_benign_collection_error`.
        """
        code = await self._run_with(
            monkeypatch,
            _outcome(collection_errors=("10.0.0.5: login failed for credential 'ome-bmc'",)),
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "PARTIAL —" not in out
        assert "10.0.0.5" in out

    async def test_a_mix_of_unreachable_and_a_real_failure_still_exits_three(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """One benign miss must not hide a real one riding along with it."""
        code = await self._run_with(
            monkeypatch,
            _outcome(
                collection_errors=(
                    "10.0.0.9: unreachable — Connection refused",
                    "10.0.0.2: TLS verification failed — bad cert",
                )
            ),
        )

        assert code == 3
        out = capsys.readouterr().out
        assert "PARTIAL" in out
        assert "10.0.0.9" in out
        assert "10.0.0.2" in out

    async def test_failed_ingests_alone_exit_three(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """A server that reached the pipeline and failed to be written is
        the same class of problem: the run did not record the whole fleet.
        This one also used to exit 0.
        """
        code = await self._run_with(monkeypatch, _outcome(errors=2))

        assert code == 3
        assert "2 server(s) failed to ingest" in capsys.readouterr().out

    async def test_a_total_failure_still_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """Partial and total failure stay distinguishable — 3 means "some
        data landed", 1 means none did.
        """
        code = await self._run_with(monkeypatch, None)

        out = capsys.readouterr().out
        assert code == 1
        assert "FAILED" in out
        # `_run_one_manager` swallows its exception into `None`, so the timer
        # wraps its call site in `_run`: a failed run still reports how long it took.
        assert "took=" in out

    async def test_a_complete_run_logs_run_complete_with_a_raw_float_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`collector.run_complete` is the one event a future dashboard
        reads run duration from, so it must carry a raw number — never the
        formatted `took=` string — and say which path produced it.
        """
        logged: list[tuple[str, dict[str, Any]]] = []

        class _RecordingLogger:
            def info(self, event: str, **kwargs: Any) -> None:
                logged.append((event, kwargs))

            def warning(self, event: str, **kwargs: Any) -> None:
                logged.append((event, kwargs))

            def error(self, event: str, **kwargs: Any) -> None:
                logged.append((event, kwargs))

            def exception(self, event: str, **kwargs: Any) -> None:
                logged.append((event, kwargs))

        monkeypatch.setattr(run_collector, "logger", _RecordingLogger())
        code = await self._run_with(monkeypatch, _outcome())

        assert code == 0
        run_complete = [kwargs for event, kwargs in logged if event == "collector.run_complete"]
        assert len(run_complete) == 1
        assert run_complete[0]["dry_run"] is False
        assert isinstance(run_complete[0]["seconds"], float)

    async def test_missing_configuration_still_exits_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(run_collector, "get_settings", lambda: _settings())

        assert await _run(manager_type=ManagerType.UCS_CENTRAL) == 2


class TestDryRunNeverTouchesMongo:
    """`--dry-run` only talks to the vendor manager, never to MongoDB — `_run`
    used to connect before checking `dry_run`, so an unreachable Mongo failed it.
    """

    async def test_dry_run_never_connects_to_mongo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _ExplodingMongo:
            def __init__(self, _settings: Any) -> None:
                pass

            async def connect(self) -> None:
                raise AssertionError("dry run must never connect to MongoDB")

        async def _fake_dry_run(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(run_collector, "MongoClientHolder", _ExplodingMongo)
        monkeypatch.setattr(run_collector, "_dry_run_one_manager", _fake_dry_run)
        monkeypatch.setattr(run_collector, "get_settings", _central_settings_for_run)

        assert await _run(manager_type=ManagerType.UCS_CENTRAL, dry_run=True) == 0


class TestDryRunExitCodes:
    """`--dry-run` has its own exit-code branch, separate from `TestRunExitCodes`:
    0 on success, 1 on any exception.
    """

    async def test_a_successful_dry_run_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_dry_run(*_args: Any, **_kwargs: Any) -> int:
            return 3

        monkeypatch.setattr(run_collector, "_dry_run_one_manager", _fake_dry_run)
        monkeypatch.setattr(run_collector, "get_settings", _central_settings_for_run)

        assert await _run(manager_type=ManagerType.UCS_CENTRAL, dry_run=True) == 0

    async def test_a_failed_dry_run_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        async def _failing_dry_run(*_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("vendor endpoint unreachable")

        monkeypatch.setattr(run_collector, "_dry_run_one_manager", _failing_dry_run)
        monkeypatch.setattr(run_collector, "get_settings", _central_settings_for_run)

        code = await _run(manager_type=ManagerType.UCS_CENTRAL, dry_run=True)

        assert code == 1
        assert "FAILED" in capsys.readouterr().out


class TestManagerTypeInLogContext:
    """`ingest.completed` has no `manager_type` field, so it reaches the log line
    via structlog contextvars — asserted on those directly, not `capture_logs()`,
    whose `cache_logger_on_first_use` interaction makes it order-dependent.
    """

    async def test_manager_type_is_bound_for_the_run_and_unbound_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import structlog

        seen: dict[str, Any] = {}

        async def _fake_dry_run(*_args: Any, **_kwargs: Any) -> None:
            seen.update(structlog.contextvars.get_contextvars())
            return

        monkeypatch.setattr(run_collector, "_dry_run_one_manager", _fake_dry_run)
        monkeypatch.setattr(run_collector, "get_settings", _central_settings_for_run)

        assert "manager_type" not in structlog.contextvars.get_contextvars()
        await _run(manager_type=ManagerType.UCS_CENTRAL, dry_run=True)

        assert seen.get("manager_type") == "UCS_CENTRAL"
        # Unbound in `_run`'s own `finally`, not left to leak into the next test.
        assert "manager_type" not in structlog.contextvars.get_contextvars()


class TestParseArgs:
    def test_manager_type_is_required(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_rejects_an_unknown_manager_type(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--manager-type", "NOT_A_VENDOR"])

    def test_accepts_a_known_manager_type(self) -> None:
        assert _parse_args(["--manager-type", "UCS_MANAGER"]).manager_type == "UCS_MANAGER"

    def test_dry_run_and_debug_flags_default_off(self) -> None:
        args = _parse_args(["--manager-type", "UCS_MANAGER"])
        assert args.dry_run is False
        assert args.debug_xml is False
        assert args.limit is None
