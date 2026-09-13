"""`RegexModuleEngine`: the default `RegexEngine` implementation.

Uses the third-party `regex` module (already a project dependency), not
stdlib `re`, specifically for its `timeout=` parameter on `search()` —
stdlib `re` has no way to bound matching time at all, which is what makes
catastrophic backtracking (ReDoS) possible from a user-authored pattern in
the first place. `regex`'s timeout is checked at internal backtracking
checkpoints, not preemptively, so it bounds *most* pathological cases but
isn't a hard real-time guarantee; `RegexEngine` staying a Protocol is the
mitigation if a case is found that needs Google's RE2 (which forbids
backtracking entirely, at the cost of backreferences/lookarounds working
at all) instead.

Deliberately domain, not infrastructure: this is CPU-bound pure
computation with no external I/O (no network, no filesystem, no database)
— it doesn't belong behind the same kind of port that Mongo/Redis clients
sit behind, and keeping it in `domain/services` means the classification
engine's tests exercise the real regex behavior, not a mock of it.
"""

from __future__ import annotations

from functools import lru_cache

import regex

from app.domain.ports.regex_engine import RegexMatch, RegexTimeout, RegexUnsafeError

# Probed at validate() time under the real match timeout. 2000 chars, not
# ~40: `regex` resists `(a+)+$`-style blowup far longer than stdlib `re`
# does — see docs/architecture.md, "Classification engine".
_CANARY_INPUTS = (
    "",
    "a" * 200,
    "a" * 2000 + "!",
    "ocp-dell-worker-0001-" + "x" * 100,
)


class RegexModuleEngine:
    """The `RegexEngine` `Protocol`'s implementation, backed by the third-party `regex` module."""

    def __init__(self, *, max_pattern_length: int, match_timeout_seconds: float) -> None:
        """
        Configure the pattern length ceiling and match/canary time budget.

        Args:
            max_pattern_length (int): The maximum accepted pattern length, in characters.
            match_timeout_seconds (float): The time budget for `search()`
                and for each canary probe in `validate()`.
        """
        self._max_pattern_length = max_pattern_length
        self._timeout = match_timeout_seconds
        self._compile = lru_cache(maxsize=1024)(self._compile_uncached)

    def _flags_bitmask(self, *, ignore_case: bool, multiline: bool, dotall: bool) -> int:
        flags = 0
        if ignore_case:
            flags |= regex.IGNORECASE
        if multiline:
            flags |= regex.MULTILINE
        if dotall:
            flags |= regex.DOTALL
        return flags

    def _compile_uncached(self, pattern: str, flags: int) -> regex.Pattern[str]:
        return regex.compile(pattern, flags=flags)

    def validate(self, pattern: str, *, ignore_case: bool, multiline: bool, dotall: bool) -> None:
        """
        Reject a pattern that is too long, doesn't compile, or fails a canary timing probe.

        Args:
            pattern (str): The regex source to validate.
            ignore_case (bool): Whether matching would be case-insensitive.
            multiline (bool): Whether `^`/`$` would match at line boundaries.
            dotall (bool): Whether `.` would match a newline.

        Raises:
            RegexUnsafeError: If `pattern` exceeds `max_pattern_length`,
                fails to compile, or times out against any canary input.
        """
        if len(pattern) > self._max_pattern_length:
            raise RegexUnsafeError(
                f"pattern length {len(pattern)} exceeds max {self._max_pattern_length}"
            )
        try:
            compiled = self._compile(
                pattern,
                self._flags_bitmask(ignore_case=ignore_case, multiline=multiline, dotall=dotall),
            )
        except regex.error as exc:
            raise RegexUnsafeError(f"pattern does not compile: {exc}") from exc

        for canary in _CANARY_INPUTS:
            try:
                compiled.search(canary, timeout=self._timeout)
            except TimeoutError as exc:
                raise RegexUnsafeError(
                    f"pattern exceeded {self._timeout}s against a canary input"
                ) from exc

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
            pattern (str): The regex source to match. Should already have
                passed `validate()`.
            subject (str): The text to search.
            ignore_case (bool): Whether matching is case-insensitive.
            multiline (bool): Whether `^`/`$` match at line boundaries.
            dotall (bool): Whether `.` matches a newline.

        Returns:
            RegexMatch | None: The first match, or `None` if there isn't one.

        Raises:
            RegexTimeout: If matching exceeds `match_timeout_seconds`.
        """
        compiled = self._compile(
            pattern,
            self._flags_bitmask(ignore_case=ignore_case, multiline=multiline, dotall=dotall),
        )
        try:
            match = compiled.search(subject, timeout=self._timeout)
        except TimeoutError as exc:
            raise RegexTimeout(f"pattern exceeded {self._timeout}s against subject") from exc
        if match is None:
            return None
        return RegexMatch(start=match.start(), end=match.end())
