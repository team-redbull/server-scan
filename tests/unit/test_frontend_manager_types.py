"""
Guards that the frontend's hardcoded copies of `ManagerType` do not drift
from the backend's — the Intersight collector shipped with its value missing
from the Source filter for six commits, with no error anywhere. String-level
checks over the real `.ts`/`.tsx` sources, like `test_no_committed_secrets.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tools.run_collector import PROVIDER_FACTORIES

from app.domain.enums import ManagerType

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]

# Derived, never restated: a hand-written set here drifted exactly like the
# frontend list it guards (OPENMANAGE and ONEVIEW shipped, the guard stayed
# green while Dell and HPE servers were unfilterable in the UI).
_IMPLEMENTED = frozenset(PROVIDER_FACTORIES)


def _source(relative: str) -> str:
    """
    Read one frontend source file.

    Args:
        relative (str): Repo-relative path.

    Returns:
        str: Its text.
    """
    path = _REPO / relative
    assert path.exists(), f"{relative} has moved; update this guard rather than deleting it."
    return path.read_text()


def test_the_source_filter_offers_every_implemented_collector() -> None:
    text = _source("frontend/src/api/sites.ts")
    listed = set(re.findall(r'\{\s*value:\s*"([A-Z_]+)"', text))

    missing = {t.value for t in _IMPLEMENTED} - listed
    assert not missing, (
        f"SOURCE_PROVIDERS in frontend/src/api/sites.ts is missing {sorted(missing)}. "
        "A collector was implemented without being added to the inventory page's "
        "Source filter."
    )


def test_the_source_filter_offers_nothing_unimplemented() -> None:
    text = _source("frontend/src/api/sites.ts")
    listed = set(re.findall(r'\{\s*value:\s*"([A-Z_]+)"', text))
    known = {t.value for t in ManagerType}

    unimplemented = (listed & known) - {t.value for t in _IMPLEMENTED}
    assert not unimplemented, (
        f"SOURCE_PROVIDERS offers {sorted(unimplemented)}, which has no collector, "
        "so selecting it always returns nothing."
    )


def test_the_manager_type_union_carries_every_member() -> None:
    """`RuleScope.manager_type` and `PolicyScope.manager_types` are typed by
    this union, so a missing member mistypes a scope the Rules page renders —
    `REDFISH_STANDALONE` was once missing, and no scope could name it.
    """
    relative = "frontend/src/types/classification.ts"
    text = _source(relative)
    missing = [member.value for member in ManagerType if f'"{member.value}"' not in text]

    assert not missing, (
        f"{relative} is missing manager type(s) {missing}. Add them there, or narrow this "
        "guard deliberately if a type is meant to be unrepresentable."
    )
