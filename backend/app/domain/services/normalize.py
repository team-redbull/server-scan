"""Text normalization for sort/search-safe fields.

Every function here is total (never raises, never returns `None` for a
non-null input) because the fields they populate
(`name_normalized`/`serial_normalized`/`model_normalized`) must always be
present — see `app.domain.models.server.Server`'s docstring on why that
matters for keyset pagination.

`PLACEHOLDER_SERIALS` is the one exception to "normalize, don't judge": the
SMBIOS placeholders a BIOS ships unprogrammed (ADR-0016, 2026-09-13
update), treated as no serial at all. One source of truth for every reader
of a raw SMBIOS/DMI serial — Redfish's `mapping._clean_serial` and the
OpenShift node-serial SSH fallback (ADR-0036).
"""

from __future__ import annotations


def normalize_text(value: str | None) -> str:
    """
    Lowercase and collapse internal whitespace, for a `*_normalized` sort/filter field.

    No tokenizing: these fields are compared and sorted as whole strings.

    Args:
        value (str | None): The raw text, or `None`.

    Returns:
        str: The normalized text, or `""` for `None`/empty input.
    """
    if not value:
        return ""
    return " ".join(value.split()).lower()


PLACEHOLDER_SERIALS = frozenset(
    {
        "",
        "0123456789",
        "default string",
        "to be filled by o.e.m.",
        "to be filled by o.e.m",
        "not specified",
        "none",
        "n/a",
        "unknown",
        "system serial number",
    }
)


def is_placeholder_serial(value: str) -> bool:
    """
    Whether a raw serial is a known SMBIOS placeholder, not real hardware data.

    Args:
        value (str): The serial as read from the machine, un-normalized.

    Returns:
        bool: True if it matches a known placeholder (case-insensitive).
    """
    return value.strip().lower() in PLACEHOLDER_SERIALS
