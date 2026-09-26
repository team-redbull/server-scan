from datetime import UTC, datetime

from app.domain.enums import LinkState, Vendor
from app.domain.models.connectivity import Connectivity, ConnectivityFacts
from app.domain.models.hardware import Hardware, Power, Psu, Storage, StorageDrive
from app.domain.models.network import NetworkInfo, NetworkInterface
from app.domain.models.server import Identity, Server
from app.domain.services.health.facts import extract_facts

NOW = datetime.now(UTC)
IDENTITY = Identity(vendor=Vendor.DELL)


def test_extract_facts_on_empty_server_returns_zeroed_facts() -> None:
    server = Server(_id="srv_x", name="x", identity=IDENTITY, created_at=NOW, updated_at=NOW)
    facts = extract_facts(server)
    assert facts["storage.drive_count"] == 0
    assert facts["storage.failed_drive_count"] == 0
    assert facts["connectivity.fabric_paths_down"] == 0


def test_extract_facts_counts_failed_drives() -> None:
    """CRITICAL is the vocabulary both collectors normalize a dead drive
    onto — counting anything else counts nothing outside fake data.
    """
    server = Server(
        _id="srv_x",
        name="x",
        identity=IDENTITY,
        created_at=NOW,
        updated_at=NOW,
        hardware=Hardware(
            storage=Storage(
                drives=[
                    StorageDrive(id="d1", health="CRITICAL"),
                    StorageDrive(id="d2", health="HEALTHY"),
                    StorageDrive(id="d3", health="CRITICAL"),
                ]
            )
        ),
    )
    facts = extract_facts(server)
    assert facts["storage.drive_count"] == 3
    assert facts["storage.failed_drive_count"] == 2
    assert facts["storage.drive_healths"] == ["CRITICAL", "HEALTHY", "CRITICAL"]


def test_extract_facts_reads_connectivity_facts_directly() -> None:
    server = Server(
        _id="srv_x",
        name="x",
        identity=IDENTITY,
        created_at=NOW,
        updated_at=NOW,
        connectivity=Connectivity(
            facts=ConnectivityFacts(fabric_paths_total=2, fabric_paths_up=1, fabric_paths_down=1)
        ),
    )
    facts = extract_facts(server)
    assert facts["connectivity.fabric_paths_up"] == 1
    assert facts["connectivity.fabric_paths_down"] == 1


def test_extract_facts_counts_failed_psus() -> None:
    """`Psu.health` is UP/DOWN/DISABLED/UNKNOWN, not "OK"/"FAILED", and UNKNOWN
    is not counted as failed (ADR-0027).
    """
    server = Server(
        _id="srv_x",
        name="x",
        identity=IDENTITY,
        created_at=NOW,
        updated_at=NOW,
        hardware=Hardware(
            power=Power(
                psus=[
                    Psu(id="p1", health="UP"),
                    Psu(id="p2", health="DOWN"),
                    Psu(id="p3", health="UNKNOWN"),
                ]
            )
        ),
    )
    facts = extract_facts(server)
    assert facts["power.psu_count"] == 3
    assert facts["power.failed_psu_count"] == 1


def test_extract_facts_link_states() -> None:
    server = Server(
        _id="srv_x",
        name="x",
        identity=IDENTITY,
        created_at=NOW,
        updated_at=NOW,
        network=NetworkInfo(
            interfaces=[
                NetworkInterface(name="nic1", link_state=LinkState.UP),
                NetworkInterface(name="nic2", link_state=LinkState.DOWN),
            ]
        ),
    )
    facts = extract_facts(server)
    assert facts["network.interface_link_states"] == ["UP", "DOWN"]


def _with_links(*states: LinkState) -> dict[str, object]:
    """
    Extract facts for a server whose only feature is its link states.

    Args:
        states (LinkState): One per interface, in order.

    Returns:
        dict[str, object]: The extracted facts.
    """
    server = Server(
        _id="srv_x",
        name="x",
        identity=IDENTITY,
        created_at=NOW,
        updated_at=NOW,
        network=NetworkInfo(
            interfaces=[
                NetworkInterface(name=f"nic{n}", link_state=state)
                for n, state in enumerate(states, start=1)
            ]
        ),
    )
    return extract_facts(server)


def test_links_known_count_excludes_unknown_link_states() -> None:
    """A link state of UNKNOWN is "not read", never "not up".

    UCS reports UNKNOWN on ~99.75% of vNICs, so counting one as a down
    link made nearly every Cisco server read CRITICAL — ADR-0027.
    """
    facts = _with_links(LinkState.UP, LinkState.DOWN, LinkState.UNKNOWN, LinkState.DISABLED)

    assert facts["network.interface_count"] == 4
    assert facts["network.links_known_count"] == 3
    assert facts["network.links_up_count"] == 1


def test_network_has_no_data_when_no_link_state_could_be_read() -> None:
    """Listing NICs is not reading them.

    Every state UNKNOWN is a verdict from zero readings — ADR-0027.
    """
    assert _with_links(LinkState.UNKNOWN, LinkState.UNKNOWN)["network.has_data"] is False


def test_network_has_data_when_even_one_link_state_was_read() -> None:
    """One readable state is enough to judge the category on."""
    assert _with_links(LinkState.UP, LinkState.UNKNOWN)["network.has_data"] is True


def test_links_known_count_is_zero_when_nothing_could_be_read() -> None:
    facts = _with_links(LinkState.UNKNOWN, LinkState.UNKNOWN)

    assert facts["network.interface_count"] == 2
    assert facts["network.links_known_count"] == 0
