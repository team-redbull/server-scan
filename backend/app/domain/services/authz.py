"""AD-login role-list types shared by `app.application.services.auth_service`.

Split out purely so `LoginResult` and the comma-list parsing have somewhere
domain-layer to live without `auth_service` reaching for a plain string
literal for "authenticated but listed nowhere" (docs/adr/0034).
"""

from __future__ import annotations

from enum import StrEnum


class LoginResult(StrEnum):
    """The one login outcome `Role` doesn't cover: authenticated, but listed nowhere."""

    NO_PERMISSION = "NO_PERMISSION"


def split_csv_lower(value: str) -> set[str]:
    """
    Split a comma-separated `Settings` field into a lower-cased set.

    Args:
        value (str): A comma-separated string, e.g. `Settings.admin_groups`.

    Returns:
        set[str]: Each entry, trimmed, lower-cased, with blanks dropped.
    """
    return {entry.strip().lower() for entry in value.split(",") if entry.strip()}
