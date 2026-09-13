"""The standalone Redfish collector's fleet list and credential chain.

No network and no mapping: this module turns an operator's TOML into
validated `RedfishTarget`s, or fails the run before a single packet is
sent. That ordering is the point — a fan-out collector must not discover
a configuration error on host 380 of 400, and a credential must never be
presented to a host that a typo put it in front of.

See docs/adr/0016-redfish-standalone-collector.md.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.ports.credentials import ManagerNotConfiguredError

_SUPPORTED_VERSION = 1


@dataclass(frozen=True, slots=True)
class RedfishCredential:
    """
    One BMC login, carrying the name it was resolved through.

    Attributes:
        name (str): The credential's name in the inventory, used in every
            log line and error message in place of the secret.
        username (str): Login user.
        password (str): Login password.
    """

    name: str
    username: str
    password: str

    def __repr__(self) -> str:
        """
        Redact the password, so no log line, traceback or debugger can print it.

        Returns:
            str: The credential with its password masked.
        """
        return f"RedfishCredential(name={self.name!r}, username={self.username!r}, password='***')"


@dataclass(frozen=True, slots=True)
class RedfishTarget:
    """
    One BMC to collect from, with everything needed to reach it.

    Attributes:
        host (str): Bare hostname or IP, never a URL. Also the identity
            this target is reported under.
        port (int): HTTPS port.
        credential (RedfishCredential): Resolved login.
        verify_tls (bool): False only where the operator opted out
            explicitly and gave a reason.
        verify_tls_reason (str | None): Why verification is off, required
            whenever it is.
        ca_bundle (str | None): PEM bundle trusted in addition to the
            system store.
        name (str | None): Operator-supplied server name, preferred over
            whatever the BMC reports. See the module docstring's ADR.
    """

    host: str
    port: int
    credential: RedfishCredential
    verify_tls: bool
    verify_tls_reason: str | None
    ca_bundle: str | None
    name: str | None

    @property
    def base_url(self) -> str:
        """
        The origin every request for this target is made against.

        Returns:
            str: e.g. `"https://10.20.30.41"` or `"https://bmc.example:8443"`.
        """
        return f"https://{self.host}" if self.port == 443 else f"https://{self.host}:{self.port}"


class InventoryError(ManagerNotConfiguredError):
    """The inventory could not be loaded, or does not describe a run.

    A `ManagerNotConfiguredError`, so `tools.run_collector` exits 2 naming what to fix.
    """


def _normalize_host(raw: object, *, source: str) -> str:
    """
    Reduce an operator-written address to the bare host this collector connects to.

    Args:
        raw (object): The `host` value as written.
        source (str): File the entry came from, for the error message.

    Returns:
        str: Lowercased host with no scheme, path or credentials.

    Raises:
        InventoryError: If the value is empty, carries a URL scheme or
            path, or embeds credentials.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise InventoryError(f"{source}: every [[hosts]] entry needs a non-empty `host`.")
    host = raw.strip().lower()
    if "@" in host:
        # Rejected, not stripped: stripped, it would still reach `bmc_address_raw`.
        raise InventoryError(
            f"{source}: host {raw!r} embeds credentials. Put the login in the credentials "
            "file and reference it by name."
        )
    if "://" in host or "/" in host:
        raise InventoryError(
            f"{source}: host {raw!r} must be a bare hostname or IP, not a URL — "
            "the collector builds the URL itself."
        )
    return host


