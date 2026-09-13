"""
`app.domain.value_objects.site` — the site catalog and the parser every
server's site label depends on (ADR-0018). Its false-positive behaviour
matters more than its happy path: the codes are short and live inside hostnames.
"""

from __future__ import annotations

import pytest

from app.domain.value_objects.site import (
    DEFAULT_SITES_SPEC,
    SiteCatalog,
    SiteConfigurationError,
    parse_site_code,
    site_catalog,
)

pytestmark = pytest.mark.unit

SITES = site_catalog(DEFAULT_SITES_SPEC)


def parse(name: str | None) -> str | None:
    """
    Parse against the shipped default catalog.

    Args:
        name (str | None): A hostname or DN.

    Returns:
        str | None: The site code, or None.
    """
    return parse_site_code(name, SITES)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ocp4-prod-tlv-infra-01", "tlv"),
        ("ocp4-prep-five-compute-01", "five"),
        ("ocp4-hypershift-five-01", "five"),
        ("ocp4-hypershift-data-five-02", "five"),
        ("ocp-dell-r660-five-128c-1024gb-FCH1234567", "five"),
        ("ocp4-five-compute-01", "five"),
        ("ocp4-nyc-control-plane-02", "nyc"),
        ("ocp4-tlv-worker-03", "tlv"),
        ("ocp4-bat-yam-infra-01", "bat-yam"),
        ("ocp-dell-r660-bat-yam-64c-512gb-FCH1234567", "bat-yam"),
        ("OCP4-PROD-TLV-INFRA-01", "tlv"),
        ("Ocp4-Prod-Nyc-Infra-01", "nyc"),
        ("ocp4_prod_tlv_infra_01", "tlv"),
        ("ocp4.prod.nyc.infra.01", "nyc"),
        ("  ocp4-prod-tlv-infra-01  ", "tlv"),
        # A UCS org DN, which is the collector's fallback when the name
        # carries no site token.
        ("org-root/org_tlv/ls-worker-01", "tlv"),
        ("org-root/org-bat-yam/ls-worker-01", "bat-yam"),
    ],
)
def test_parses_the_site_token(name: str, expected: str) -> None:
    assert parse(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Substring matching (2026-09-09, operator's request, see the module
        # docstring): a code glued inside a larger token now matches.
        ("ocp4-tlvx-01", "tlv"),
        ("ocp4-prod-nycity-01", "nyc"),
        ("ocp4-fivestar-01", "five"),
    ],
)
def test_a_code_embedded_in_a_larger_word_now_matches(name: str, expected: str) -> None:
    assert parse(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        # A multi-token code never substring-matches: splitting removed every
        # "-" from each token, so "bat-yam" cannot be a substring of one.
        "ocp4-batyam-01",  # "bat-yam" without its separator
        "ocp4-bat-01",  # half of "bat-yam"
        "batman-host",  # no configured code/alias is a substring of "batman"
    ],
)
def test_a_multi_token_code_never_substring_matches(name: str) -> None:
    assert parse(name) is None


@pytest.mark.parametrize(
    "name", ["", None, "   ", "ocp4-prod-infra-01", "server-01", "-", "___", "org-root/ls-w-01"]
)
def test_returns_none_when_there_is_no_site_token(name: str | None) -> None:
    assert parse(name) is None


def test_ambiguous_name_with_two_sites_returns_none_rather_than_guessing() -> None:
    assert parse("ocp4-tlv-nyc-infra-01") is None


def test_a_short_code_can_collide_with_infra_and_go_ambiguous() -> None:
    """A real collision: "fra" sits inside the role token "infra", so every
    `-infra-` server goes ambiguous. Accepted cost of substring matching
    (2026-09-09); avoiding role words is the operator's job (ADR-0018).
    """
    catalog = SiteCatalog.from_spec("lon:London,fra:Frankfurt")
    assert parse_site_code("ocp4-prod-lon-infra-01", catalog) is None


def test_the_same_site_repeated_is_not_ambiguous() -> None:
    assert parse("ocp4-tlv-infra-tlv-01") == "tlv"


def test_every_configured_site_is_parseable_from_a_realistic_name() -> None:
    """Guards against a site being configured that the parser can never
    actually produce — which would leave that site's servers unassigned
    with no error anywhere.
    """
    for code in SITES.codes:
        assert parse(f"ocp4-prod-{code}-infra-01") == code


