"""Capacity-token aliases for `GET /servers/available`'s pattern mode.

Two naming conventions describe the same class of machine: a descriptive
slug carrying an explicit capacity token
(`ocp-dell-r650-five-128c-1024gb-5tb-DEL0001475`), and a `hypershift`/
`hypershift-data` pair that means the same size without spelling it out.
This table is what lets `pattern=5tb` also match a bare `hypershift`
server (never `hypershift-data`), and `pattern=10tb` match
`hypershift-data`.

Same shape as `INVENTORY_SITES`/`INVENTORY_GPU_MODELS`/
`INVENTORY_NIC_OS_NAMES` (`<token>:<name-pattern-alias>`, parsed once,
cached), so a future capacity tier is a configuration change, not code —
see docs/adr/0032-available-server-lookup-api.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

DEFAULT_CAPACITY_ALIASES_SPEC = "5tb:hypershift(?!-data),10tb:hypershift-data"


class CapacityAliasConfigurationError(ValueError):
    """Raised when `INVENTORY_CAPACITY_ALIASES` cannot be parsed."""


@dataclass(frozen=True, slots=True)
class CapacityAliasCatalog:
    """
    Capacity token -> the extra name-pattern alias it also matches.

    Attributes:
        aliases (dict[str, str]): Lowercased token -> regex alias.
    """

    aliases: dict[str, str]

    @classmethod
    def from_spec(cls, spec: str) -> CapacityAliasCatalog:
        """
        Parse `INVENTORY_CAPACITY_ALIASES` (`token:alias`, comma-separated).

        Args:
            spec (str): The raw configured value. Empty means the shipped
                default rather than "no aliases".

        Returns:
            CapacityAliasCatalog: The parsed catalog.

        Raises:
            CapacityAliasConfigurationError: On an entry with no `:`, an
                empty token, or an empty alias.
        """
        text = spec.strip() or DEFAULT_CAPACITY_ALIASES_SPEC
        aliases: dict[str, str] = {}
        for entry in text.split(","):
            candidate = entry.strip()
            if not candidate:
                continue
            token, separator, alias = candidate.partition(":")
            token = token.strip().lower()
            alias = alias.strip()
            if not separator or not token or not alias:
                raise CapacityAliasConfigurationError(
                    f"{candidate!r} is not a 'token:alias' entry. Expected e.g. "
                    "'5tb:hypershift(?!-data)'."
                )
            aliases[token] = alias
        return cls(aliases=aliases)

    def expansion_for(self, pattern: str) -> str | None:
        """
        The extra name-pattern alias for a pattern that is exactly one configured token.

        Args:
            pattern (str): The caller's `?pattern=` value, matched
                case-insensitively against a whole configured token — a
                pattern that merely contains the token as a substring of a
                larger regex does not match here (ADR-0032).

        Returns:
            str | None: The alias regex, or `None` when `pattern` isn't
                exactly a configured token.
        """
        return self.aliases.get(pattern.strip().lower())


@lru_cache(maxsize=8)
def capacity_alias_catalog(spec: str) -> CapacityAliasCatalog:
    """
    A cached catalog for one configured spec.

    Keyed on the spec, not `Settings`, so a test can pass a literal.

    Args:
        spec (str): The `INVENTORY_CAPACITY_ALIASES` value.

    Returns:
        CapacityAliasCatalog: The parsed catalog.

    Raises:
        CapacityAliasConfigurationError: On a malformed entry.
    """
    return CapacityAliasCatalog.from_spec(spec)
