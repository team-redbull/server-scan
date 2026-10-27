"""Classification result, embedded on `Server`.

The classification *engine* (rule resolution, regex evaluation) is a
slice-2 concern; this module only declares the shape a result takes once
computed, so the `Server` document schema doesn't need a migration when
the engine lands — an unclassified server today has the same document
shape a classified one will have tomorrow, just with null rule fields.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import InstallationType


class Classification(BaseModel):
    """The classification outcome embedded on a `Server` document."""

    installation_type: InstallationType = InstallationType.UNCLASSIFIED
    matched_rule_id: str | None = None
    matched_rule_name: str | None = None
    matched_pattern: str | None = None
    matched_field: str | None = None
    classified_at: datetime | None = None
    classification_version: int = 0


def classification_changed_data(
    previous: InstallationType,
    classification: Classification,
    *,
    name_from: str | None = None,
    name_to: str | None = None,
) -> dict[str, str | None]:
    """
    Build the `CLASSIFICATION_CHANGED` event data, in words a user can read.

    Args:
        previous (InstallationType): The installation type before the change.
        classification (Classification): The new classification.
        name_from (str | None): The server's name before, when it was renamed in the same ingest.
        name_to (str | None): The server's name now, when it was renamed in the same ingest.

    Returns:
        dict[str, str | None]: from/to plus the matched rule's name, field and pattern,
            and `name_from`/`name_to` only for a rename.
    """
    data: dict[str, str | None] = {
        "from": previous.value,
        "to": classification.installation_type.value,
        "matched_rule": classification.matched_rule_name,
        "matched_field": classification.matched_field,
        "matched_pattern": classification.matched_pattern,
    }
    if name_from is not None and name_to is not None and name_from != name_to:
        data["name_from"] = name_from
        data["name_to"] = name_to
    return data
