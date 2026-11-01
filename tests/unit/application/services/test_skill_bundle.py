"""The skill folder as the UI and the download read it (ADR-0052)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.application.services.skill_bundle import (
    build_zip,
    is_valid_base_url,
    load_skill,
    resolve_skill_dir,
)

_SKILL = """---
name: demo-skill
description: Answers things. Use for "all Dell servers in tlv".
allowed-tools: Bash(curl *) Bash(jq *)
---

# Demo

```
BASE_URL=https://server-scan.example.internal
AUDITOR_TOKEN=REPLACE_WITH_AUDITOR_TOKEN
```
"""


def _folder(tmp_path: Path, skill: str = _SKILL) -> Path:
    (tmp_path / "references").mkdir()
    (tmp_path / "SKILL.md").write_text(skill, encoding="utf-8")
    (tmp_path / "references" / "endpoints.md").write_text("# Endpoints\n", encoding="utf-8")
    return tmp_path


def test_the_frontmatter_and_files_are_read(tmp_path: Path) -> None:
    bundle = load_skill(_folder(tmp_path))

    assert bundle.name == "demo-skill"
    assert bundle.description.startswith("Answers things.")
    assert bundle.allowed_tools == "Bash(curl *) Bash(jq *)"
    assert [f.path for f in bundle.files] == ["SKILL.md", "references/endpoints.md"]


def test_the_version_changes_when_any_file_does(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    before = load_skill(folder).version

    (folder / "references" / "endpoints.md").write_text("# Endpoints\nmore\n", encoding="utf-8")

    assert load_skill(folder).version != before


def test_an_unsafe_name_falls_back_to_the_default(tmp_path: Path) -> None:
    folder = _folder(tmp_path, _SKILL.replace("demo-skill", "../evil"))

    assert load_skill(folder).name == "server-scan-api"


def test_the_download_writes_the_base_url_and_leaves_the_token(tmp_path: Path) -> None:
    bundle = load_skill(_folder(tmp_path))

    with zipfile.ZipFile(io.BytesIO(build_zip(bundle, "https://scan.corp.example"))) as archive:
        text = archive.read("demo-skill/SKILL.md").decode()
        assert "demo-skill/references/endpoints.md" in archive.namelist()

    assert "BASE_URL=https://scan.corp.example\n" in text
    assert "AUDITOR_TOKEN=REPLACE_WITH_AUDITOR_TOKEN" in text


def test_the_download_without_a_base_url_keeps_the_placeholder(tmp_path: Path) -> None:
    bundle = load_skill(_folder(tmp_path))

    with zipfile.ZipFile(io.BytesIO(build_zip(bundle, None))) as archive:
        assert b"example.internal" in archive.read("demo-skill/SKILL.md")


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("https://scan.corp.example", True),
        ("http://localhost:5173", True),
        ("https://scan.corp.example/base", True),
        ("javascript:alert(1)", False),
        ("https://a.example\nAUDITOR_TOKEN=x", False),
        ("https://a.example space", False),
        ("https://a.example/'; rm -rf", False),
    ],
)
def test_only_a_plain_http_address_may_be_written(value: str, ok: bool) -> None:
    assert is_valid_base_url(value) is ok


def test_the_configured_folder_wins_and_a_missing_one_is_skipped(tmp_path: Path) -> None:
    folder = _folder(tmp_path)

    assert resolve_skill_dir(str(folder)) == folder
    assert resolve_skill_dir(str(tmp_path / "nope")) != tmp_path / "nope"
