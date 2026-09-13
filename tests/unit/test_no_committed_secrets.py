"""
Guards that no real BMC credential or captured mockup reaches git —
`.gitignore` does not stop `git add -f`, and committed is forever. A Redfish
credentials file is a fleet-wide root-equivalent secret, and a capture
discloses the estate's topology. See ADR-0016.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]

_EXAMPLES = "docs/examples/"

# Values a committed example may carry. Anything else in a `password`
# field is treated as real — an allowlist, because a blocklist of "bad"
# passwords fails open on the first one nobody thought of.
_ALLOWED_PLACEHOLDERS = frozenset({"CHANGE-ME", "", "***"})

_PASSWORD_LINE = re.compile(r"""^\s*password\s*=\s*["']?([^"'\n]*)["']?\s*$""", re.MULTILINE)

_CAPTURE_MARKERS = ("X-Auth-Token", "Set-Cookie", "Authorization: Basic", "-----BEGIN")


def _tracked_files() -> list[Path]:
    """
    Every file git currently tracks.

    Returns:
        list[Path]: Repo-relative paths.
    """
    out = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 - git resolved from PATH in CI and dev alike
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(line) for line in out.stdout.splitlines() if line]


def test_no_tracked_file_carries_a_real_password() -> None:
    """A `password = ` line outside the examples directory, or one inside
    it holding anything but the placeholder, fails the build.
    """
    offenders: list[str] = []
    for relative in _tracked_files():
        if relative.suffix not in (".toml", ".yaml", ".yml"):
            continue
        path = _REPO / relative
        if not path.exists():
            continue
        text = path.read_text(errors="ignore")
        for match in _PASSWORD_LINE.finditer(text):
            value = match.group(1).strip()
            if value in _ALLOWED_PLACEHOLDERS:
                continue
            # Helm templates carry `{{ .password }}`-style references,
            # which are indirection rather than a secret.
            if "{{" in value:
                continue
            offenders.append(f"{relative}: password = {value!r}")

    assert not offenders, (
        "A real-looking password is committed:\n  "
        + "\n  ".join(offenders)
        + "\nCredentials belong in a Kubernetes Secret. Note that removing the file in a "
        "follow-up commit does NOT remove it from git history."
    )


def test_no_tracked_file_looks_like_a_raw_bmc_capture() -> None:
    """`Redfish-Mockup-Creator` performs no redaction: with `--Headers`
    it writes `X-Auth-Token` verbatim, alongside real account names,
    certificate subjects and internal addresses.
    """
    offenders: list[str] = []
    for relative in _tracked_files():
        if relative.suffix not in (".json", ".toml", ".yaml", ".yml"):
            continue
        path = _REPO / relative
        if not path.exists():
            continue
        text = path.read_text(errors="ignore")
        offenders.extend(
            f"{relative}: contains {marker!r}"
            for marker in _CAPTURE_MARKERS
            if marker in text and str(relative).replace("\\", "/") != "tests/redfish_fixture.py"
        )

    assert not offenders, (
        "A file looks like an unscrubbed capture from real hardware:\n  "
        + "\n  ".join(offenders)
        + "\nScrub it to a property allowlist before committing anything captured from a BMC."
    )


def test_the_example_files_exist_and_are_placeholders_only() -> None:
    credentials = _REPO / _EXAMPLES / "redfish-credentials.example.toml"
    inventory = _REPO / _EXAMPLES / "redfish-inventory.example.toml"
    assert credentials.exists() and inventory.exists()

    for value in _PASSWORD_LINE.findall(credentials.read_text()):
        assert value.strip() == "CHANGE-ME", f"example carries a plausible password: {value!r}"

    # The loader rejects a host that embeds credentials. Checked as an
    # assignment, not the bare word: the file's own comments warn about it.
    assert not _PASSWORD_LINE.findall(inventory.read_text())
