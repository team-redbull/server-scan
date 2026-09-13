"""Logic shared by the Cisco collectors, which describe the same hardware with the same vocabulary.

`ucsmsdk` (UCS Manager, one domain), `ucscsdk` (UCS Central, every
registered domain) and the Intersight REST API. See docs/cisco-
collectors.md, "Shared object model and DN joins".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_EQUIPPED_PREFIX = "equipped"
_NON_PRIMARY_PRESENCE = frozenset({"equipped-slave", "equipped-not-primary"})

TEMPLATE_TYPES = frozenset({"initial-template", "updating-template"})

_NON_BMC_ACCESS = frozenset({"in-band", "internal", "virtual"})

# Complete against the SDK enum and live Intersight data; `indeterminate` is
# unmapped on purpose — ADR-0009 and ADR-0017, "2026-09-07" updates.
_OPER_STATE_MAP = {
    "operable": "UP",
    "ok": "UP",
    "up": "UP",
    "link-up": "UP",
    "admin-down": "DISABLED",
    "disabled": "DISABLED",
    "no-license": "DISABLED",
    "inoperable": "DOWN",
    "down": "DOWN",
    "link-down": "DOWN",
    "failed": "DOWN",
    "sfp-not-present": "DOWN",
    "error-disabled": "DOWN",
    "hardware-failure": "DOWN",
    "software-failure": "DOWN",
    "udld-aggr-down": "DOWN",
}

_ADMIN_STATE_MAP = {"enabled": "ENABLED", "disabled": "DISABLED"}


def normalize_oper_state(value: object) -> str:
    """
    Map Cisco's operational-state vocabulary onto the platform's.

    Args:
        value (object): The raw `operState`/`OperState` value, in
            whatever form the SDK or the REST API reported it.

    Returns:
        str: UP, DOWN, DISABLED, or UNKNOWN for an unrecognized value.
    """
    return _OPER_STATE_MAP.get(str(value or "").lower(), "UNKNOWN")


def normalize_admin_state(value: object) -> str:
    """
    Map Cisco's administrative-state vocabulary onto the platform's.

    Args:
        value (object): The raw `adminState`/`AdminState` value.

    Returns:
        str: ENABLED, DISABLED, or UNKNOWN for an unrecognized value.
    """
    return _ADMIN_STATE_MAP.get(str(value or "").lower(), "UNKNOWN")


def is_equipped(server_mo: Any) -> bool:
    """
    Report whether a compute MO is a physically-present, independently addressable server.

    See docs/cisco-collectors.md, "Shared object model and DN joins".

    Args:
        server_mo (Any): A `computeBlade` or `computeRackUnit` managed
            object from either SDK.

    Returns:
        bool: True if the server is equipped and is not the secondary half
            of a multi-node server.
    """
    raw = getattr(server_mo, "presence", None)
    if not raw:
        return False
    presence = str(raw)
    if presence in _NON_PRIMARY_PRESENCE:
        return False
    return presence.startswith(_EQUIPPED_PREFIX)


def group_by_owning_server_dn(
    mos: Iterable[Any], *, server_dns: Iterable[str]
) -> dict[str, list[Any]]:
    """
    Bucket descendant managed objects under the compute unit each lives below.

    Drops anything owned by something other than a server. See
    docs/cisco-collectors.md, "Shared object model and DN joins".

    Args:
        mos (Iterable[Any]): Descendant managed objects, typically the whole
            result of one domain-wide class query.
        server_dns (Iterable[str]): The compute-unit DNs to group under.

    Returns:
        dict[str, list[Any]]: One entry per DN in `server_dns`, each holding
            the MOs beneath it. DNs with no descendants map to an empty list.
    """
    known = set(server_dns)
    grouped: dict[str, list[Any]] = {dn: [] for dn in server_dns}
    for mo in mos:
        dn = getattr(mo, "dn", None)
        if not dn:
            continue
        ancestor, _, _ = str(dn).rpartition("/")
        while ancestor:
            if ancestor in known:
                grouped[ancestor].append(mo)
                break
            ancestor, _, _ = ancestor.rpartition("/")
    return grouped


def bmc_interface(mgmt_ifs: list[Any], *, server_dn: str) -> Any | None:
    """
    Pick a server's own CIMC management interface out of every `mgmtIf` under its DN.

    See docs/cisco-collectors.md, "BMC and management interface selection".

    Args:
        mgmt_ifs (list[Any]): Every `mgmtIf` found beneath `server_dn`,
            typically one bucket from `group_by_owning_server_dn`.
        server_dn (str): The owning compute unit's distinguished name.

    Returns:
        Any | None: The CIMC interface, or None if the server exposes none.
    """
    own_controller_prefix = f"{server_dn}/mgmt/"
    own = [
        mo
        for mo in mgmt_ifs
        if str(getattr(mo, "dn", "")).startswith(own_controller_prefix)
        and str(getattr(mo, "access", "") or "").lower() not in _NON_BMC_ACCESS
    ]
    if not own:
        return None
    return next(
        (mo for mo in own if getattr(mo, "access", None) == "out-of-band"),
        own[0],
    )


def management_ip_by_parent_dn(ip_addrs: Iterable[Any]) -> dict[str, Any]:
    """
    Index every real management IP assignment by the DN of the object it hangs off of.

    Both schema-valid parents are indexed; only the profile DN was populated on
    real hardware — docs/cisco-collectors.md, "BMC and management interface selection".

    Args:
        ip_addrs (Iterable[Any]): Every `vnicIpV4PooledAddr`/
            `vnicIpV4StaticAddr` returned by a domain-wide query.

    Returns:
        dict[str, Any]: Parent DN -> the first MO found there with a real
            `addr`. A parent whose only MO carries an unset sentinel is
            absent from the mapping, not present with a `None` value.
    """
    by_parent_dn: dict[str, Any] = {}
    for mo in ip_addrs:
        dn = str(getattr(mo, "dn", "") or "")
        parent_dn, _, _ = dn.rpartition("/")
        if not parent_dn:
            continue
        addr = getattr(mo, "addr", None)
        if not addr or addr in ("0.0.0.0", "none"):  # noqa: S104 - unset-IP sentinel
            continue
        by_parent_dn.setdefault(parent_dn, mo)
    return by_parent_dn


def partition_profiles(ls_servers: Iterable[Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """
    Split one `lsServer` query into real service profiles and the templates they derive from.

    See docs/cisco-collectors.md, "Service profiles and server names".

    Args:
        ls_servers (Iterable[Any]): The full result of one `lsServer` query,
            carrying both profiles and templates.

    Returns:
        tuple[dict[str, Any], dict[str, str]]: Service profiles keyed by DN,
            and template DNs keyed by bare template name.
    """
    profile_by_dn: dict[str, Any] = {}
    template_dn_by_name: dict[str, str] = {}
    for mo in ls_servers:
        if str(getattr(mo, "type", "") or "") in TEMPLATE_TYPES:
            name = getattr(mo, "name", None)
            if name:
                template_dn_by_name.setdefault(name, mo.dn)
        else:
            profile_by_dn[mo.dn] = mo
    return profile_by_dn, template_dn_by_name
