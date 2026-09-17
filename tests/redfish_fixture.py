"""A minimal Redfish service, on stdlib only, for hermetic tests.

Modelled on `sushy-tools`' `sushy-static` (Apache-2.0, ~118 lines): a
`BaseHTTPRequestHandler` mapping a URI onto a mockup tree. It is
hand-rolled rather than depended on for a specific reason — **neither
`sushy-tools` nor DMTF's own mockup server implements `SessionService`
at all**, so neither can exercise session login, logout, or a rejected
credential, which is the only genuinely non-trivial part of the client.

What that buys, and what no off-the-shelf option offers: a 401 without a
token, a session that expires mid-run, a resource that 404s while its
collection still advertises it, and a deliberately slow response.

See docs/adr/0016-redfish-standalone-collector.md.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

_SESSIONS = "/redfish/v1/SessionService/Sessions"


class _QuietServer(ThreadingHTTPServer):
    """A server that does not print a traceback when a client hangs up.

    The `delays` tests close the socket before this server answers, which
    is a passing timeout test rather than a failure.
    """

    def handle_error(self, request: object, client_address: object) -> None:
        """
        Ignore a client that disconnected; report anything else.

        Args:
            request (object): The socket the failure happened on.
            client_address (object): Its peer address.
        """
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, socket.timeout)):
            return
        super().handle_error(request, client_address)  # type: ignore


@dataclass
class RedfishFixture:
    """
    An in-process Redfish service.

    Attributes:
        resources (dict[str, Any]): Payloads by path. A path absent from
            this mapping returns 404, which is how a collection member
            that the collection still advertises is made to fail.
        username (str): The credential the service accepts.
        password (str): The credential the service accepts.
        faults (dict[str, int]): Path -> status code to return instead.
        delays (dict[str, float]): Path -> seconds to stall before
            answering, for timeout tests.
        require_auth (bool): Whether anything but the service root needs a
            token.
        session_valid (bool): Set False to make every issued token stop
            working, simulating a BMC that never recovers.
        expire_tokens_after (int | None): Kill exactly the Nth authorized
            GET's own token — one mid-run expiry a fresh login recovers
            from, unlike `session_valid` which never recovers.
        reject_login_after (int | None): Reject any session-creation POST
            after the Nth one succeeds — for a re-login that itself gets
            rejected.
        requests (list[tuple[str, str]]): Every (method, path) served.
    """

    resources: dict[str, Any] = field(default_factory=dict)
    username: str = "svc"
    password: str = "secret"
    faults: dict[str, int] = field(default_factory=dict)
    delays: dict[str, float] = field(default_factory=dict)
    require_auth: bool = True
    session_valid: bool = True
    expire_tokens_after: int | None = None
    reject_login_after: int | None = None
    requests: list[tuple[str, str]] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)
    _server: _QuietServer | None = None
    _thread: threading.Thread | None = None
    _authorized_count: int = field(default=0, init=False, repr=False)
    _login_count: int = field(default=0, init=False, repr=False)

    @property
    def port(self) -> int:
        """
        The port the fixture is listening on.

        Returns:
            int: An ephemeral port chosen at start.
        """
        assert self._server is not None
        return int(self._server.server_address[1])

    def start(self) -> RedfishFixture:
        """
        Bind an ephemeral port and serve in a background thread.

        Returns:
            RedfishFixture: This fixture, started.
        """
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                """Silence the default stderr access log."""

            def _authorized(self) -> bool:
                token = self.headers.get("X-Auth-Token")
                valid = bool(token) and token in fixture.tokens and fixture.session_valid
                if not valid:
                    return False
                fixture._authorized_count += 1
                if fixture._authorized_count == fixture.expire_tokens_after:
                    fixture.tokens.discard(str(token))
                    return False
                return True

            def _respond(self, status: int, body: dict[str, Any] | None = None) -> None:
                payload = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if status == 201 and body is not None and "@odata.id" in body:
                    self.send_header("Location", str(body["@odata.id"]))
                    self.send_header("X-Auth-Token", str(body.pop("_token", "")))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                fixture.requests.append(("GET", self.path))
                # Query strings ($select, $expand, ...) are recorded above but ignored below.
                path = urlsplit(self.path).path
                if path in fixture.delays:
                    time.sleep(fixture.delays[path])
                if path in fixture.faults:
                    self._respond(fixture.faults[path], {"error": {"code": "Base.1.0.Fault"}})
                    return
                # The service root is unauthenticated by specification,
                # which is what lets the collector probe a host for
                # conformance before presenting any credential.
                needs_auth = path.rstrip("/") != "/redfish/v1" and fixture.require_auth
                if needs_auth and not self._authorized():
                    self._respond(401, {"error": {"code": "Base.1.0.NoValidSession"}})
                    return
                resource = fixture.resources.get(path)
                if resource is None:
                    self._respond(404, {"error": {"code": "Base.1.0.ResourceMissing"}})
                    return
                self._respond(200, dict(resource))

            def do_POST(self) -> None:
                fixture.requests.append(("POST", self.path))
                if self.path in fixture.faults:
                    self._respond(fixture.faults[self.path], {})
                    return
                if not self.path.startswith(_SESSIONS):
                    self._respond(404, {})
                    return
                if (
                    fixture.reject_login_after is not None
                    and fixture._login_count >= fixture.reject_login_after
                ):
                    self._respond(401, {"error": {"code": "Base.1.0.InsufficientPrivilege"}})
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if (
                    body.get("UserName") != fixture.username
                    or body.get("Password") != fixture.password
                ):
                    self._respond(401, {"error": {"code": "Base.1.0.InsufficientPrivilege"}})
                    return
                fixture._login_count += 1
                token = uuid.uuid4().hex
                fixture.tokens.add(token)
                self._respond(
                    201,
                    {"@odata.id": f"{_SESSIONS}/{token[:8]}", "Id": token[:8], "_token": token},
                )

            def do_DELETE(self) -> None:
                fixture.requests.append(("DELETE", self.path))
                self._respond(200, {})

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut the server down and join its thread."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> RedfishFixture:
        """
        Start the fixture.

        Returns:
            RedfishFixture: This fixture, started.
        """
        return self.start()

    def __exit__(self, *exc: object) -> None:
        """Stop the fixture."""
        self.stop()

    def paths_served(self) -> list[str]:
        """
        Every path requested so far.

        Returns:
            list[str]: Request paths in order.
        """
        return [path for _, path in self.requests]


def minimal_service(**overrides: Any) -> dict[str, Any]:
    """
    A conformant single-server Redfish service.

    Shapes and property names follow the DMTF schema bundle — see
    docs/adr/0016 for what each was verified against.

    Args:
        **overrides: Resource paths to replace or add.

    Returns:
        dict[str, Any]: Payloads by path, ready for `RedfishFixture`.
    """
    resources: dict[str, Any] = {
        "/redfish/v1/": {
            "@odata.id": "/redfish/v1/",
            "@odata.type": "#ServiceRoot.v1_5_0.ServiceRoot",
            "Id": "RootService",
            "Name": "Root Service",
            "RedfishVersion": "1.15.0",
            "Systems": {"@odata.id": "/redfish/v1/Systems"},
            "Links": {"Sessions": {"@odata.id": _SESSIONS}},
        },
        "/redfish/v1/Systems": {
            "@odata.id": "/redfish/v1/Systems",
            "Name": "Systems Collection",
            "Members": [{"@odata.id": "/redfish/v1/Systems/1"}],
            # Deliberately wrong: the collector must ignore this and
            # follow Members/nextLink, since the count is the total across
            # every page rather than this page's length.
            "Members@odata.count": 99,
        },
        "/redfish/v1/Systems/1": {
            "@odata.id": "/redfish/v1/Systems/1",
            "@odata.type": "#ComputerSystem.v1_22_0.ComputerSystem",
            "Id": "1",
            "Name": "System",
            "HostName": "ocp4-prod-tlv-infra-01",
            "Manufacturer": "Dell Inc.",
            "Model": "PowerEdge R660",
            "SerialNumber": "FCH2201V0AB",
            "UUID": "11111111-2222-3333-4444-555555555555",
            "ProcessorSummary": {"Count": 2, "CoreCount": 64, "LogicalProcessorCount": 128},
            "MemorySummary": {"TotalSystemMemoryGiB": 512.0},
            "Processors": {"@odata.id": "/redfish/v1/Systems/1/Processors"},
            "Storage": {"@odata.id": "/redfish/v1/Systems/1/Storage"},
            "Memory": {"@odata.id": "/redfish/v1/Systems/1/Memory"},
            "EthernetInterfaces": {"@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces"},
            "Links": {"ManagedBy": [{"@odata.id": "/redfish/v1/Managers/bmc"}]},
        },
        "/redfish/v1/Systems/1/Memory": {
            "@odata.id": "/redfish/v1/Systems/1/Memory",
            "Members": [
                {"@odata.id": "/redfish/v1/Systems/1/Memory/DIMM_A1"},
                {"@odata.id": "/redfish/v1/Systems/1/Memory/DIMM_A2"},
                {"@odata.id": "/redfish/v1/Systems/1/Memory/DIMM_B1"},
            ],
        },
        "/redfish/v1/Systems/1/Memory/DIMM_A1": {
            "@odata.id": "/redfish/v1/Systems/1/Memory/DIMM_A1",
            "@odata.type": "#Memory.v1_16_0.Memory",
            "Id": "DIMM_A1",
            "Name": "DIMM_A1",
            "CapacityMiB": 65536,
            "MemoryDeviceType": "DDR5",
            "Status": {"State": "Enabled", "Health": "OK"},
        },
        "/redfish/v1/Systems/1/Memory/DIMM_A2": {
            "@odata.id": "/redfish/v1/Systems/1/Memory/DIMM_A2",
            "@odata.type": "#Memory.v1_16_0.Memory",
            "Id": "DIMM_A2",
            "Name": "DIMM_A2",
            "CapacityMiB": 65536,
            "MemoryDeviceType": "DDR5",
            "Status": {"State": "Enabled", "Health": "OK"},
        },
        # An empty DIMM slot with a deliberately stale CapacityMiB, to
        # prove exclusion follows `Status.State == "Absent"` and not a
        # missing capacity (the empty-bay signal ADR-0016 confirmed).
        "/redfish/v1/Systems/1/Memory/DIMM_B1": {
            "@odata.id": "/redfish/v1/Systems/1/Memory/DIMM_B1",
            "@odata.type": "#Memory.v1_16_0.Memory",
            "Id": "DIMM_B1",
            "Name": "DIMM_B1",
            "CapacityMiB": 32768,
            "Status": {"State": "Absent"},
        },
        "/redfish/v1/Systems/1/Processors": {
            "@odata.id": "/redfish/v1/Systems/1/Processors",
            "Members": [
                {"@odata.id": "/redfish/v1/Systems/1/Processors/CPU1"},
                {"@odata.id": "/redfish/v1/Systems/1/Processors/GPU1"},
            ],
        },
        "/redfish/v1/Systems/1/Processors/CPU1": {
            "@odata.id": "/redfish/v1/Systems/1/Processors/CPU1",
            "@odata.type": "#Processor.v1_22_0.Processor",
            "Id": "CPU1",
            "Name": "CPU1",
            "ProcessorType": "CPU",
            "Model": "Xeon Gold 6338",
            "TotalCores": 32,
            "TotalThreads": 64,
        },
        "/redfish/v1/Systems/1/Processors/GPU1": {
            "@odata.id": "/redfish/v1/Systems/1/Processors/GPU1",
            "@odata.type": "#Processor.v1_22_0.Processor",
            "Id": "GPU1",
            "Name": "GPU1",
            "ProcessorType": "GPU",
            "Manufacturer": "Nvidia(R) Corporation",
            "Model": "Nvidia(R) TU102",
            "MemorySummary": {"TotalMemorySizeMiB": 11264, "ECCModeEnabled": True},
            "ProcessorMemory": [{"CapacityMiB": 11264, "MemoryType": "HBM2"}],
            "Metrics": {"@odata.id": "/redfish/v1/Systems/1/Processors/GPU1/ProcessorMetrics"},
            "EnvironmentMetrics": {
                "@odata.id": "/redfish/v1/Systems/1/Processors/GPU1/EnvironmentMetrics"
            },
            "Status": {"State": "Enabled", "Health": "OK"},
        },
        "/redfish/v1/Systems/1/Processors/GPU1/ProcessorMetrics": {
            "@odata.id": "/redfish/v1/Systems/1/Processors/GPU1/ProcessorMetrics",
            "@odata.type": "#ProcessorMetrics.v1_6_0.ProcessorMetrics",
            "Id": "ProcessorMetrics",
            "Name": "GPU1 Metrics",
            "CorrectableCoreErrorCount": 3,
            "UncorrectableCoreErrorCount": 0,
            "CorrectableOtherErrorCount": 1,
            "UncorrectableOtherErrorCount": 0,
        },
        "/redfish/v1/Systems/1/Processors/GPU1/EnvironmentMetrics": {
            "@odata.id": "/redfish/v1/Systems/1/Processors/GPU1/EnvironmentMetrics",
            "@odata.type": "#EnvironmentMetrics.v1_5_0.EnvironmentMetrics",
            "Id": "EnvironmentMetrics",
            "Name": "GPU1 Environment",
            "TemperatureCelsius": {"Reading": 62.5},
            "PowerWatts": {"Reading": 310.0},
        },
        "/redfish/v1/Systems/1/Storage": {
            "@odata.id": "/redfish/v1/Systems/1/Storage",
            "Members": [{"@odata.id": "/redfish/v1/Systems/1/Storage/RAID"}],
        },
        "/redfish/v1/Systems/1/Storage/RAID": {
            "@odata.id": "/redfish/v1/Systems/1/Storage/RAID",
            "@odata.type": "#Storage.v1_15_0.Storage",
            "Id": "RAID",
            "Name": "RAID Controller",
            # An inline array of links, not a sub-collection — and served
            # under Chassis, which is the form DMTF's own mockup uses.
            "Drives": [{"@odata.id": "/redfish/v1/Chassis/1/Drives/0"}],
        },
        "/redfish/v1/Chassis/1/Drives/0": {
            "@odata.id": "/redfish/v1/Chassis/1/Drives/0",
            "@odata.type": "#Drive.v1_15_0.Drive",
            "Id": "0",
            "Name": "Drive 0",
            "Model": "MZ7LH3T8",
            "SerialNumber": "DRIVE-1",
            # NVMe is expressed through Protocol; MediaType has no NVMe
            # member, so reading MediaType alone reports this as an SSD.
            "MediaType": "SSD",
            "Protocol": "NVMe",
            "CapacityBytes": 3840755982336,
            "Status": {"State": "Enabled", "Health": "OK"},
        },
        "/redfish/v1/Systems/1/EthernetInterfaces": {
            "@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces",
            "Members": [{"@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces/nic0"}],
        },
        "/redfish/v1/Systems/1/EthernetInterfaces/nic0": {
            "@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces/nic0",
            "@odata.type": "#EthernetInterface.v1_12_0.EthernetInterface",
            "Id": "nic0",
            "Name": "NIC 0",
            "MACAddress": "00:00:5e:00:53:01",
        },
        "/redfish/v1/Managers/bmc": {
            "@odata.id": "/redfish/v1/Managers/bmc",
            "@odata.type": "#Manager.v1_15_0.Manager",
            "Id": "bmc",
            "Name": "Manager",
            "ManagerType": "BMC",
            "EthernetInterfaces": {"@odata.id": "/redfish/v1/Managers/bmc/EthernetInterfaces"},
        },
        "/redfish/v1/Managers/bmc/EthernetInterfaces": {
            "@odata.id": "/redfish/v1/Managers/bmc/EthernetInterfaces",
            "Members": [{"@odata.id": "/redfish/v1/Managers/bmc/EthernetInterfaces/eth0"}],
        },
        "/redfish/v1/Managers/bmc/EthernetInterfaces/eth0": {
            "@odata.id": "/redfish/v1/Managers/bmc/EthernetInterfaces/eth0",
            "@odata.type": "#EthernetInterface.v1_12_0.EthernetInterface",
            "Id": "eth0",
            "Name": "BMC NIC",
            "PermanentMACAddress": "00:00:5e:00:53:99",
        },
    }
    resources.update(overrides)
    return resources
