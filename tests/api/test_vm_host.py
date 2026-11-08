"""`GET /api/v1/servers/{id}/vm-host`: a VM's host, resolved at read time (ADR-0059)."""

from __future__ import annotations

from httpx import AsyncClient
from tests.api.test_servers import _make_server, app_context

from app.domain.enums import HealthSeverity, OpenShiftState, Vendor
from app.domain.models.hardware import Cpu
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Server, VmHost
from app.infrastructure.mongodb.server_repository import MongoServerRepository

__all__ = ["app_context"]

_Context = tuple[AsyncClient, MongoServerRepository]


def _host(
    index: int,
    name: str,
    *,
    cluster: str | None = "ocp4-tlv",
    reported_name: str | None = None,
) -> Server:
    server = _make_server(index, name=name, health=HealthSeverity.HEALTHY)
    server.hardware.cpu = Cpu(sockets=2, cores=32, threads=64, model="Xeon Gold 6348")
    if cluster is not None:
        server.openshift = OpenShiftLifecycle(
            lifecycle_state=OpenShiftState.INSTALLED,
            cluster_name=cluster,
            reported_name=reported_name,
        )
    return server


def _vm(index: int, node: str | None, *, cluster: str | None = "ocp4-tlv") -> Server:
    server = _make_server(index, name=f"ocp4-tlv-vm-{index:02d}", vendor=Vendor.KUBEVIRT)
    server.vm_host = VmHost(cluster_name=cluster, node_name=node)
    return server


async def _vm_host(client: AsyncClient, server: Server) -> dict[str, object]:
    resp = await client.get(f"/api/v1/servers/{server.id}/vm-host")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_node_in_the_cluster_resolves_through_the_membership(app_context: _Context) -> None:
    client, repo = app_context
    host = _host(1, "ocp4-tlv-compute-01")
    vm = _vm(2, "ocp4-tlv-compute-01")
    for server in (host, vm):
        await repo.upsert(server)

    body = await _vm_host(client, vm)

    assert (body["cluster_name"], body["node_name"], body["match"]) == (
        "ocp4-tlv",
        "ocp4-tlv-compute-01",
        "MEMBERSHIP",
    )
    resolved = body["host"]
    assert isinstance(resolved, dict)
    assert resolved["id"] == host.id
    assert resolved["cpu"]["model"] == "Xeon Gold 6348"
    assert (resolved["health"], resolved["maintenance"]) == ("HEALTHY", False)
    assert resolved["cluster_name"] == "ocp4-tlv"


async def test_a_renamed_host_resolves_through_its_reported_name(app_context: _Context) -> None:
    client, repo = app_context
    host = _host(1, "old-vendor-name", reported_name="worker-7")
    vm = _vm(2, "worker-7")
    for server in (host, vm):
        await repo.upsert(server)

    body = await _vm_host(client, vm)

    assert body["match"] == "MEMBERSHIP"
    assert isinstance(body["host"], dict)
    assert body["host"]["id"] == host.id


async def test_a_host_stored_without_a_reported_name_key_matches(app_context: _Context) -> None:
    client, repo = app_context
    host = _host(1, "ocp4-tlv-compute-01")
    vm = _vm(2, "ocp4-tlv-compute-01")
    for server in (host, vm):
        await repo.upsert(server)
    collection = repo._collection
    await collection.update_one({"_id": host.id}, {"$unset": {"openshift.reported_name": ""}})

    body = await _vm_host(client, vm)

    assert body["match"] == "MEMBERSHIP"


async def test_a_host_in_another_cluster_falls_back_to_its_name(app_context: _Context) -> None:
    client, repo = app_context
    host = _host(1, "ocp4-tlv-compute-01", cluster="ocp4-nyc")
    vm = _vm(2, "ocp4-tlv-compute-01")
    for server in (host, vm):
        await repo.upsert(server)

    body = await _vm_host(client, vm)

    assert body["match"] == "NAME"
    assert isinstance(body["host"], dict)
    assert body["host"]["id"] == host.id


async def test_a_vm_never_resolves_to_a_vm(app_context: _Context) -> None:
    client, repo = app_context
    namesake = _vm(1, None)
    namesake.name = namesake.name_normalized = "worker-1"
    vm = _vm(2, "worker-1")
    for server in (namesake, vm):
        await repo.upsert(server)

    body = await _vm_host(client, vm)

    assert (body["match"], body["host"]) == ("NOT_IN_INVENTORY", None)


async def test_two_servers_with_the_node_name_are_ambiguous(app_context: _Context) -> None:
    client, repo = app_context
    vm = _vm(3, "worker-1")
    for server in (_host(1, "worker-1", cluster=None), _host(2, "worker-1", cluster=None), vm):
        await repo.upsert(server)

    body = await _vm_host(client, vm)

    assert (body["match"], body["host"]) == ("AMBIGUOUS", None)


async def test_a_stopped_vm_is_not_running(app_context: _Context) -> None:
    client, repo = app_context
    vm = _vm(1, None)
    await repo.upsert(vm)

    body = await _vm_host(client, vm)

    assert (body["node_name"], body["match"], body["host"]) == (None, "NOT_RUNNING", None)


async def test_the_detail_carries_the_stored_names(app_context: _Context) -> None:
    client, repo = app_context
    vm = _vm(1, "worker-1")
    physical = _make_server(2)
    for server in (vm, physical):
        await repo.upsert(server)

    vm_detail = (await client.get(f"/api/v1/servers/{vm.id}")).json()
    physical_detail = (await client.get(f"/api/v1/servers/{physical.id}")).json()

    assert vm_detail["vm_host"] == {"cluster_name": "ocp4-tlv", "node_name": "worker-1"}
    assert physical_detail["vm_host"] is None


async def test_a_physical_server_or_unknown_id_is_404(app_context: _Context) -> None:
    client, repo = app_context
    physical = _make_server(1)
    await repo.upsert(physical)

    assert (await client.get(f"/api/v1/servers/{physical.id}/vm-host")).status_code == 404
    assert (await client.get("/api/v1/servers/server_missing/vm-host")).status_code == 404
