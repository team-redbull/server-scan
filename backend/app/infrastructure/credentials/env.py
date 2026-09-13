"""Resolves a manager type's connection details from application settings.

Settings are environment-sourced (`app.config.settings.Settings`, prefix
`INVENTORY_`), so in Kubernetes these arrive from a `Secret` via
`envFrom` — the values live in the Helm chart's `values.yaml`, get
rendered into a Secret, and never appear in the pod spec itself.

The mappings below are explicit rather than derived from the enum member
name (`ManagerType.UCS_MANAGER` -> `ucs_manager_*`). A convention would
work today and silently break the day a member is renamed: the rename
would change which environment variables a deployment must set, with
nothing failing until a collector runs against a live domain and reports
"not configured". Naming the fields makes that a type error instead.

**A login and an endpoint are separate questions**, which is why there
are two maps rather than one three-tuple. UCS Manager has a login but no
configured endpoint: it is never pointed at directly any more, it is
reached once per registered domain by the UCS Central collector, which
gets each domain's address from Central itself
(`ComputeSystem.address`). A single `_SETTINGS_FIELDS` tuple could only
express that with an empty-string or `None` endpoint field, which would
make `resolve()` fail with "set INVENTORY_UCS_MANAGER_IP" — naming a
variable that does not exist. Splitting the maps makes the absence a
fact of the type instead of a runtime surprise.
"""

from __future__ import annotations

from pydantic import SecretStr

from app.config.settings import Settings
from app.domain.enums import ManagerType
from app.domain.ports.credentials import (
    ManagerConnection,
    ManagerNotConfiguredError,
)

# Always a pair; Intersight's is a key id and a PEM, named so (ADR-0012).
_LOGIN_FIELDS: dict[ManagerType, tuple[str, str]] = {
    ManagerType.UCS_MANAGER: ("ucs_manager_username", "ucs_manager_password"),
    ManagerType.UCS_CENTRAL: ("ucs_central_username", "ucs_central_password"),
    ManagerType.ONEVIEW: ("oneview_username", "oneview_password"),
    ManagerType.OPENMANAGE: ("ome_username", "ome_password"),
    ManagerType.INTERSIGHT: ("intersight_api_key_id", "intersight_api_key_pem"),
    ManagerType.REDFISH_STANDALONE: ("redfish_username", "redfish_password"),
}

# `UCS_MANAGER` and `REDFISH_STANDALONE` are absent on purpose (ADR-0012).
_ENDPOINT_FIELD: dict[ManagerType, str] = {
    ManagerType.UCS_CENTRAL: "ucs_central_ip",
    ManagerType.ONEVIEW: "oneview_ip",
    ManagerType.OPENMANAGE: "ome_ip",
    ManagerType.INTERSIGHT: "intersight_ip",
}


def _env_var(field: str) -> str:
    """
    The environment variable a settings field reads, for error messages.

    Args:
        field (str): A `Settings` field name.

    Returns:
        str: The `INVENTORY_`-prefixed variable name an operator sets.
    """
    return f"INVENTORY_{field.upper()}"


def _read(settings: Settings, name: str) -> str:
    """
    Read one settings field as a plain string, unwrapping `SecretStr`.

    The one place that unwraps: `str(SecretStr)` is the mask, and a naive
    `str(getattr(...))` would log in with `"**********"` (ADR-0012).

    Args:
        settings (Settings): The settings instance to read from.
        name (str): The field name (an endpoint, username, password, or
            PEM field — this function works for both plain `str` and
            `SecretStr` fields).

    Returns:
        str: The field's value, stripped of surrounding whitespace, or
            `""` if the field is unset or absent.
    """
    value = getattr(settings, name, "")
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return str(value or "").strip()


