"""The set of sites, and deriving a server's site from its name.

**The sites are deployment configuration, not code.** Which site codes
exist is a property of one estate's hostname convention — `tlv` means
something in this deployment and nothing in the next — so the set is
loaded from `INVENTORY_SITES` and parsed into a `SiteCatalog` at startup.
Renaming a site, adding one, or standing the platform up for a different
estate is an environment change, never an edit to this file. See
docs/adr/0018-sites-from-configuration.md.

Every production hostname embeds its site as a whole `-`-delimited token:

    ocp4-prod-tlv-infra-01          -> tlv
    ocp4-hypershift-five-01         -> five
    ocp4-hypershift-data-five-02    -> five
    ocp-dell-r660-five-128c-1024gb-FCH123  -> five
    ocp4-nyc-control-plane-02       -> nyc
    ocp-bat-yam-r660-worker-01      -> bat-yam

so the name is the authority, not the collector's configuration. That
choice is deliberate: a manager whose site was set wrong would otherwise
mislabel every server it collects, and nothing downstream could tell.
Parsing the name instead makes the label self-correcting — rename the
host, and the platform agrees on the next collection.

The same function reads a UCS org DN (`org-root/org_tlv/ls-worker-01`),
which is the collector's *fallback* when a name carries no site token —
see `app.application.services.ingest`. `/` is a separator here for that
reason.

A code spelled with a separator (`bat-yam`) matches a run of consecutive
tokens, exactly. Every code and alias also matches as a substring of a
single token (`ocp4-computezn-01` -> alias `zn`) — an operator-requested
reversal, 2026-09-09, that accepts a real false-positive risk; the
canonical-before-alias tier and the leftmost-alias tiebreak followed on
2026-09-10. ADR-0018's dated updates carry each decision and the
collision that forced it.

When neither says anything, the BMC IP is the last fallback
(`INVENTORY_SITE_BMC_PREFIXES`, ADR-0056).

A name with no site token returns `None`. That is a real state the UI
surfaces ("Unassigned"), never a silent default to some arbitrary site —
mislabelling a server's location is worse than admitting the name
doesn't say.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, replace
from functools import lru_cache

# An explicit class, not `\W+`: an unexpected name format yields no
# tokens and so no site, rather than a creative reinterpretation.
_SEPARATORS = re.compile(r"[-_./]+")

# What a hostname can carry; rejected at startup rather than never matching.
_VALID_CODE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

DEFAULT_SITES_SPEC = "nyc:New York City,tlv:Tel Aviv,bat-yam:Bat Yam,five:Site Five"

# Wire spelling for a `site_id` stored as `None`.
UNASSIGNED_SITE_ID = "unassigned"


class SiteConfigurationError(ValueError):
    """
    `INVENTORY_SITES` could not be read.

    Raised at startup, never during a request, so a typo fails loudly.
    """


@dataclass(frozen=True, slots=True)
class SiteDefinition:
    """
    One site.

    Attributes:
        code (str): The canonical token embedded in hostnames, e.g.
            `"bat-yam"`. Also the `Site` document's id and the value
            stored in `Server.site_id` — an alias never is, even when a
            server's own name carried the alias rather than this code.
        name (str): What the UI shows, e.g. `"Bat Yam"`.
        aliases (tuple[str, ...]): Other tokens a hostname may carry that
            mean this same site — e.g. an old naming convention's code, or
            an abbreviation a different team used. Every server matching
            an alias is stored and shown under `code`, not the alias, so
            they combine onto one site card rather than splitting across
            two.
    """

    code: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SiteCatalog:
    """
    The sites this deployment knows about: closed at runtime, from configuration.

    Immutable once built, so it can be shared freely and cannot drift mid-run.
    """

    definitions: tuple[SiteDefinition, ...]
    bmc_prefixes: tuple[tuple[tuple[int, ...], str], ...] = ()

    @classmethod
    def from_spec(cls, spec: str, bmc_prefixes: str = "") -> SiteCatalog:
        """
        Parse `INVENTORY_SITES` (`code:Display Name`, comma-separated) into a catalog.

        The display half is optional; the code half may be `|`-separated
        aliases, the first of which is canonical (ADR-0018, 2026-09-08 update).

        Args:
            spec (str): The raw configured value. Empty means the shipped
                default rather than "no sites", since a deployment with no
                sites could label nothing at all.
            bmc_prefixes (str): `INVENTORY_SITE_BMC_PREFIXES`, e.g.
                `"10=tlv,149=bat-yam"`; empty means no BMC fallback.

        Returns:
            SiteCatalog: The parsed catalog, in configured order.

        Raises:
            SiteConfigurationError: On a malformed or duplicate entry —
                including one token (a code or an alias) reused anywhere
                else in the spec, which would make it ambiguous which
                site a hostname carrying it names.
        """
        text = spec.strip() or DEFAULT_SITES_SPEC
        definitions: list[SiteDefinition] = []
        seen: set[str] = set()
        for entry in text.split(","):
            if not entry.strip():
                continue
            code_field, _, name = entry.partition(":")
            tokens = [token.strip().lower() for token in code_field.split("|")]
            name = name.strip()
            for token in tokens:
                if not _VALID_CODE.match(token):
                    raise SiteConfigurationError(
                        f"INVENTORY_SITES: {token!r} is not a usable site code or alias. "
                        "A code is the token that appears inside a hostname, so it must "
                        "be lowercase letters, digits and single hyphens — e.g. 'tlv' or "
                        "'bat-yam'."
                    )
                if token in seen:
                    raise SiteConfigurationError(
                        f"INVENTORY_SITES: {token!r} is listed twice — as a code or alias "
                        "of more than one site, or twice for the same one."
                    )
                seen.add(token)
            code, *aliases = tokens
            definitions.append(
                SiteDefinition(code=code, name=name or _title_case(code), aliases=tuple(aliases))
            )
        if not definitions:
            raise SiteConfigurationError(
                "INVENTORY_SITES is set but lists no sites. Leave it unset for the "
                f"default ({DEFAULT_SITES_SPEC!r}), or name at least one site."
            )
        catalog = cls(definitions=tuple(definitions))
        return replace(catalog, bmc_prefixes=_parse_bmc_prefixes(bmc_prefixes, catalog))

    @property
    def codes(self) -> tuple[str, ...]:
        """
        Every configured site code, in configured order.

        Returns:
            tuple[str, ...]: Every site code, in configured order.
        """
        return tuple(definition.code for definition in self.definitions)

    def __contains__(self, code: object) -> bool:
        """
        Whether this deployment knows about a candidate site code.

        Args:
            code (object): A candidate site code.

        Returns:
            bool: Whether this deployment knows that site.
        """
        return isinstance(code, str) and code.lower() in set(self.codes)

    def name_for(self, code: str) -> str:
        """
        What to call a site in the UI.

        Args:
            code (str): A site code.

        Returns:
            str: Its display name, or a title-cased fallback for a code
                this catalog does not know — which happens to a server
                stored under a site that has since been reconfigured
                away, and is better rendered than hidden.
        """
        for definition in self.definitions:
            if definition.code == code:
                return definition.name
        return _title_case(code)

    def alternation(self) -> str:
        """
        Every site code and alias as one regex alternation.

        No production caller since 2026-09-08 (ADR-0018); kept correct for the next one.

        Returns:
            str: e.g. `"nyc|tlv|bat-yam|five"`, regex-escaped.
        """
        return "|".join(
            re.escape(token)
            for definition in self.definitions
            for token in (definition.code, *definition.aliases)
        )

    def parse(self, name: str | None) -> str | None:
        """
        The site code embedded in `name`, or `None` if it holds none.

        Canonical codes before aliases; two real codes is `None`, two aliases
        picks the leftmost — each rule's date and collision is in ADR-0018.

        Args:
            name (str | None): A hostname, or a UCS org/profile DN.

        Returns:
            str | None: The single canonical site code named, or `None`.
                Always `definition.code`, never an alias, even when the
                alias is what the name actually carried.
        """
        if not name:
            return None
        tokens = [token for token in _SEPARATORS.split(name.strip().lower()) if token]

        codes_only = {definition.code: definition.code for definition in self.definitions}
        found = self._matches(tokens, codes_only)
        if len(found) == 1:
            return next(iter(found))
        if found:
            return None  # 2+ real codes named at once — ambiguous, no alias tier can help

        with_aliases = {
            token: definition.code
            for definition in self.definitions
            for token in (definition.code, *definition.aliases)
        }
        found = self._matches(tokens, with_aliases)
        if not found:
            return None
        # 2+ aliases at once picks the leftmost (ADR-0018, 2026-09-10).
        return min(found.items(), key=lambda item: item[1])[0]

    def parse_bmc(self, host: str | None) -> str | None:
        """
        The site whose configured IP prefix is the longest match for a BMC host.

        The last fallback after the name and the profile DN (ADR-0056); matches
        whole octets, so `10` never matches `100.x`.

        Args:
            host (str | None): The parsed BMC host.

        Returns:
            str | None: The canonical site code, or `None` for no rule, a
                hostname or an IPv6 address.
        """
        if not host or not self.bmc_prefixes:
            return None
        try:
            address = ipaddress.ip_address(host.strip())
        except ValueError:
            return None
        if address.version != 4:
            return None
        octets = tuple(address.packed)
        best: tuple[int, str] | None = None
        for prefix, code in self.bmc_prefixes:
            if octets[: len(prefix)] == prefix and (best is None or len(prefix) > best[0]):
                best = (len(prefix), code)
        return best[1] if best else None

    @staticmethod
    def _matches(tokens: list[str], by_value: dict[str, str]) -> dict[str, int]:
        """
        Every site `tokens` names, against one code/alias -> code mapping.

        Args:
            tokens (list[str]): The hostname's own `-`/`_`/`.`/`/`-split
                tokens, already lowercased.
            by_value (dict[str, str]): Candidate token -> canonical code,
                scoped by the caller to just codes or codes-plus-aliases.

        Returns:
            dict[str, int]: Canonical code -> the earliest token index it
                matched at. Empty (nothing matched), one entry (a clean
                result) or 2+ (ambiguous within this tier — `parse`
                decides what "ambiguous" means for the tier it called
                this with).
        """
        max_tokens = max((len(_SEPARATORS.split(code)) for code in by_value), default=1)
        positions: dict[str, int] = {}
        for size in range(1, max_tokens + 1):
            for start in range(len(tokens) - size + 1):
                candidate = "-".join(tokens[start : start + size])
                code = by_value.get(candidate)
                if code is not None:
                    positions[code] = min(positions.get(code, start), start)
        # Substring of one token ("zn" in "computezn"), ADR-0018's 2026-09-09
        # update; a multi-token code can never be one, its "-" is gone.
        for index, hostname_token in enumerate(tokens):
            for candidate, code in by_value.items():
                if candidate in hostname_token:
                    positions[code] = min(positions.get(code, index), index)
        return positions


def _parse_bmc_prefixes(spec: str, catalog: SiteCatalog) -> tuple[tuple[tuple[int, ...], str], ...]:
    """
    Parse `INVENTORY_SITE_BMC_PREFIXES` into (octets, canonical site code) pairs.

    Args:
        spec (str): `prefix=site` entries, comma-separated, where a prefix is
            one to four dotted octets (`10`, `149`, `10.20`).
        catalog (SiteCatalog): The configured sites every rule must name.

    Returns:
        tuple[tuple[tuple[int, ...], str], ...]: The rules, in configured order.

    Raises:
        SiteConfigurationError: On a malformed entry, a duplicate prefix or a site
            that is not in `INVENTORY_SITES`.
    """
    canonical = {
        token: definition.code
        for definition in catalog.definitions
        for token in (definition.code, *definition.aliases)
    }
    rules: dict[tuple[int, ...], str] = {}
    for entry in spec.split(","):
        if not entry.strip():
            continue
        prefix_text, sep, site = entry.partition("=")
        parts = prefix_text.strip().split(".")
        valid = sep and 1 <= len(parts) <= 4 and all(p.isdigit() and int(p) <= 255 for p in parts)
        if not valid:
            raise SiteConfigurationError(
                f"INVENTORY_SITE_BMC_PREFIXES: {entry.strip()!r} is not 'prefix=site' with a "
                "prefix of one to four dotted octets, e.g. '10=tlv' or '10.20=bat-yam'."
            )
        code = canonical.get(site.strip().lower())
        if code is None:
            raise SiteConfigurationError(
                f"INVENTORY_SITE_BMC_PREFIXES: site {site.strip()!r} is not in INVENTORY_SITES."
            )
        octets = tuple(int(p) for p in parts)
        if octets in rules:
            raise SiteConfigurationError(
                f"INVENTORY_SITE_BMC_PREFIXES: prefix {prefix_text.strip()!r} is listed twice."
            )
        rules[octets] = code
    return tuple(rules.items())


def _title_case(code: str) -> str:
    """
    A readable label for a code with no configured name.

    Args:
        code (str): A site code, e.g. `"bat-yam"`.

    Returns:
        str: e.g. `"Bat Yam"`.
    """
    return " ".join(part.capitalize() for part in code.split("-") if part)


@lru_cache(maxsize=8)
def site_catalog(spec: str, bmc_prefixes: str = "") -> SiteCatalog:
    """
    A cached catalog for one configured spec.

    Keyed on the specs, not `Settings`, so a test can pass a literal.

    Args:
        spec (str): The `INVENTORY_SITES` value.
        bmc_prefixes (str): The `INVENTORY_SITE_BMC_PREFIXES` value.

    Returns:
        SiteCatalog: The parsed catalog.

    Raises:
        SiteConfigurationError: On a malformed entry.
    """
    return SiteCatalog.from_spec(spec, bmc_prefixes)


def parse_site_code(name: str | None, catalog: SiteCatalog) -> str | None:
    """
    The site code embedded in `name`, against a given catalog.

    The explicit catalog keeps this module free of application configuration.

    Args:
        name (str | None): A hostname, or a UCS org/profile DN.
        catalog (SiteCatalog): The sites this deployment knows.

    Returns:
        str | None: The single site code named, or `None`.
    """
    return catalog.parse(name)
