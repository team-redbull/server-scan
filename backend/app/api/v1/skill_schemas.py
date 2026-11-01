"""Response shapes for `GET /api/v1/skill`."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SkillFileOut(BaseModel):
    """
    One file of the skill.

    Attributes:
        path (str): Path inside the skill folder.
        content (str): The file's text.
    """

    path: str
    content: str


class SkillResponse(BaseModel):
    """
    The skill as currently on the server.

    Attributes:
        name (str): The skill's name and install folder.
        description (str): What the skill is for.
        when_to_use (str): Extra trigger hints, or empty.
        allowed_tools (str): The tools it may call, or empty.
        version (str): Short hash that changes whenever any file does.
        updated_at (datetime): When the newest file last changed.
        files (list[SkillFileOut]): `SKILL.md` first.
    """

    name: str
    description: str
    when_to_use: str
    allowed_tools: str
    version: str
    updated_at: datetime
    files: list[SkillFileOut]
