"""An in-process kubevirt-redfish facade, on stdlib only, for hermetic KUBEVIRT tests.

The sibling of `tests.redfish_fixture`, with HTTP Basic instead of sessions:
the facade answers the service root without credentials and everything else
only with them (docs/adr/0058). `kubevirt_tree` builds the exact payload shapes
the fork serves, checked against a live OpenShift Virtualization 4.22 crawl.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlsplit

from tests.redfish_fixture import _QuietServer

ROOT = "/redfish/v1/"
SYSTEMS = "/redfish/v1/Systems"


@dataclass
class Vm:
    """
    One VM as the facade would serve it.

    Attributes:
        name (str): The VM's name (`ComputerSystem.Name`).
        namespace (str): Its namespace; `Id` is `<namespace>.<name>` (enhanced).
        power (str): `PowerState`, `On` or `Off`.
        serial (str | None): `SerialNumber`, None for a facade build without it.
        uuid (str | None): `UUID`, None to omit.
        model (str | None): `Model` (machine type), None to omit.
        cpu_model (str | None): `ProcessorSummary.Model`, None to omit (cluster default).
        vcpus (int): Cores on one socket.
        memory_mib (int | None): `CapacityMiB` of `Memory/1`; None omits it and the summary.
        processor_summary (bool): False omits `ProcessorSummary` (topology unread).
        nics (list[tuple[str, str | None]]): `(name, mac)` per interface.
        disks (list[tuple[str, int | None]]): `(name, capacity_bytes)` per drive.
        legacy_id (bool): Use the `legacy` convention, `Id == name`.
    """

    name: str
    namespace: str = "vms"
    power: str = "On"
    serial: str | None = "SERIAL-1"
    uuid: str | None = "11111111-2222-3333-4444-555555555555"
    model: str | None = "pc-q35-rhel9.8.0"
    cpu_model: str | None = None
    vcpus: int = 2
    memory_mib: int | None = 4096
    processor_summary: bool = True
    nics: list[tuple[str, str | None]] = field(
        default_factory=lambda: [("default", "02:00:00:00:00:01")]
    )
    disks: list[tuple[str, int | None]] = field(
        default_factory=lambda: [("rootdisk", None), ("scratch", 1024**3)]
    )
    legacy_id: bool = False

    @property
    def system_id(self) -> str:
        """
        The System `Id` the facade assigns.

        Returns:
            str: `<namespace>.<name>`, or the bare name under `legacy`.
        """
        return self.name if self.legacy_id else f"{self.namespace}.{self.name}"

    @property
    def path(self) -> str:
        """
        The System's `@odata.id`.

        Returns:
            str: `/redfish/v1/Systems/<Id>`.
        """
        return f"{SYSTEMS}/{self.system_id}"


def _collection(path: str, members: list[str]) -> dict[str, Any]:
    """
    A collection payload of bare member links, as the fork serves it.

    Args:
        path (str): The collection's own path.
        members (list[str]): Member paths.

    Returns:
        dict[str, Any]: The payload.
    """
    return {
        "@odata.id": path,
        "@odata.type": "#Collection.Collection",
        "Name": "Collection",
        "Members": [{"@odata.id": m} for m in members],
        "Members@odata.count": len(members),
    }


_OK = {"State": "Enabled", "Health": "OK"}


def _system(vm: Vm) -> dict[str, Any]:
    """
    One `ComputerSystem` payload.

    Args:
        vm (Vm): The VM.

    Returns:
        dict[str, Any]: The payload, omitting what the fork's `omitempty` omits.
    """
    p = vm.path
    system: dict[str, Any] = {
        "@odata.id": p,
        "@odata.type": "#ComputerSystem.v1_15_0.ComputerSystem",
        "Id": vm.system_id,
        "Name": vm.name,
        "SystemType": "Virtual",
        "Manufacturer": "KubeVirt",
        "Status": dict(_OK),
        "PowerState": vm.power,
        "MemorySummary": {},
        "Memory": {"@odata.id": f"{p}/Memory"},
        "Processors": {"@odata.id": f"{p}/Processors"},
        "Storage": {"@odata.id": f"{p}/Storage"},
        "EthernetInterfaces": {"@odata.id": f"{p}/EthernetInterfaces"},
        "Links": {"ManagedBy": [{"@odata.id": f"/redfish/v1/Managers/{vm.namespace}"}]},
    }
    identity = {"SerialNumber": vm.serial, "UUID": vm.uuid, "Model": vm.model}
    system.update({key: value for key, value in identity.items() if value is not None})
    if vm.memory_mib:
        system["MemorySummary"] = {"TotalSystemMemoryGiB": vm.memory_mib / 1024}
    if vm.processor_summary:
        summary: dict[str, Any] = {
            "Count": 1,
            "CoreCount": vm.vcpus,
            "LogicalProcessorCount": vm.vcpus,
            "ThreadingEnabled": False,
        }
        if vm.cpu_model:
            summary["Model"] = vm.cpu_model
        system["ProcessorSummary"] = summary
    return system


def kubevirt_tree(*vms: Vm) -> dict[str, Any]:
    """
    Every resource the facade serves for these VMs, by path.

    Args:
        *vms (Vm): The VMs the user can see.

    Returns:
        dict[str, Any]: Payloads by path, ready for `KubeVirtFixture`.
    """
    resources: dict[str, Any] = {
        ROOT: {
            "@odata.id": ROOT,
            "@odata.type": "#ServiceRoot.v1_0_0.ServiceRoot",
            "Id": "RootService",
            "Name": "KubeVirt Redfish",
            "Systems": {"@odata.id": SYSTEMS},
        },
        SYSTEMS: _collection(SYSTEMS, [vm.path for vm in vms]),
    }
    for vm in vms:
        p = vm.path
        resources[p] = _system(vm)
        resources[f"{p}/Memory"] = _collection(f"{p}/Memory", [f"{p}/Memory/1"])
        memory: dict[str, Any] = {
            "@odata.id": f"{p}/Memory/1",
            "Id": "1",
            "Name": "System Memory",
            "MemoryType": "DRAM",
            "Status": dict(_OK),
        }
        if vm.memory_mib:
            memory["CapacityMiB"] = vm.memory_mib
        resources[f"{p}/Memory/1"] = memory
        resources[f"{p}/Processors"] = _collection(f"{p}/Processors", [f"{p}/Processors/CPU0"])
        cpu: dict[str, Any] = {
            "@odata.id": f"{p}/Processors/CPU0",
            "Id": "CPU0",
            "Name": "vCPU Socket 0",
            "ProcessorType": "CPU",
            "TotalCores": vm.vcpus,
            "TotalThreads": vm.vcpus,
            "Status": dict(_OK),
        }
        if vm.cpu_model:
            cpu["Model"] = vm.cpu_model
        resources[f"{p}/Processors/CPU0"] = cpu
        storage = f"{p}/Storage/1"
        resources[f"{p}/Storage"] = _collection(f"{p}/Storage", [storage])
        resources[storage] = {
            "@odata.id": storage,
            "Id": "1",
            "Name": "VM Disks",
            "Status": dict(_OK),
            "Drives": [{"@odata.id": f"{storage}/Drives/{name}"} for name, _ in vm.disks],
            "Drives@odata.count": len(vm.disks),
        }
        for name, capacity in vm.disks:
            drive: dict[str, Any] = {
                "@odata.id": f"{storage}/Drives/{name}",
                "Id": name,
                "Name": name,
                "Status": dict(_OK),
            }
            if capacity is not None:
                drive["CapacityBytes"] = capacity
            resources[f"{storage}/Drives/{name}"] = drive
        nics = f"{p}/EthernetInterfaces"
        resources[nics] = _collection(nics, [f"{nics}/{name}" for name, _ in vm.nics])
        for name, mac in vm.nics:
            nic: dict[str, Any] = {
                "@odata.id": f"{nics}/{name}",
                "Id": name,
                "Name": name,
                "LinkStatus": "LinkUp" if vm.power == "On" else "NoLink",
                "Status": dict(_OK),
            }
            if mac is not None:
                nic["MACAddress"] = mac
            resources[f"{nics}/{name}"] = nic
    return resources


@dataclass
class KubeVirtFixture:
    """
    An in-process facade.

    Attributes:
        resources (dict[str, Any]): Payloads by path; an absent path is 404.
        username (str): The accepted user.
        password (str): Its password.
        faults (dict[str, int]): Path -> status to answer instead.
        fault_headers (dict[str, dict[str, str]]): Extra headers sent with a fault.
        fault_times (dict[str, int]): Path -> how many requests get the fault, then it heals.
        delays (dict[str, float]): Path -> seconds to stall first.
        redirects (dict[str, str]): Path -> `Location` of a 302.
        raw (dict[str, bytes]): Path -> a raw 200 body instead of JSON.
        reject_after (int | None): Every authenticated request after this many is a 401.
        requests (list[tuple[str, bool]]): Every (path, carried credentials) served.
    """

    resources: dict[str, Any] = field(default_factory=dict)
    username: str = "scan"
    password: str = "secret"
    faults: dict[str, int] = field(default_factory=dict)
    fault_headers: dict[str, dict[str, str]] = field(default_factory=dict)
    fault_times: dict[str, int] = field(default_factory=dict)
    delays: dict[str, float] = field(default_factory=dict)
    redirects: dict[str, str] = field(default_factory=dict)
    raw: dict[str, bytes] = field(default_factory=dict)
    reject_after: int | None = None
    requests: list[tuple[str, bool]] = field(default_factory=list)
    _server: _QuietServer | None = None
    _thread: threading.Thread | None = None
    _authorized: int = field(default=0, init=False, repr=False)
    _rejected: int = field(default=0, init=False, repr=False)

    @property
    def url(self) -> str:
        """
        The fixture's base URL.

        Returns:
            str: `http://127.0.0.1:<port>`.
        """
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def unauthorized_count(self) -> int:
        """
        How many requests were answered 401.

        Returns:
            int: The count, which a wrong password must keep at one per run.
        """
        return self._rejected

    def paths(self) -> list[str]:
        """
        Every path requested, in order.

        Returns:
            list[str]: The paths.
        """
        return [path for path, _ in self.requests]

    def start(self) -> KubeVirtFixture:
        """
        Serve on an ephemeral port in a background thread.

        Returns:
            KubeVirtFixture: This fixture.
        """
        fixture = self
        expected = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                """Silence the access log."""

            def _send(
                self, status: int, body: bytes, headers: dict[str, str] | None = None
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                auth = self.headers.get("Authorization")
                fixture.requests.append((path, auth is not None))
                if path in fixture.delays:
                    time.sleep(fixture.delays[path])
                if path != ROOT:
                    if auth != expected or (
                        fixture.reject_after is not None
                        and fixture._authorized >= fixture.reject_after
                    ):
                        fixture._rejected += 1
                        self._send(401, b'{"error":{"code":"Base.1.0.Unauthorized"}}')
                        return
                    fixture._authorized += 1
                if path in fixture.faults and fixture.fault_times.get(path, 1) > 0:
                    if path in fixture.fault_times:
                        fixture.fault_times[path] -= 1
                    self._send(fixture.faults[path], b"{}", fixture.fault_headers.get(path))
                    return
                if path in fixture.redirects:
                    self._send(302, b"{}", {"Location": fixture.redirects[path]})
                    return
                if path in fixture.raw:
                    self._send(200, fixture.raw[path])
                    return
                resource = fixture.resources.get(path)
                if resource is None:
                    self._send(404, b'{"error":{"code":"Base.1.0.ResourceMissing"}}')
                    return
                self._send(200, json.dumps(resource).encode())

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut the server down."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> KubeVirtFixture:
        """
        Start the fixture.

        Returns:
            KubeVirtFixture: This fixture, started.
        """
        return self.start()

    def __exit__(self, *exc: object) -> None:
        """Stop the fixture."""
        self.stop()