def _load_toml(path: Path) -> dict[str, Any]:
    """
    Read one TOML file.

    Args:
        path (Path): File to read.

    Returns:
        dict[str, Any]: Its parsed contents.

    Raises:
        InventoryError: If the file is unreadable or malformed.
    """
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise InventoryError(f"{path}: cannot be read — {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise InventoryError(f"{path}: is not valid TOML — {exc}") from exc


def _inventory_documents(source: Path) -> list[tuple[Path, dict[str, Any]]]:
    """
    Every inventory document behind a configured path.

    A directory is accepted so a large estate can be sharded per site
    without a format change.

    Args:
        source (Path): File or directory named by
            `INVENTORY_REDFISH_INVENTORY_FILE`.

    Returns:
        list[tuple[Path, dict[str, Any]]]: Each file with its contents.

    Raises:
        InventoryError: If the path does not exist, or a directory holds
            no `*.toml`.
    """
    if not source.exists():
        raise InventoryError(
            f"{source}: does not exist. Set INVENTORY_REDFISH_INVENTORY_FILE to a readable "
            "TOML inventory file or a directory of them."
        )
    if source.is_dir():
        files = sorted(source.glob("*.toml"))
        if not files:
            raise InventoryError(f"{source}: is a directory containing no *.toml inventory files.")
        return [(path, _load_toml(path)) for path in files]
    return [(source, _load_toml(source))]


def _load_credentials(path: Path | None) -> dict[str, RedfishCredential]:
    """
    Read the named-credential file, if one is configured.

    Args:
        path (Path | None): File named by
            `INVENTORY_REDFISH_CREDENTIALS_FILE`, or None.

    Returns:
        dict[str, RedfishCredential]: Credentials by name, empty when no
            file is configured.

    Raises:
        InventoryError: If the file is unreadable, malformed, or holds an
            entry missing a username or password.
    """
    if path is None:
        return {}
    document = _load_toml(path)
    raw = document.get("credentials", {})
    if not isinstance(raw, dict):
        raise InventoryError(f"{path}: `credentials` must be a table of named logins.")

    credentials: dict[str, RedfishCredential] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise InventoryError(f"{path}: credential {name!r} must be a table.")
        username = str(entry.get("username", "") or "").strip()
        password = str(entry.get("password", "") or "")
        # A blank password reaches the BMC as a real, lockout-counted login.
        if not username or not password:
            raise InventoryError(
                f"{path}: credential {name!r} needs both `username` and `password`."
            )
        credentials[name] = RedfishCredential(name=name, username=username, password=password)
    return credentials


def _setting(
    entry: dict[str, Any], group: dict[str, Any], defaults: dict[str, Any], key: str
) -> Any:
    """
    Resolve one setting down the host -> group -> defaults ladder.

    Args:
        entry (dict[str, Any]): The host's own table.
        group (dict[str, Any]): Its group's table, empty if it has none.
        defaults (dict[str, Any]): The inventory's `[defaults]` table.
        key (str): Setting to resolve.

    Returns:
        Any: The first value found, or None.
    """
    for source in (entry, group, defaults):
        if key in source:
            return source[key]
    return None


def load_targets(
    *,
    inventory_path: str,
    credentials_path: str,
    fallback_login: tuple[str, str] | None,
    ca_bundle: str | None = None,
) -> list[RedfishTarget]:
    """
    Parse and fully validate the fleet list, before any connection is opened.

    Args:
        inventory_path (str): `INVENTORY_REDFISH_INVENTORY_FILE`.
        credentials_path (str): `INVENTORY_REDFISH_CREDENTIALS_FILE`, or
            empty when the fleet shares one login.
        fallback_login (tuple[str, str] | None): The fleet-wide
            `(username, password)`, or None if unset.
        ca_bundle (str | None): Default CA bundle for hosts that name
            none.

    Returns:
        list[RedfishTarget]: One entry per host, in file order.

    Raises:
        InventoryError: On any unreadable file, unknown group, undefined
            credential, duplicate host, missing opt-out reason, or an
            inventory that yields no hosts at all.
    """
    if not inventory_path.strip():
        raise InventoryError(
            "REDFISH_STANDALONE has no inventory — set INVENTORY_REDFISH_INVENTORY_FILE to a "
            "TOML file or directory listing the BMCs to collect from."
        )

    credentials = _load_credentials(Path(credentials_path) if credentials_path.strip() else None)
    targets: list[RedfishTarget] = []
    seen: dict[str, Path] = {}

    for path, document in _inventory_documents(Path(inventory_path)):
        version = document.get("version", _SUPPORTED_VERSION)
        if version != _SUPPORTED_VERSION:
            raise InventoryError(
                f"{path}: inventory `version = {version}` is not supported "
                f"(this build reads version {_SUPPORTED_VERSION})."
            )

        defaults = document.get("defaults", {}) or {}
        groups = document.get("groups", {}) or {}
        hosts = document.get("hosts", []) or []
        if not isinstance(hosts, list):
            raise InventoryError(f"{path}: `hosts` must be a list of [[hosts]] tables.")

        for entry in hosts:
            if not isinstance(entry, dict):
                raise InventoryError(f"{path}: every entry under `hosts` must be a table.")
            host = _normalize_host(entry.get("host"), source=str(path))
            if host in seen:
                raise InventoryError(
                    f"{path}: host {host!r} is already defined in {seen[host]}. A duplicate "
                    "would silently lose one entry's settings."
                )
            seen[host] = path

            group_name = entry.get("group")
            if group_name is not None and group_name not in groups:
                # Fail closed: the default credential must not reach a typo'd group.
                known = ", ".join(sorted(groups)) or "none defined"
                raise InventoryError(
                    f"{path}: host {host!r} names group {group_name!r}, which is not "
                    f"defined. Known groups: {known}."
                )
            group = groups.get(group_name, {}) if group_name is not None else {}

            credential = _resolve_credential(
                entry=entry,
                group=group,
                defaults=defaults,
                host=host,
                credentials=credentials,
                fallback_login=fallback_login,
                source=str(path),
                credentials_path=credentials_path,
            )

            verify_tls = _setting(entry, group, defaults, "verify_tls")
            verify_tls = True if verify_tls is None else bool(verify_tls)
            reason = _setting(entry, group, defaults, "verify_tls_reason")
            if not verify_tls and not reason:
                raise InventoryError(
                    f"{path}: host {host!r} disables TLS verification without a "
                    "`verify_tls_reason`. The collector sends this BMC's password in the "
                    "clear, so the reason is required and is what makes the exception "
                    "visible in review."
                )

            port = _setting(entry, group, defaults, "port")
            targets.append(
                RedfishTarget(
                    host=host,
                    port=int(port) if port is not None else 443,
                    credential=credential,
                    verify_tls=verify_tls,
                    verify_tls_reason=str(reason) if reason else None,
                    ca_bundle=_setting(entry, group, defaults, "ca_bundle") or ca_bundle,
                    name=str(entry["name"]) if entry.get("name") else None,
                )
            )

    if not targets:
        raise InventoryError(
            f"{inventory_path}: parsed 0 hosts. An empty inventory collects nothing and is "
            "almost always a ConfigMap that failed to mount — check the volume, not the file."
        )
    return targets


def _resolve_credential(
    *,
    entry: dict[str, Any],
    group: dict[str, Any],
    defaults: dict[str, Any],
    host: str,
    credentials: dict[str, RedfishCredential],
    fallback_login: tuple[str, str] | None,
    source: str,
    credentials_path: str,
) -> RedfishCredential:
    """
    Resolve one host's login.

    Its own `credential`, one named after the host, its group's,
    `[defaults]`, then the fleet-wide login (ADR-0016).

    Args:
        entry (dict[str, Any]): The host's own table.
        group (dict[str, Any]): Its group's table.
        defaults (dict[str, Any]): The inventory's `[defaults]` table.
        host (str): Normalized host, also tried as a credential name.
        credentials (dict[str, RedfishCredential]): Named credentials.
        fallback_login (tuple[str, str] | None): The fleet-wide login.
        source (str): Inventory file, for error messages.
        credentials_path (str): Credentials file, for error messages.

    Returns:
        RedfishCredential: The login to present to this host.

    Raises:
        InventoryError: If a named credential is undefined, or nothing
            resolves.
    """
    explicit = entry.get("credential") or group.get("credential") or defaults.get("credential")
    if explicit is not None:
        name = str(explicit)
        if name not in credentials:
            where = credentials_path or "INVENTORY_REDFISH_CREDENTIALS_FILE"
            raise InventoryError(
                f"{source}: host {host!r} references credential {name!r}, which is not "
                f"defined in {where}."
            )
        return credentials[name]

    if host in credentials:
        return credentials[host]

    if fallback_login is not None:
        return RedfishCredential(
            name="default", username=fallback_login[0], password=fallback_login[1]
        )

    raise InventoryError(
        f"{source}: host {host!r} has no credential. Add a [credentials.{host}] entry to "
        "INVENTORY_REDFISH_CREDENTIALS_FILE, give the host an explicit `credential`, or set "
        "INVENTORY_REDFISH_USERNAME and INVENTORY_REDFISH_PASSWORD as the fleet-wide login."
    )
