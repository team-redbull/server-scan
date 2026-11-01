"""Reads the `server-scan-api` Claude Code skill from disk, for the UI and its download (ADR-0052).

The files are read on every request, so an edit to the skill reaches the UI
with no UI change; the image carries whatever the repo held at build time.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_MAX_FILE_BYTES = 512 * 1024
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_BASE_URL_LINE = re.compile(r"^BASE_URL=.*$", re.MULTILINE)
_BASE_URL = re.compile(r"https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~\-/]*)?")
_REPO_SKILL = Path(".claude") / "skills" / "server-scan-api"


@dataclass(frozen=True, slots=True)
class SkillFile:
    """
    One file of the skill.

    Attributes:
        path (str): Path relative to the skill folder, e.g. `references/endpoints.md`.
        content (str): The file's text.
    """

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class SkillBundle:
    """
    The whole skill as read from disk.

    Attributes:
        name (str): The skill's name, also its install folder.
        description (str): The frontmatter `description`.
        when_to_use (str): The frontmatter `when_to_use`, or empty.
        allowed_tools (str): The frontmatter `allowed-tools`, or empty.
        version (str): Short hash of every file, changing when any file does.
        updated_at (datetime): The newest file modification time (UTC).
        files (tuple[SkillFile, ...]): `SKILL.md` first, then the rest by path.
    """

    name: str
    description: str
    when_to_use: str
    allowed_tools: str
    version: str
    updated_at: datetime
    files: tuple[SkillFile, ...]


def resolve_skill_dir(configured: str | None) -> Path | None:
    """
    Find the skill folder: the configured one, the image's, or the repo's.

    Args:
        configured (str | None): `INVENTORY_SKILL_DIR`, if set.

    Returns:
        Path | None: The first folder holding a `SKILL.md`, or `None`.
    """
    here = Path(__file__).resolve()
    candidates = [Path(configured)] if configured else []
    candidates += [here.parents[3] / "skill", here.parents[4] / _REPO_SKILL]
    return next((p for p in candidates if (p / "SKILL.md").is_file()), None)


def _frontmatter(text: str) -> dict[str, str]:
    """
    Read the `key: value` lines between the opening `---` pair.

    Args:
        text (str): The text of `SKILL.md`.

    Returns:
        dict[str, str]: Each key with its value, empty when there is no frontmatter.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    found: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep:
            found[key.strip()] = value.strip()
    return found


def load_skill(skill_dir: Path) -> SkillBundle:
    """
    Read the skill folder.

    Args:
        skill_dir (Path): A folder holding `SKILL.md` and optionally `references/*.md`.

    Returns:
        SkillBundle: Its files, frontmatter fields, version and newest mtime.
    """
    paths = [skill_dir / "SKILL.md", *sorted((skill_dir / "references").glob("*.md"))]
    files: list[SkillFile] = []
    newest = 0.0
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            continue
        content = path.read_text(encoding="utf-8")
        relative = path.relative_to(skill_dir).as_posix()
        files.append(SkillFile(relative, content))
        newest = max(newest, path.stat().st_mtime)
        digest.update(relative.encode() + b"\0" + content.encode() + b"\0")
    meta = _frontmatter(files[0].content) if files else {}
    name = meta.get("name", "")
    return SkillBundle(
        name=name if _NAME.fullmatch(name) else "server-scan-api",
        description=meta.get("description", ""),
        when_to_use=meta.get("when_to_use", ""),
        allowed_tools=meta.get("allowed-tools", ""),
        version=digest.hexdigest()[:12],
        updated_at=datetime.fromtimestamp(newest, tz=UTC),
        files=tuple(files),
    )


def is_valid_base_url(value: str) -> bool:
    """
    Whether a string is safe to write into the skill as `BASE_URL`.

    Args:
        value (str): A candidate such as `https://server-scan.example.internal`.

    Returns:
        bool: True for an http(s) origin with an optional path and no whitespace or quotes.
    """
    return _BASE_URL.fullmatch(value) is not None


def build_zip(bundle: SkillBundle, base_url: str | None, viewer: bool = False) -> bytes:
    """
    Pack the skill as `<name>/...`, ready to unzip into `~/.claude/skills/`.

    Args:
        bundle (SkillBundle): The skill as read.
        base_url (str | None): Written over the placeholder `BASE_URL=` line when given
            (the caller validates it); the token line is never filled in.
        viewer (bool): Rename `AUDITOR_TOKEN` to `VIEWER_TOKEN` in `SKILL.md`, so a
            viewer's copy names the token they actually hold.

    Returns:
        bytes: The zip archive.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in bundle.files:
            content = file.content
            if base_url and file.path == "SKILL.md":
                content = _BASE_URL_LINE.sub(lambda _: f"BASE_URL={base_url}", content, count=1)
            if viewer and file.path == "SKILL.md":
                content = content.replace("AUDITOR_TOKEN", "VIEWER_TOKEN")
            archive.writestr(f"{bundle.name}/{file.path}", content)
    return out.getvalue()
