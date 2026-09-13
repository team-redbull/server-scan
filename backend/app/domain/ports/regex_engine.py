"""The `RegexEngine` port.

Declared here so `domain.services.classification` depends only on this
Protocol, never on a specific regex library — swapping the implementation
(e.g. to Google's RE2 for a stricter linear-time guarantee, at the cost of
losing backreferences/lookarounds) is a one-line change at the call site,
not a rewrite of the classification engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class RegexTimeout(Exception):
    """
    Raised when a match against a subject exceeds the configured time budget.

    The classification engine catches this per rule and skips it, never stalling the run.
    """


class RegexUnsafeError(Exception):
    """
    Raised at compile time for a pattern rejected before it's ever run.

    A pattern is rejected for being too long or for failing a canary
    timing probe against known-pathological inputs.
    """


@dataclass(frozen=True, slots=True)
class RegexMatch:
    """The span of one successful match, start/end offsets into the subject string."""

    start: int
    end: int


class RegexEngine(Protocol):
    """The regex operations `classification` depends on, not on a specific library."""

    def validate(self, pattern: str, *, ignore_case: bool, multiline: bool, dotall: bool) -> None:
        """
        Reject a pattern that should never be accepted, at rule write time.

        Args:
            pattern (str): The regex source to validate.
            ignore_case (bool): Whether matching would be case-insensitive.
            multiline (bool): Whether `^`/`$` would match at line boundaries.
            dotall (bool): Whether `.` would match a newline.

        Raises:
            RegexUnsafeError: If `pattern` is too long or fails a timeout canary.
        """
        ...

    def search(
        self,
        pattern: str,
        subject: str,
        *,
        ignore_case: bool,
        multiline: bool,
        dotall: bool,
    ) -> RegexMatch | None:
        """
        Search `subject` for the first match of `pattern`.

        Args:
            pattern (str): The regex source to match.
            subject (str): The text to search.
            ignore_case (bool): Whether matching is case-insensitive.
            multiline (bool): Whether `^`/`$` match at line boundaries.
            dotall (bool): Whether `.` matches a newline.

        Returns:
            RegexMatch | None: The first match, or `None` if there isn't one.

        Raises:
            RegexTimeout: If matching exceeds the configured time budget.
        """
        ...