def test_every_configured_site_has_a_display_name() -> None:
    """`GET /api/v1/sites` is the only place the UI learns site names."""
    for definition in SITES.definitions:
        assert definition.name


def test_a_deployment_can_name_its_own_sites() -> None:
    catalog = SiteCatalog.from_spec("lon:London,fra:Frankfurt")

    assert catalog.codes == ("lon", "fra")
    assert catalog.name_for("lon") == "London"
    # Not "-infra-" here: "fra" collides with it, and that has its own test
    # above (`test_a_short_code_can_collide_with_infra_and_go_ambiguous`).
    assert parse_site_code("ocp4-prod-lon-worker-01", catalog) == "lon"
    # And the sites it no longer has are no longer recognised.
    assert parse_site_code("ocp4-prod-tlv-worker-01", catalog) is None


def test_the_display_half_is_optional() -> None:
    catalog = SiteCatalog.from_spec("lon,bat-yam")

    assert catalog.name_for("lon") == "Lon"
    assert catalog.name_for("bat-yam") == "Bat Yam"


def test_an_empty_spec_falls_back_to_the_shipped_default() -> None:
    assert SiteCatalog.from_spec("").codes == SITES.codes
    assert SiteCatalog.from_spec("   ").codes == SITES.codes


def test_a_multi_token_configured_code_still_matches_consecutive_tokens() -> None:
    catalog = SiteCatalog.from_spec("new-york-city:NYC")

    assert parse_site_code("ocp4-new-york-city-infra-01", catalog) == "new-york-city"
    assert parse_site_code("ocp4-new-york-01", catalog) is None


@pytest.mark.parametrize(
    "spec",
    [
        "tel aviv",  # a space cannot appear in a hostname token
        "tlv_x",  # underscore is a separator, so it can never match
        "-tlv",  # leading separator
        "tlv-",  # trailing separator
        "tlv--x",  # doubled separator
    ],
)
def test_a_code_that_could_never_match_a_hostname_is_rejected_loudly(spec: str) -> None:
    """Silently accepting one would produce a site that exists in the UI
    and that no server can ever be assigned to.
    """
    with pytest.raises(SiteConfigurationError):
        SiteCatalog.from_spec(spec)


def test_a_code_written_in_uppercase_is_normalised_rather_than_rejected() -> None:
    """Hostname tokens are matched lowercase, so `TLV` in configuration
    is an operator being tidy, not an error worth failing startup over.
    """
    catalog = SiteCatalog.from_spec("TLV:Tel Aviv")

    assert catalog.codes == ("tlv",)
    assert parse_site_code("ocp4-prod-TLV-infra-01", catalog) == "tlv"


def test_a_duplicate_code_is_rejected() -> None:
    """Two entries for one code means one display name silently wins."""
    with pytest.raises(SiteConfigurationError, match="listed twice"):
        SiteCatalog.from_spec("tlv:Tel Aviv,tlv:Telaviv")


def test_a_spec_of_only_separators_is_rejected() -> None:
    """Distinct from empty, which legitimately means "use the default"."""
    with pytest.raises(SiteConfigurationError, match="lists no sites"):
        SiteCatalog.from_spec(",,,")


def test_the_regex_alternation_covers_every_configured_site() -> None:
    """No production caller uses this since the seeded rules became catch-alls,
    but it must still cover every code and alias for whatever next needs it.
    """
    catalog = SiteCatalog.from_spec("lon:London,bat-yam:Bat Yam")

    assert catalog.alternation() == "lon|bat\\-yam"


def test_membership_is_case_insensitive_but_codes_stay_canonical() -> None:
    assert "TLV" in SITES
    assert "tlv" in SITES
    assert "nope" not in SITES
    assert SITES.codes == tuple(c.lower() for c in SITES.codes)


def test_an_unknown_code_still_renders_rather_than_disappearing() -> None:
    """A server stored under a site that has since been reconfigured away
    must still be displayable — that is the reason `Server.site_id` is a
    plain string and not an enum.
    """
    assert SITES.name_for("bat-yam") == "Bat Yam"
    assert SITES.name_for("decommissioned-dc") == "Decommissioned Dc"


# --- aliases: several hostname tokens naming one site -------------------


