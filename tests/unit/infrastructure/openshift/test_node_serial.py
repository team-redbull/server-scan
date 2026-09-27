"""`SshSerialReader` against a real (local) SSH server (ADR-0036).

A `MockTransport`-style fake would only prove this module calls asyncssh
correctly, not that asyncssh's own timeout/exit-status/concurrency behaviour
is what this reader assumes. asyncssh ships a full server implementation
for exactly this, so each test runs a throwaway `asyncssh.listen()` on
localhost with a generated key pair — no container, no real hardware.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import asyncssh
import pytest

from app.infrastructure.openshift.node_serial import SshSerialReader

ProcessHandler = Callable[[asyncssh.SSHServerProcess], Awaitable[None]]


class _Server:
    """A running throwaway SSH server, and a reader already pointed at it."""

    def __init__(self, port: int, key_path: str) -> None:
        """
        Args:
            port (int): The server's ephemeral listening port.
            key_path (str): The matching client private key's path.
        """
        self.port = port
        self.key_path = key_path

    def reader(self, *, connect_timeout: float = 5.0, concurrency: int = 10) -> SshSerialReader:
        """
        Build a reader that talks to this server.

        Args:
            connect_timeout (float): Passed straight through.
            concurrency (int): Passed straight through.

        Returns:
            SshSerialReader: Configured with this server's port and key.
        """
        return SshSerialReader(
            key_file=self.key_path,
            port=self.port,
            connect_timeout=connect_timeout,
            concurrency=concurrency,
        )


@pytest.fixture
async def ssh_server(
    tmp_path: Path,
) -> AsyncIterator[Callable[[ProcessHandler], Awaitable[_Server]]]:
    """
    A factory the test body calls with its own process handler.

    Args:
        tmp_path (Path): The pytest-provided temp directory, for the
            generated key pair.

    Yields:
        Callable[[ProcessHandler], Awaitable[_Server]]: Starts one server
            per call; every server started is closed on teardown.
    """
    listeners: list[asyncssh.SSHAcceptor] = []

    async def start(handler: ProcessHandler) -> _Server:
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        key_path = tmp_path / f"id_test_{len(listeners)}"
        client_key.write_private_key(str(key_path))
        key_path.chmod(0o600)
        authorized_keys_path = tmp_path / f"authorized_keys_{len(listeners)}"
        client_key.write_public_key(str(authorized_keys_path))
        host_key = asyncssh.generate_private_key("ssh-ed25519")

        listener = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[host_key],
            authorized_client_keys=str(authorized_keys_path),
            process_factory=handler,
        )
        listeners.append(listener)
        return _Server(listener.get_port(), str(key_path))

    yield start

    for listener in listeners:
        listener.close()
        await listener.wait_closed()


StartServer = Callable[[ProcessHandler], Awaitable[_Server]]


class TestSshSerialReader:
    """`read()` never raises — every failure mode returns `None`."""

    async def test_a_clean_read_returns_the_serial(self, ssh_server: StartServer) -> None:
        async def handler(process: asyncssh.SSHServerProcess) -> None:
            process.stdout.write("SN123\n")
            process.exit(0)

        server = await ssh_server(handler)

        assert await server.reader().read("127.0.0.1") == "SN123"

    async def test_a_non_zero_exit_is_unreadable(self, ssh_server: StartServer) -> None:
        async def handler(process: asyncssh.SSHServerProcess) -> None:
            process.stderr.write("permission denied\n")
            process.exit(1)

        server = await ssh_server(handler)

        assert await server.reader().read("127.0.0.1") is None

    async def test_a_placeholder_serial_is_treated_as_absent(self, ssh_server: StartServer) -> None:
        async def handler(process: asyncssh.SSHServerProcess) -> None:
            process.stdout.write("0123456789\n")
            process.exit(0)

        server = await ssh_server(handler)

        assert await server.reader().read("127.0.0.1") is None

    async def test_a_command_timeout_is_unreadable_not_raised(
        self, ssh_server: StartServer
    ) -> None:
        async def handler(process: asyncssh.SSHServerProcess) -> None:
            await asyncio.sleep(2)
            process.exit(0)

        server = await ssh_server(handler)

        assert await server.reader(connect_timeout=0.2).read("127.0.0.1") is None

    async def test_an_unreachable_port_is_unreadable_not_raised(self) -> None:
        reader = SshSerialReader(key_file="/nonexistent/key", port=1, connect_timeout=0.5)

        assert await reader.read("127.0.0.1") is None

    async def test_concurrency_is_capped(self, ssh_server: StartServer) -> None:
        in_flight = 0
        peak = 0

        async def handler(process: asyncssh.SSHServerProcess) -> None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.1)
            in_flight -= 1
            process.stdout.write("SN123\n")
            process.exit(0)

        server = await ssh_server(handler)
        reader = server.reader(concurrency=2)

        results = await asyncio.gather(*(reader.read("127.0.0.1") for _ in range(6)))

        assert results == ["SN123"] * 6
        assert peak <= 2
