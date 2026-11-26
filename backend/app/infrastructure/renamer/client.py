"""HTTP client for the renamer service's batch rename routes (ADR-0071).

The renamer answers a batch with 200 and per-server `renamed`/`failed` lists; a non-200, a
connect failure or an unreadable body fails the whole chunk as `UNSENT` (retried next run). A
read or write timeout is `FAILED`, never retried: the renamer may already have renamed them.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

_TRANSPORT_RETRIES = 2
OnChunk = Callable[[list["RenameResult"]], Awaitable[None]]
_DELL_BATCH = "/servers/dell/rename-batch"
_CISCO_BATCH = "/servers/cisco/rename-batch"


class RenameStatus(StrEnum):
    """What the renamer did with one server."""

    RENAMED = "renamed"
    UNCHANGED = "unchanged"
    WOULD_RENAME = "would_rename"
    CONF_MISMATCH = "conf_mismatch"
    FAILED = "failed"
    UNSENT = "unsent"


@dataclass(frozen=True, slots=True)
class RenameResult:
    """
    The renamer's answer for one identity (an iDRAC IP or a service-profile name).

    Attributes:
        identity (str): What was sent.
        status (RenameStatus): The outcome.
        new_profile (str | None): The convention name the renamer chose.
        conf_id (str | None): The matched hardware conf.
        variant (str | None): `d`, `n` or None.
        changed (bool): True only when a real rename happened.
        error (str | None): Why it failed or was not sent.
        nearest_confs (list[dict[str, Any]]): The closest confs on a conf mismatch.
    """

    identity: str
    status: RenameStatus
    new_profile: str | None = None
    conf_id: str | None = None
    variant: str | None = None
    changed: bool = False
    error: str | None = None
    nearest_confs: list[dict[str, Any]] = field(default_factory=list)


def build_verify(*, verify_tls: bool, ca_bundle: str) -> ssl.SSLContext | bool:
    """
    Build the TLS configuration for the renamer.

    Args:
        verify_tls (bool): False skips certificate verification.
        ca_bundle (str): A PEM file to trust instead of the system store, or "".

    Returns:
        ssl.SSLContext | bool: A verifying context, or False.
    """
    if not verify_tls:
        return False
    context = ssl.create_default_context(cafile=ca_bundle or None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _identity_of(entry: dict[str, Any]) -> str | None:
    """
    The identity a batch entry answers for.

    Args:
        entry (dict[str, Any]): One `renamed` or `failed` item.

    Returns:
        str | None: Its `idrac_ip` (also used for Cisco profile names), else `profile_name`.
    """
    value = entry.get("idrac_ip") or entry.get("profile_name")
    return str(value) if value else None


def parse_batch(body: dict[str, Any], requested: list[str], *, dry_run: bool) -> list[RenameResult]:
    """
    Turn a 200 batch body into one result per requested identity.

    Args:
        body (dict[str, Any]): `{"renamed": [...], "failed": [...]}`.
        requested (list[str]): The identities sent, in order.
        dry_run (bool): Whether the request was a dry run.

    Returns:
        list[RenameResult]: In `requested` order. One the body does not mention is UNSENT (retried
            next run); one it answers twice is FAILED, so a human looks before it is sent again.

    Raises:
        TypeError: When `renamed` or `failed` is not a list of objects.
    """
    renamed = body.get("renamed") or []
    failed = body.get("failed") or []
    if not isinstance(renamed, list) or not isinstance(failed, list):
        raise TypeError("renamed/failed is not a list")
    if not all(isinstance(e, dict) for e in [*renamed, *failed]):
        raise TypeError("a renamed/failed entry is not an object")
    found: dict[str, RenameResult] = {}
    twice: set[str] = set()

    def keep(result: RenameResult) -> None:
        """
        Store one answer, noting an identity answered more than once.

        Args:
            result (RenameResult): The answer.
        """
        if result.identity in found:
            twice.add(result.identity)
        found[result.identity] = result

    for entry in renamed:
        identity = _identity_of(entry)
        if identity is None:
            continue
        changed = bool(entry.get("changed"))
        if dry_run:
            status = RenameStatus.WOULD_RENAME
        else:
            status = RenameStatus.RENAMED if changed else RenameStatus.UNCHANGED
        keep(
            RenameResult(
                identity=identity,
                status=status,
                new_profile=entry.get("new_profile"),
                conf_id=entry.get("conf_id"),
                variant=entry.get("variant"),
                changed=changed,
            )
        )
    for entry in failed:
        identity = _identity_of(entry)
        if identity is None:
            continue
        nearest = entry.get("nearest_confs")
        nearest = [c for c in nearest if isinstance(c, dict)] if isinstance(nearest, list) else []
        mismatch = bool(nearest) or "components" in entry
        keep(
            RenameResult(
                identity=identity,
                status=RenameStatus.CONF_MISMATCH if mismatch else RenameStatus.FAILED,
                error=str(entry.get("error") or "") or None,
                nearest_confs=nearest,
            )
        )
    results: list[RenameResult] = []
    for identity in requested:
        if identity in twice:
            results.append(
                RenameResult(identity, RenameStatus.FAILED, error="the renamer answered it twice")
            )
        else:
            results.append(
                found.get(identity)
                or RenameResult(
                    identity, RenameStatus.UNSENT, error="missing from the renamer's response"
                )
            )
    return results


class RenamerClient:
    """The renamer's batch routes, HTTP Basic on every request, chunked."""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        verify: ssl.SSLContext | bool,
        timeout_seconds: float,
        chunk_size: int,
        budget_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Open the HTTP client.

        Args:
            base_url (str): The renamer's origin, e.g. `https://renamer.example`.
            username (str): The service account.
            password (str): Its password.
            verify (ssl.SSLContext | bool): TLS verification (`build_verify`).
            timeout_seconds (float): Read timeout; the renamer's route limit is 300 s.
            chunk_size (int): Identities per batch request.
            budget_seconds (float): No new chunk is sent after this long; the rest are UNSENT.
            transport (httpx.AsyncBaseTransport | None): A test transport.
        """
        self._chunk_size = max(1, chunk_size)
        self._deadline = time.monotonic() + budget_seconds
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=httpx.BasicAuth(username, password),
            verify=verify,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            transport=transport,
        )

    @property
    def chunk_size(self) -> int:
        """
        Identities per batch request.

        Returns:
            int: At least 1.
        """
        return self._chunk_size

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()

    async def rename_batch_dell(
        self, idrac_ips: list[str], *, dry_run: bool, on_chunk: OnChunk | None = None
    ) -> list[RenameResult]:
        """
        Rename Dell servers by iDRAC IP.

        Args:
            idrac_ips (list[str]): The iDRAC IPs.
            dry_run (bool): Ask the renamer to only report the name.
            on_chunk (OnChunk | None): Awaited with each chunk's results as soon as it returns.

        Returns:
            list[RenameResult]: One per IP, in order.
        """
        return await self._batches(_DELL_BATCH, idrac_ips, dry_run=dry_run, on_chunk=on_chunk)

    async def rename_batch_cisco(
        self, profile_names: list[str], *, dry_run: bool, on_chunk: OnChunk | None = None
    ) -> list[RenameResult]:
        """
        Rename Cisco servers by service-profile name.

        Args:
            profile_names (list[str]): The service-profile names.
            dry_run (bool): Ask the renamer to only report the name.
            on_chunk (OnChunk | None): Awaited with each chunk's results as soon as it returns.

        Returns:
            list[RenameResult]: One per profile, in order.
        """
        return await self._batches(_CISCO_BATCH, profile_names, dry_run=dry_run, on_chunk=on_chunk)

    async def _batches(
        self, path: str, identities: list[str], *, dry_run: bool, on_chunk: OnChunk | None
    ) -> list[RenameResult]:
        """
        Send `identities` in chunks, one after another, until the time budget is spent.

        Args:
            path (str): The batch route.
            identities (list[str]): Everything to rename.
            dry_run (bool): Passed through.
            on_chunk (OnChunk | None): Awaited with each chunk's results.

        Returns:
            list[RenameResult]: One per identity, in order.
        """
        results: list[RenameResult] = []
        for start in range(0, len(identities), self._chunk_size):
            chunk = identities[start : start + self._chunk_size]
            if time.monotonic() >= self._deadline:
                chunk_results = self._unsent(path, chunk, "the run's time budget is spent")
            else:
                chunk_results = await self._one_batch(path, chunk, dry_run=dry_run)
            if on_chunk is not None:
                await on_chunk(chunk_results)
            results.extend(chunk_results)
        return results

    async def _one_batch(self, path: str, chunk: list[str], *, dry_run: bool) -> list[RenameResult]:
        """
        Send one chunk, retrying only a failure to connect.

        The batch field is `idrac_ips` for both vendors; for Cisco it carries profile names.

        Args:
            path (str): The batch route.
            chunk (list[str]): The identities.
            dry_run (bool): Passed through.

        Returns:
            list[RenameResult]: One per identity; UNSENT when the request was not processed,
                FAILED when it timed out after being sent.
        """
        payload = {"idrac_ips": chunk, "dry_run": dry_run}
        logger.info("renamer.request", path=path, identities=chunk, dry_run=dry_run)
        for attempt in range(_TRANSPORT_RETRIES + 1):
            sent_at = time.monotonic()
            try:
                response = await self._http.post(path, json=payload)
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
                error = f"no answer after sending ({exc!r}); the renamer may have renamed it"
                logger.warning(
                    "renamer.batch_unanswered",
                    path=path,
                    identities=chunk,
                    elapsed_seconds=round(time.monotonic() - sent_at, 1),
                    error=error,
                )
                return [RenameResult(i, RenameStatus.FAILED, error=error) for i in chunk]
            except httpx.TransportError as exc:
                if attempt < _TRANSPORT_RETRIES:
                    logger.warning("renamer.retry", path=path, attempt=attempt + 1, error=repr(exc))
                    await self._backoff(attempt)
                    continue
                return self._unsent(path, chunk, f"transport error: {exc!r}")
            if response.status_code != 200:
                return self._unsent(
                    path, chunk, f"HTTP {response.status_code}: {response.text[:300]}"
                )
            try:
                body = response.json()
            except ValueError:
                return self._unsent(path, chunk, "the response is not JSON")
            if not isinstance(body, dict):
                return self._unsent(path, chunk, "the response is not a JSON object")
            try:
                results = parse_batch(body, chunk, dry_run=dry_run)
            except TypeError as exc:
                return self._unsent(path, chunk, f"unreadable response: {exc}")
            logger.info(
                "renamer.batch",
                path=path,
                status_code=response.status_code,
                elapsed_seconds=round(time.monotonic() - sent_at, 1),
                sent=len(chunk),
                ok=sum(
                    r.status in (RenameStatus.RENAMED, RenameStatus.WOULD_RENAME) for r in results
                ),
                dry_run=dry_run,
            )
            return results
        return self._unsent(path, chunk, "no attempt made")

    @staticmethod
    def _unsent(path: str, chunk: list[str], error: str) -> list[RenameResult]:
        """
        Mark a whole chunk as not processed by the renamer.

        Args:
            path (str): The batch route, for the log.
            chunk (list[str]): The identities.
            error (str): Why.

        Returns:
            list[RenameResult]: One UNSENT result per identity.
        """
        logger.error("renamer.batch_failed", path=path, identities=chunk, error=error)
        return [RenameResult(i, RenameStatus.UNSENT, error=error) for i in chunk]

    @staticmethod
    async def _backoff(attempt: int) -> None:
        """
        Wait before a connect retry: 1 s, then 2 s.

        Args:
            attempt (int): The failed attempt, from 0.
        """
        await asyncio.sleep(2**attempt)