class EnvConnectionResolver:
    """Implements `app.domain.ports.credentials.CredentialResolver`."""

    def __init__(self, settings: Settings) -> None:
        """
        Build the resolver.

        Args:
            settings (Settings): The process-wide settings to resolve
                every manager type's connection from.
        """
        self._settings = settings

    def resolve(self, manager_type: ManagerType) -> ManagerConnection:
        """
        Resolve one manager type's endpoint and login from settings.

        Args:
            manager_type (ManagerType): Which collector to resolve for.

        Returns:
            ManagerConnection: The endpoint, username and password to
                connect with.

        Raises:
            ManagerNotConfiguredError: If the type has no connection
                shape at all, or any required variable is unset.
        """
        if manager_type is ManagerType.UCS_MANAGER:
            raise ManagerNotConfiguredError(
                "UCS_MANAGER has no endpoint of its own to configure — each domain is "
                "reached through UCS Central, which reports its address. Configure "
                "INVENTORY_UCS_CENTRAL_IP/_USERNAME/_PASSWORD plus the fleet-wide "
                "INVENTORY_UCS_MANAGER_USERNAME/_PASSWORD, and collect with "
                "--manager-type UCS_CENTRAL."
            )
        if manager_type is ManagerType.REDFISH_STANDALONE:
            raise ManagerNotConfiguredError(
                "REDFISH_STANDALONE has no endpoint of its own to configure — it reaches one "
                "BMC at a time from an inventory file. Set INVENTORY_REDFISH_INVENTORY_FILE to "
                "that file, give each host a credential in "
                "INVENTORY_REDFISH_CREDENTIALS_FILE, and/or set INVENTORY_REDFISH_USERNAME and "
                "INVENTORY_REDFISH_PASSWORD as the fleet-wide fallback login."
            )
        endpoint_field = _ENDPOINT_FIELD.get(manager_type)
        login_fields = _LOGIN_FIELDS.get(manager_type)
        if endpoint_field is None or login_fields is None:
            raise ManagerNotConfiguredError(
                f"{manager_type.value} has no connection configuration defined."
            )

        username_field, password_field = login_fields
        fields = (endpoint_field, username_field, password_field)
        values = {name: _read(self._settings, name) for name in fields}

        missing = [_env_var(name) for name, value in values.items() if not value]
        if missing:
            raise ManagerNotConfiguredError(
                f"{manager_type.value} is not configured — set {', '.join(sorted(missing))}."
            )

        return ManagerConnection(
            endpoint=values[endpoint_field],
            username=values[username_field],
            password=values[password_field],
        )


def resolve_login(settings: Settings, manager_type: ManagerType) -> tuple[str, str]:
    """
    A manager type's `(username, password)` without its endpoint.

    For the UCS Central collector, which logs into each domain at the
    address Central reports with this one fleet-wide account.

    Args:
        settings (Settings): The settings instance to read from.
        manager_type (ManagerType): The type whose login is wanted.

    Returns:
        tuple[str, str]: The username and password.

    Raises:
        ManagerNotConfiguredError: Naming the environment variables to set.
    """
    login_fields = _LOGIN_FIELDS.get(manager_type)
    if login_fields is None:
        raise ManagerNotConfiguredError(
            f"{manager_type.value} has no connection configuration defined."
        )
    username_field, password_field = login_fields
    username = _read(settings, username_field)
    password = _read(settings, password_field)

    missing = [
        _env_var(field)
        for field, value in ((username_field, username), (password_field, password))
        if not value
    ]
    if missing:
        raise ManagerNotConfiguredError(
            f"{manager_type.value} has no login configured — set {', '.join(sorted(missing))}."
        )
    return username, password


def configured_manager_types(settings: Settings) -> list[ManagerType]:
    """
    Every manager type this deployment can be pointed at directly.

    Iterates `_ENDPOINT_FIELD`, so `UCS_MANAGER` can never appear — it is
    collected only as part of a `UCS_CENTRAL` run.

    Args:
        settings (Settings): The settings instance to read from.

    Returns:
        list[ManagerType]: The types whose endpoint and login are both set.
    """
    resolver = EnvConnectionResolver(settings)
    configured: list[ManagerType] = []
    for manager_type in _ENDPOINT_FIELD:
        try:
            resolver.resolve(manager_type)
        except ManagerNotConfiguredError:
            continue
        configured.append(manager_type)
    return configured
