"""`_stable_view` ignores exactly the per-run stamps and nothing else (ADR-0044)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.application.services.ingest import _has_unset_fields, _stable_view
from app.domain.enums import HealthSeverity, InstallationType, Vendor
from app.domain.models.classification import Classification
from app.domain.models.connectivity import Connectivity, ConnectivityAttachment
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.unit

_LATER = timedelta(hours=3)


def _attachment(last_seen_delta: timedelta = timedelta(0)) -> ConnectivityAttachment:
    return ConnectivityAttachment(
        type="FABRIC",
        provider=None,
        fabric="A",
        fabric_name=None,
        fabric_id=None,
        fabric_model=None,
        fabric_serial=None,
        server_interface="vnic0",
        server_port=None,
        fabric_port=None,
        admin_state="UP",
        oper_state="UP",
        speed_mbps=None,
        interface_kind="PHYSICAL",
        last_seen=utcnow() + last_seen_delta,
    )


def _server() -> Server:
    now = utcnow()
    return Server(
        _id=new_id("server"),
        name="ocp4-prod-tlv-infra-07",
        name_normalized=normalize_text("ocp4-prod-tlv-infra-07"),
        identity=Identity(
            vendor=Vendor.CISCO,
            serial="SN1",
            serial_normalized=normalize_text("SN1"),
            system_uuid="uuid-1",
        ),
        classification=Classification(
            installation_type=InstallationType.UNCLASSIFIED,
            classified_at=now,
            classification_version=4,
        ),
        health=Health(overall=HealthSeverity.HEALTHY, evaluated_at=now),
        connectivity=Connectivity(attachments=[_attachment()]),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
        listed_at=now,
        revision=7,
    )


def _later(server: Server) -> Server:
    """The same server one run later: only the per-run stamps and counters moved."""
    stamped = server.model_copy(deep=True)
    stamped.revision += 1
    stamped.updated_at += _LATER
    stamped.last_seen_at = (server.last_seen_at or utcnow()) + _LATER
    stamped.listed_at = (server.listed_at or utcnow()) + _LATER
    stamped.health.evaluated_at = (server.health.evaluated_at or utcnow()) + _LATER
    stamped.classification.classified_at = (
        server.classification.classified_at or utcnow()
    ) + _LATER
    stamped.classification.classification_version += 1
    stamped.connectivity.attachments[0].last_seen = utcnow() + _LATER
    return stamped


def test_per_run_stamps_and_counters_do_not_count_as_a_change() -> None:
    server = _server()
    assert _stable_view(_later(server)) == _stable_view(server)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: setattr(s, "model", "UCSB-B200-M6"),
        lambda s: setattr(s, "name", "ocp4-prod-tlv-infra-08"),
        lambda s: setattr(s, "reachable", False),
        lambda s: setattr(s, "site_id", "ams"),
        lambda s: setattr(s, "tags", ["new-tag"]),
        lambda s: setattr(s, "unread_fields", ["hardware.cpu"]),
        lambda s: setattr(s, "maintenance", Maintenance(enabled=True, reason="x")),
        lambda s: setattr(s.health, "overall", HealthSeverity.CRITICAL),
        lambda s: setattr(s.classification, "installation_type", InstallationType.UPI),
        lambda s: setattr(s.connectivity.attachments[0], "oper_state", "DOWN"),
        lambda s: s.connectivity.attachments.append(_attachment()),
    ],
    ids=[
        "model",
        "name",
        "reachable",
        "site_id",
        "tags",
        "unread_fields",
        "maintenance",
        "health.overall",
        "classification.installation_type",
        "attachment.oper_state",
        "attachment added",
    ],
)
def test_a_real_content_change_is_seen(mutate) -> None:  # noqa: ANN001 - a parametrized lambda
    server = _server()
    changed = server.model_copy(deep=True)
    mutate(changed)
    assert _stable_view(changed) != _stable_view(server)


def test_a_document_with_every_field_is_current() -> None:
    stored = _server().model_dump(by_alias=True, mode="json")
    assert _has_unset_fields(Server.model_validate(stored)) is False


@pytest.mark.parametrize(
    "path", [("listed_at",), ("health", "evaluated_at"), ("connectivity", "facts")]
)
def test_a_document_missing_a_field_predates_the_model(path: tuple[str, ...]) -> None:
    stored = _server().model_dump(by_alias=True, mode="json")
    node = stored
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    assert _has_unset_fields(Server.model_validate(stored)) is True


def test_a_field_missing_inside_a_list_item_is_seen() -> None:
    stored = _server().model_dump(by_alias=True, mode="json")
    del stored["connectivity"]["attachments"][0]["last_seen"]
    assert _has_unset_fields(Server.model_validate(stored)) is True