def test_an_alias_resolves_to_its_sites_canonical_code() -> None:
    """A hostname carrying the alias lands on the same site as one
    carrying the canonical code — that is the whole point: two naming
    conventions, one site card.
    """
    catalog = SiteCatalog.from_spec("znif|prep:Znif,five|fn:Site Five")

    assert parse_site_code("ocp4-prod-znif-infra-01", catalog) == "znif"
    assert parse_site_code("ocp4-prod-prep-infra-01", catalog) == "znif"
    assert parse_site_code("ocp4-fn-compute-01", catalog) == "five"
    assert parse_site_code("ocp4-five-compute-01", catalog) == "five"


def test_a_site_may_carry_more_than_two_aliases() -> None:
    catalog = SiteCatalog.from_spec("znif|prep|legacy-znf:Znif")

    for token in ("znif", "prep", "legacy-znf"):
        assert parse_site_code(f"ocp4-{token}-infra-01", catalog) == "znif"


def test_the_first_token_is_canonical_everywhere_else_too() -> None:
    """`code`, `codes` and `name_for` all key off the first `|`-separated
    token — the rest exist only for `parse` to recognise, never as a
    value this catalog produces on its own.
    """
    catalog = SiteCatalog.from_spec("znif|prep:Znif")

    assert catalog.codes == ("znif",)
    assert catalog.definitions[0].code == "znif"
    assert catalog.definitions[0].aliases == ("prep",)
    assert catalog.name_for("znif") == "Znif"


def test_an_alias_is_case_insensitive_like_a_code() -> None:
    catalog = SiteCatalog.from_spec("znif|PREP:Znif")

    assert catalog.definitions[0].aliases == ("prep",)
    assert parse_site_code("ocp4-PREP-infra-01", catalog) == "znif"


def test_two_aliases_of_the_same_site_in_one_name_is_not_ambiguous() -> None:
    catalog = SiteCatalog.from_spec("znif|prep:Znif")

    assert parse_site_code("ocp4-znif-prep-infra-01", catalog) == "znif"


def test_a_real_site_code_wins_over_an_unrelated_sites_alias() -> None:
    """The real bug, reproduced — see `SiteCatalog.parse`'s own docstring
    for why canonical codes are tried before aliases at all.
    """
    catalog = SiteCatalog.from_spec("znif|prep:Znif,five:Site Five")

    assert parse_site_code("ocp4-prep-five-compute-01", catalog) == "five"


def test_an_alias_still_resolves_when_no_real_code_is_present() -> None:
    catalog = SiteCatalog.from_spec("znif|prep:Znif,five:Site Five")

    assert parse_site_code("ocp4-prep-compute-01", catalog) == "znif"


def test_two_real_codes_at_once_stays_ambiguous_even_with_an_alias_present() -> None:
    """Ambiguity at the canonical tier is final - it does not fall
    through to aliases looking for a tiebreaker that cannot exist.
    """
    catalog = SiteCatalog.from_spec("znif|prep:Znif,five:Site Five,nyc:New York City")

    assert parse_site_code("ocp4-prep-five-nyc-01", catalog) is None


def test_two_different_aliases_at_once_picks_the_leftmost_one() -> None:
    """Reversed 2026-09-10, at the operator's request: two real codes at
    once stays ambiguous (the test above), but two *aliases* at once no
    longer is — no real code is ever in play to make the pick risky.
    """
    catalog = SiteCatalog.from_spec("znif|prep:Znif,five|fn:Site Five")

    assert parse_site_code("ocp4-prep-fn-01", catalog) == "znif"  # "prep" (index 1) < "fn" (2)
    assert parse_site_code("ocp4-fn-prep-01", catalog) == "five"  # order in the name flips it


def test_the_operators_own_reported_case() -> None:
    """The exact hostname that motivated leftmost-wins: `fn` (index 0)
    beats `prep` (index 2), so this resolves to `five`, not Unassigned.
    """
    catalog = SiteCatalog.from_spec("znif|prep:Znif,five|fn:Site Five")

    assert parse_site_code("FN-Data-prep-ocp-compute-01", catalog) == "five"


def test_an_alias_reused_as_another_sites_code_is_rejected() -> None:
    with pytest.raises(SiteConfigurationError, match="listed twice"):
        SiteCatalog.from_spec("znif|five:Znif,five:Site Five")


def test_an_alias_reused_as_another_sites_alias_is_rejected() -> None:
    with pytest.raises(SiteConfigurationError, match="listed twice"):
        SiteCatalog.from_spec("znif|shared:Znif,five|shared:Site Five")


def test_the_regex_alternation_includes_aliases() -> None:
    catalog = SiteCatalog.from_spec("znif|prep:Znif")

    assert catalog.alternation() == "znif|prep"
