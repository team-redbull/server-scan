"""The fake OpenShift membership pass.

It stands in for two CronJobs that do not exist yet, so these pin the
contracts those jobs will have to honour rather than the randomness.
"""

from __future__ import annotations

import pytest

from app.domain.enums import HealthSeverity, InstallationType, OpenShiftState, Vendor
from app.domain.models.classification import Classification
from app.domain.models.health import Health
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.infrastructure.providers.fake.openshift import openshift_for
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.unit


# A share of every server is seeded AVAILABLE regardless of its name, so
# a test exercising the installed paths must pin an id that draws past it.
_INSTALLED_ID = "srv_openshift_test_0"
_AVAILABLE_ID = "srv_openshift_free_2"


def _server(
    name: str,
    *,
    server_id: str = _INSTALLED_ID,
    installation_type: InstallationType = InstallationType.UNCLASSIFIED,
    site_id: str | None = "tlv",
) -> Server:
    """
    Build one stored server to report on.

    Args:
        name (str): Its hostname.
        server_id (str): Its id, which seeds the per-server RNG.
        installation_type (InstallationType): Its regex classification.
        site_id (str | None): Its site.

    Returns:
        Server: A server the pass can read.
    """
    now = utcnow()
    return Server(
        _id=server_id,
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(
            vendor=Vendor.DELL,
            serial="OSTEST0001",
            serial_normalized="ostest0001",
            nic_macs=["aa:bb:cc:00:00:01"],
        ),
        site_id=site_id,
        classification=Classification(installation_type=installation_type),
        health=Health(overall=HealthSeverity.HEALTHY),
        created_at=now,
        updated_at=now,
    )


def test_it_is_deterministic_per_server() -> None:
    """Re-running the seeder over the same fleet must not reshuffle it,
    and must not depend on iteration order — the state is drawn from the
    server's own id, not from a shared stream.
    """
    server = _server("random-server-0001")

    first, second = openshift_for(server), openshift_for(server)

    # `last_reported_at` is `utcnow()`, so it differs between two calls by
    # design; everything drawn from the id must not.
    assert first.model_dump(exclude={"last_reported_at"}) == second.model_dump(
        exclude={"last_reported_at"}
    )


def test_a_hosted_cluster_node_names_its_cluster_and_its_mce() -> None:
    """Both, because a hosted cluster name is only unique within the MCE
    that runs it.
    """
    state = openshift_for(_server("ocp4-hypershift-tlv-05"))

    assert state.lifecycle_state is OpenShiftState.INSTALLED
    assert state.cluster_name is not None
    assert state.mce_name == "mce-tlv"


def test_a_upi_node_names_a_cluster_but_no_mce() -> None:
    """No MCE manages a UPI cluster, so claiming one would be a fiction
    the real job could never produce.
    """
    state = openshift_for(
        _server("ocp4-prod-tlv-compute-01", installation_type=InstallationType.UPI)
    )

    assert state.lifecycle_state is OpenShiftState.INSTALLED
    assert state.cluster_name == "upi-tlv"
    assert state.mce_name is None


def test_an_agent_in_inventory_has_an_mce_but_no_cluster() -> None:
    """`INSTALLED_TO_INVENTORY` means registered to an MCE and bound to
    nothing. `cluster_name` stays None: there is no cluster, which is a
    different claim from a cluster whose name went unread.
    """
    states = [
        openshift_for(_server(f"random-server-{i:04d}", server_id=f"srv_avail_{i}"))
        for i in range(40)
    ]
    in_inventory = [s for s in states if s.lifecycle_state is OpenShiftState.INSTALLED_TO_INVENTORY]

    assert in_inventory, "no server came back in inventory across 40 draws"
    for state in in_inventory:
        assert state.cluster_name is None
        assert state.mce_name is not None


def test_an_unclaimed_server_carries_no_claims_at_all() -> None:
    """`AVAILABLE` is the default and the only state reached by absence:
    no cluster holds this machine. Every field stays empty rather than
    defaulting to something plausible.
    """
    states = [
        openshift_for(_server(f"random-server-{i:04d}", server_id=f"srv_unknown_{i}"))
        for i in range(40)
    ]
    available = [s for s in states if s.lifecycle_state is OpenShiftState.AVAILABLE]

    assert available, "no server came back available across 40 draws"
    for state in available:
        assert state.cluster_name is None
        assert state.mce_name is None
        assert state.last_reported_at is None


def test_a_reported_server_always_carries_a_timestamp() -> None:
    """Nothing reports a removal, so `last_reported_at` is the only thing
    that can distinguish a live membership from a stale one. A reported
    state without it would be unfalsifiable.
    """
    for name, installation_type in (
        ("ocp4-hypershift-tlv-05", InstallationType.UNCLASSIFIED),
        ("ocp4-prod-tlv-compute-01", InstallationType.UPI),
    ):
        state = openshift_for(_server(name, installation_type=installation_type))
        assert state.last_reported_at is not None


def test_membership_is_not_forced_to_agree_with_the_classification() -> None:
    """The point of keeping the two apart: a name that classifies one way
    can still be running somewhere else, and that is the signal.
    """
    state = openshift_for(_server("ocp4-hypershift-tlv-05"))

    assert state.lifecycle_state is OpenShiftState.INSTALLED


def test_a_name_mismatch_is_occasionally_seeded() -> None:
    """ADR-0036: the UI's mismatch chip and its filter need seeded data
    even though this fleet has no real cluster reporting a different name.
    """
    servers = [
        _server(f"ocp4-prod-tlv-compute-{i:04d}", server_id=f"srv_mismatch_{i}") for i in range(400)
    ]
    states = [openshift_for(server) for server in servers]
    mismatched = [
        (server, state)
        for server, state in zip(servers, states, strict=True)
        if state.lifecycle_state is not OpenShiftState.AVAILABLE and state.reported_name
    ]

    assert mismatched, "no mismatch seeded across 400 draws"
    assert all(state.reported_name != server.name for server, state in mismatched)


def test_a_single_token_name_never_gets_a_mismatch() -> None:
    """The mismatch substitutes a hyphen-separated token; a bare hostname
    has none to swap, so it is left untouched rather than mangled.
    """
    states = [openshift_for(_server("soloname", server_id=f"srv_solo_{i}")) for i in range(200)]
    installed = [s for s in states if s.lifecycle_state is not OpenShiftState.AVAILABLE]

    assert installed, "no installed draw across 200 ids"
    assert all(s.reported_name is None for s in installed)


def test_every_state_is_reachable_for_every_installation_type() -> None:
    """Convention 10: a seeded fleet where only junk-named servers are ever
    free is the opposite of the real one, and would make the Available
    card and `?openshift_state=AVAILABLE` look broken in every demo.
    """
    for installation_type in InstallationType:
        seen = {
            openshift_for(
                _server(
                    "ocp4-prod-tlv-compute-01",
                    server_id=f"srv_{installation_type.value}_{index}",
                    installation_type=installation_type,
                )
            ).lifecycle_state
            for index in range(200)
        }
        assert OpenShiftState.AVAILABLE in seen, installation_type
        assert OpenShiftState.INSTALLED in seen or (
            installation_type is InstallationType.UNCLASSIFIED
        ), installation_type
