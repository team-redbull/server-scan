"""SSH fallback for a node's hardware serial (ADR-0036).

`OpenShiftMembershipService` only ever calls this for a hostname that
missed, or that named more than one server (a duplicate name) — never for
every node on every run. Any failure (unreachable host, rejected auth, a
non-zero exit, a timeout) is swallowed and logged; the caller treats a
`None` exactly like a vendor collector's "could not read this run".

`asyncssh`, not the `ssh` binary: the API image is `ubi9-minimal` with no
`openssh-clients`, runs as an arbitrary non-root UID with no `/etc/passwd`
entry (which OpenSSH refuses to run under), and its root filesystem is
read-only, so there is nowhere to write `~/.ssh/known_hosts`. asyncssh is
pure Python, native `asyncio`, and needs no on-disk `HOME`.

Host keys are not verified (`known_hosts=None`), matching the operator's
own existing tooling for these nodes. Accepted risk on the internal node
network — see ADR-0036.
"""

from __future__ import annotations

import asyncio

import asyncssh
import structlog

from app.domain.services.normalize import is_placeholder_serial

logger = structlog.get_logger(__name__)

_SERIAL_COMMAND = "sudo cat /sys/class/dmi/id/product_serial"


class SshSerialReader:
    """Reads `/sys/class/dmi/id/product_serial` from a node over SSH."""

    def __init__(
        self,
        *,
        key_file: str,
        user: str = "core",
        port: int = 22,
        connect_timeout: float = 10.0,
        concurrency: int = 10,
    ) -> None:
        """
        Build the reader.

        Args:
            key_file (str): Path to the private key shared across every
                node in this cluster.
            user (str): The SSH user. RHCOS's `core` has default
                passwordless sudo, which `_SERIAL_COMMAND` relies on.
            port (int): The SSH port. Never anything but 22 in production;
                overridable so a test can point this at a throwaway server.
            connect_timeout (float): Seconds to wait for the SSH handshake
                and, separately, for the command to finish.
            concurrency (int): Maximum simultaneous connections, bounding
                how far a run with many unmatched hosts can fan out.
        """
        self._key_file = key_file
        self._user = user
        self._port = port
        self._timeout = connect_timeout
        self._semaphore = asyncio.Semaphore(concurrency)

    async def read(self, address: str) -> str | None:
        """
        Read one node's hardware serial.

        Args:
            address (str): The node's `InternalIP`.

        Returns:
            str | None: The raw serial, or `None` if it could not be read
                (connection, auth, timeout, non-zero exit) or came back as
                a known SMBIOS placeholder.
        """
        async with self._semaphore:
            try:
                async with asyncssh.connect(
                    address,
                    port=self._port,
                    username=self._user,
                    client_keys=[self._key_file],
                    known_hosts=None,
                    connect_timeout=self._timeout,
                ) as conn:
                    result = await conn.run(_SERIAL_COMMAND, check=False, timeout=self._timeout)
            except (asyncssh.Error, OSError, TimeoutError) as exc:
                logger.warning("openshift.serial_read_failed", address=address, error=str(exc))
                return None

        if result.exit_status != 0:
            logger.warning(
                "openshift.serial_read_failed",
                address=address,
                exit_status=result.exit_status,
            )
            return None

        raw = result.stdout.strip() if isinstance(result.stdout, str) else None
        if not raw or is_placeholder_serial(raw):
            return None
        return raw
