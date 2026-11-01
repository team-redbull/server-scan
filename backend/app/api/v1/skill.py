"""`GET /api/v1/skill` and `/skill/download` — the Claude Code skill for this API (ADR-0052)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.api.v1.skill_schemas import SkillFileOut, SkillResponse
from app.application.services.skill_bundle import (
    SkillBundle,
    build_zip,
    is_valid_base_url,
    load_skill,
    resolve_skill_dir,
)
from app.config import Settings, get_settings
from app.dependencies import get_current_actor
from app.domain.models.audit_event import Actor, Role
from app.errors import NotFoundError, ValidationAppError

router = APIRouter(prefix="/api/v1", tags=["skill"])


def _bundle(settings: Settings) -> SkillBundle:
    """
    Read the skill from disk.

    Args:
        settings (Settings): Supplies `skill_dir`.

    Returns:
        SkillBundle: The skill as it is right now.

    Raises:
        NotFoundError: No skill folder exists on this deployment.
    """
    skill_dir = resolve_skill_dir(settings.skill_dir)
    if skill_dir is None:
        raise NotFoundError("This deployment carries no Claude Code skill.")
    return load_skill(skill_dir)


@router.get("/skill", response_model=SkillResponse)
async def get_skill(settings: Annotated[Settings, Depends(get_settings)]) -> SkillResponse:
    """
    The skill's files and metadata, read from disk on every call.

    Args:
        settings (Settings): Supplies `skill_dir`.

    Returns:
        SkillResponse: The skill; `version` changes whenever a file does.
    """
    bundle = _bundle(settings)
    return SkillResponse(
        name=bundle.name,
        description=bundle.description,
        when_to_use=bundle.when_to_use,
        allowed_tools=bundle.allowed_tools,
        version=bundle.version,
        updated_at=bundle.updated_at,
        files=[SkillFileOut(path=f.path, content=f.content) for f in bundle.files],
    )


@router.get("/skill/download")
async def download_skill(
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    base_url: Annotated[str | None, Query(max_length=200)] = None,
) -> Response:
    """
    The skill as a zip to unzip into `~/.claude/skills/`.

    Args:
        settings (Settings): Supplies `skill_dir`.
        actor (Actor): The caller; a viewer's copy names `VIEWER_TOKEN`.
        base_url (str | None): This deployment's address, written into `BASE_URL`.

    Returns:
        Response: The zip, named `<skill>.zip`.

    Raises:
        ValidationAppError: `base_url` is not a plain http(s) address.
    """
    if base_url is not None and not is_valid_base_url(base_url):
        raise ValidationAppError("base_url must be a plain http(s) address.")
    bundle = _bundle(settings)
    return Response(
        content=build_zip(bundle, base_url, viewer=actor.role is Role.VIEWER),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.name}.zip"',
            "Cache-Control": "no-cache",
        },
    )
