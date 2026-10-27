"""`reset_trigger` names all-UNKNOWN disks and NICs on a reachable OME-collected Dell (ADR-0050)."""

from datetime import UTC, datetime

import pytest

from app.domain.enums import LinkState, Vendor
from app.domain.models.hardware import Hardware, Storage, StorageDrive
from app.domain.models.network import NetworkInfo, NetworkInterface
from app.domain.models.server import Identity, Server
from app.domain.services.bmc_reset import reset_trigger

NOW = datetime.now(UTC)
UNKNOWN, UP = LinkState.UNKNOWN, LinkState.UP


def _server(
    *,
    links: tuple[LinkState, ...] = (UP,),
    drives: tuple[str | None, ...] = ("HEALTHY",),
    vendor: Vendor = Vendor.DELL,
    provider: str | None = "OPENMANAGE",
    reachable: bool = True,
) -> Server:
    """
    Build a server with the given link states and drive healths.

    Args:
        links (tuple[LinkState, ...]): One interface per entry.
        drives (tuple[str | None, ...]): One drive per entry.
        vendor (Vendor): The server's vendor.
        provider (str | None): The collector that owns it.
        reachable (bool): Whether the BMC was reached.

    Returns:
        Server: The server.
    """
    return Server(
        _id="srv_x",
        name="x",
        identity=Identity(vendor=vendor),
        created_at=NOW,
        updated_at=NOW,
        source_provider=provider,
        reachable=reachable,
        hardware=Hardware(
            storage=Storage(
                drives=[StorageDrive(id=f"d{i}", health=h) for i, h in enumerate(drives)]
            )
        ),
        network=NetworkInfo(
            interfaces=[NetworkInterface(name=f"eno{i}", link_state=s) for i, s in enumerate(links)]
        ),
    )


@pytest.mark.parametrize(
    ("links", "drives", "expected"),
    [
        ((UNKNOWN, UNKNOWN), ("HEALTHY",), "nics"),
        ((UP,), (None, "UNKNOWN"), "disks"),
        ((UNKNOWN,), ("UNKNOWN",), "both"),
        ((UNKNOWN, UP), ("UNKNOWN", "HEALTHY"), None),
        ((UP,), ("HEALTHY",), None),
    ],
)
def test_names_which_side_is_all_unknown(
    links: tuple[LinkState, ...], drives: tuple[str | None, ...], expected: str | None
) -> None:
    assert reset_trigger(_server(links=links, drives=drives)) == expected


def test_a_server_with_no_interfaces_and_no_drives_never_triggers() -> None:
    assert reset_trigger(_server(links=(), drives=())) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"vendor": Vendor.CISCO},
        {"provider": "REDFISH_STANDALONE"},
        {"provider": None},
        {"reachable": False},
    ],
)
def test_only_a_reachable_dell_collected_through_ome_qualifies(
    overrides: dict[str, object],
) -> None:
    server = _server(links=(UNKNOWN,), drives=("UNKNOWN",), **overrides)  # ty: ignore[invalid-argument-type]
    assert reset_trigger(server) is None
