"""Response models for `GET /api/v1/confs` (docs/adr/0061-confs.md)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.models.conf import Conf


class ConfListResponse(BaseModel):
    """Every readable conf, in natural `conf_id` order."""

    items: list[Conf]
    unreadable: list[str] = Field(
        default_factory=list,
        description="The conf_id of each stored document skipped for not matching the schema.",
    )
