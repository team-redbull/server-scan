"""The fleet naming convention `ocp-c<conf_id><marker>-mtl-<site>-<serial>` (ADR-0071).

The renamer service builds these names; this module only decides whether a name already is one.
Keep `FLEET_NAME_PATTERN` in sync with the renamer's naming module, which is the source of truth.
"""

from __future__ import annotations

import re

FLEET_NAME_PATTERN = re.compile(r"^ocp-c[0-9]+(\.[0-9]+)?[dn]?-mtl-[a-z0-9]+-[a-z0-9]+$")


def matches_fleet_convention(name: str) -> bool:
    """
    Whether a server name already follows the fleet naming convention.

    Args:
        name (str): The server's current name, compared as is (the convention is lowercase).

    Returns:
        bool: True for e.g. `ocp-c200-mtl-tlv-4b184et` or `ocp-c45.2d-mtl-site2-xyz987`.
    """
    return FLEET_NAME_PATTERN.fullmatch(name) is not None
