"""Text normalization for sort/search-safe fields.

Every function here is total (never raises, never returns `None` for a
non-null input) because the fields they populate
(`name_normalized`/`serial_normalized`/`model_normalized`) must always be
present — see `app.domain.models.server.Server`'s docstring on why that
matters for keyset pagination.
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
