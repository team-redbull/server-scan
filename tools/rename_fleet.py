"""CLI: ask the renamer to rename free Dell and Cisco servers to the fleet convention (ADR-0071).

Run every 5 minutes by the `rename-fleet` CronJob. Report-only (the renamer's `dry_run`) until
`INVENTORY_RENAMER_REPORT_ONLY=false`. Every answered server gets one `RENAME_REQUESTED` event,
which is also the cooldown ledger: a server with one newer than the cooldown is not sent again.

Usage:
    uv run python -m tools.rename_fleet

Exit codes: 0 done (or no renamer URL), 1 MongoDB could not be read, 3 some servers failed or
were not answered.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from pymongo.errors import PyMongoError

from app.application.services.audit_service import AuditService
from app.config import get_settings
from app.domain.enums import ManagerType, Vendor
from app.domain.models.audit_event import Actor, ActorType, EventType
from app.domain.models.reservation import Reservation
from app.domain.value_objects.fleet_name import matches_fleet_convention
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.renamer.client import (
    RenamerClient,
    RenameResult,
    RenameStatus,
    build_verify,
)
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)

_EXAMPLES = 10

RENAME_ACTOR = Actor(type=ActorType.SYSTEM, id="rename-fleet")

# The collectors whose servers the renamer manages: OME profiles and UCS Central service profiles.
_MANAGED = {
    Vendor.DELL.value: {ManagerType.OPENMANAGE.value},
    Vendor.CISCO.value: {ManagerType.UCS_CENTRAL.value, ManagerType.UCS_MANAGER.value},
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """
    One server to send to the renamer.

    Attributes:
        server_id (str): Its `_id`.
        name (str): Its current name.
        vendor (str): `dell` or `cisco`.
        identity (str): The iDRAC IP (Dell) or the service-profile name (Cisco).
        serial (str | None): For the audit event.
        bmc (str | None): For the audit event.
    """

    server_id: str
    name: str
    vendor: str
    identity: str
    serial: str | None
    bmc: str | None


@dataclass(slots=True)
class SweepSummary:
    """
    What one sweep did; the `rename_fleet.completed` log fields.

    Attributes:
        candidates (int): Free, reachable, out-of-maintenance Dell and Cisco servers.
        already_in_convention (int): Named by the convention already.
        skipped_excluded_name (int): The name contains an excluded string (e.g. `-mce-`, `-seed-`).
        skipped_unmanaged (int): From a collector the renamer does not manage.
        skipped_reserved (int): Held by a live reservation.
        skipped_no_bmc_ip (int): Dell servers whose BMC host is not an IP.
        skipped_cooldown (int): Answered by the renamer within the cooldown.
        over_cap (int): Left for the next run by the per-run cap.
        dropped_on_recheck (int): Reserved, claimed or paused between selection and sending.
        renamed (int): Renamed for real.
        unchanged (int): Real run, but the profile already had that name.
        would_rename (int): Dry run: the renamer found a name.
        conf_mismatch (int): No conf matches the hardware; a human decides.
        failed (int): The renamer answered with another failure.
        unsent (int): The request failed as a whole; retried next run.
        examples (dict[str, list[str]]): Up to 10 server names per skip reason, for the log.
    """

    candidates: int = 0
    already_in_convention: int = 0
    skipped_excluded_name: int = 0
    skipped_unmanaged: int = 0
    skipped_reserved: int = 0
    skipped_no_bmc_ip: int = 0
    skipped_cooldown: int = 0
    over_cap: int = 0
    dropped_on_recheck: int = 0
    renamed: int = 0
    unchanged: int = 0
    would_rename: int = 0
    conf_mismatch: int = 0
    failed: int = 0
    unsent: int = 0
    examples: dict[str, list[str]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        """
        Every counter, for the completion log.

        Returns:
            dict[str, int]: Field name to count.
        """
        return {k: getattr(self, k) for k in self.__slots__ if k != "examples"}

    def skip(self, reason: str, name: str) -> None:
        """
        Count one skipped server and keep its name as an example (at most 10 per reason).

        Args:
            reason (str): The counter to increment.
            name (str): The server's name.
        """
        setattr(self, reason, getattr(self, reason) + 1)
        names = self.examples.setdefault(reason, [])
        if len(names) < _EXAMPLES:
            names.append(name)


def select_candidates(
    docs: list[dict[str, Any]],
    *,
    cooled: set[str],
    now: datetime,
    max_per_run: int,
    summary: SweepSummary,
    skip_name_contains: frozenset[str] = frozenset(),
) -> list[Candidate]:
    """
    Pick the servers to send this run, counting every one skipped.

    Args:
        docs (list[dict[str, Any]]): `rename_candidates` documents.
        cooled (set[str]): Server ids still in cooldown.
        now (datetime): The run's clock, for reservations.
        max_per_run (int): The most servers sent in one run.
        summary (SweepSummary): Updated in place.
        skip_name_contains (frozenset[str]): Lower-cased strings; a name containing one is skipped.

    Returns:
        list[Candidate]: Ordered by name, at most `max_per_run`.
    """
    picked: list[Candidate] = []
    for doc in sorted(docs, key=lambda d: str(d.get("name") or "")):
        summary.candidates += 1
        name = str(doc.get("name") or "")
        identity_doc = doc.get("identity") or {}
        vendor = str(identity_doc.get("vendor") or "")
        bmc = (doc.get("network") or {}).get("bmc") or {}
        if any(part in name.lower() for part in skip_name_contains):
            summary.skip("skipped_excluded_name", name)
            continue
        if matches_fleet_convention(name):
            summary.skip("already_in_convention", name)
            continue
        if doc.get("source_provider") not in _MANAGED.get(vendor, set()):
            summary.skip("skipped_unmanaged", name)
            continue
        if Reservation.model_validate(doc.get("reservation") or {}).is_live(now=now):
            summary.skip("skipped_reserved", name)
            continue
        if vendor == Vendor.DELL.value and not (bmc.get("host") and bmc.get("host_is_ip")):
            summary.skip("skipped_no_bmc_ip", name)
            continue
        if doc["_id"] in cooled:
            summary.skip("skipped_cooldown", name)
            continue
        if len(picked) >= max_per_run:
            summary.skip("over_cap", name)
            continue
        picked.append(
            Candidate(
                server_id=doc["_id"],
                name=name,
                vendor=vendor,
                identity=bmc["host"] if vendor == Vendor.DELL.value else name,
                serial=identity_doc.get("serial"),
                bmc=bmc.get("host"),
            )
        )
    return picked


def _count(summary: SweepSummary, result: RenameResult) -> None:
    """
    Add one renamer result to the summary.

    Args:
        summary (SweepSummary): Updated in place.
        result (RenameResult): The outcome.
    """
    field = {
        RenameStatus.RENAMED: "renamed",
        RenameStatus.UNCHANGED: "unchanged",
        RenameStatus.WOULD_RENAME: "would_rename",
        RenameStatus.CONF_MISMATCH: "conf_mismatch",
        RenameStatus.FAILED: "failed",
        RenameStatus.UNSENT: "unsent",
    }[result.status]
    setattr(summary, field, getattr(summary, field) + 1)


def _log_result(candidate: Candidate, result: RenameResult, *, dry_run: bool) -> None:
    """
    One log line per server the renamer was asked about: what was sent and what came back.

    Args:
        candidate (Candidate): The server.
        result (RenameResult): The renamer's answer.
        dry_run (bool): Report-only.
    """
    ok = result.status in (
        RenameStatus.RENAMED,
        RenameStatus.UNCHANGED,
        RenameStatus.WOULD_RENAME,
    )
    (logger.info if ok else logger.warning)(
        "rename_fleet.server",
        outcome=result.status.value,
        dry_run=dry_run,
        server_id=candidate.server_id,
        server_name=candidate.name,
        vendor=candidate.vendor,
        sent=candidate.identity,
        serial=candidate.serial,
        name_to=result.new_profile,
        conf_id=result.conf_id,
        variant=result.variant,
        error=result.error,
        nearest_confs=[c.get("conf_id") for c in result.nearest_confs],
    )


async def _still_eligible(
    repo: MongoServerRepository, candidates: list[Candidate], summary: SweepSummary
) -> list[Candidate]:
    """
    Re-read candidates just before sending, dropping any reserved, claimed or paused since.

    Args:
        repo (MongoServerRepository): The servers collection.
        candidates (list[Candidate]): One chunk's servers.
        summary (SweepSummary): `dropped_on_recheck` is updated in place.

    Returns:
        list[Candidate]: Those still free, reachable, out of maintenance and unreserved.
    """
    now = utcnow()
    docs = await repo.rename_candidates(list(_MANAGED), ids=[c.server_id for c in candidates])
    free = {
        d["_id"]
        for d in docs
        if not Reservation.model_validate(d.get("reservation") or {}).is_live(now=now)
    }
    kept = [c for c in candidates if c.server_id in free]
    for candidate in candidates:
        if candidate.server_id not in free:
            summary.skip("dropped_on_recheck", candidate.name)
            logger.info(
                "rename_fleet.dropped_on_recheck",
                server_id=candidate.server_id,
                server_name=candidate.name,
                reason="reserved, claimed, in maintenance or unreachable since selection",
            )
    return kept


async def _send(
    repo: MongoServerRepository,
    client: RenamerClient,
    audit: AuditService,
    vendor: str,
    candidates: list[Candidate],
    *,
    dry_run: bool,
    summary: SweepSummary,
) -> None:
    """
    Send one vendor's candidates chunk by chunk, re-checked just before and audited just after.

    Two servers sharing an identity (e.g. one iDRAC IP) are sent once and both get its answer.

    Args:
        repo (MongoServerRepository): The servers collection, for the re-check.
        client (RenamerClient): The renamer.
        audit (AuditService): Writes `RENAME_REQUESTED`.
        vendor (str): `dell` or `cisco`.
        candidates (list[Candidate]): That vendor's servers.
        dry_run (bool): Report-only.
        summary (SweepSummary): Updated in place.
    """
    by_identity: dict[str, list[Candidate]] = {}

    async def record(results: list[RenameResult]) -> None:
        """
        Count and audit one chunk's answers.

        Args:
            results (list[RenameResult]): The chunk's results.
        """
        for result in results:
            for candidate in by_identity.get(result.identity, []):
                _count(summary, result)
                _log_result(candidate, result, dry_run=dry_run)
                if result.status is RenameStatus.UNSENT:
                    continue
                await audit.record(
                    EventType.RENAME_REQUESTED,
                    actor=RENAME_ACTOR,
                    server_id=candidate.server_id,
                    server_name=candidate.name,
                    data={
                        "outcome": result.status.value,
                        "dry_run": dry_run,
                        "vendor": candidate.vendor,
                        "identity": candidate.identity,
                        "serial": candidate.serial,
                        "bmc": candidate.bmc,
                        "name_from": candidate.name,
                        "name_to": result.new_profile,
                        "conf_id": result.conf_id,
                        "variant": result.variant,
                        "error": result.error,
                        "nearest_confs": result.nearest_confs,
                    },
                )

    send = client.rename_batch_dell if vendor == Vendor.DELL.value else client.rename_batch_cisco
    for start in range(0, len(candidates), client.chunk_size):
        chunk = await _still_eligible(repo, candidates[start : start + client.chunk_size], summary)
        by_identity.clear()
        for candidate in chunk:
            by_identity.setdefault(candidate.identity, []).append(candidate)
        if by_identity:
            await send(list(by_identity), dry_run=dry_run, on_chunk=record)


async def sweep(
    repo: MongoServerRepository,
    audit: AuditService,
    client: RenamerClient,
    *,
    dry_run: bool,
    cooldown: timedelta,
    max_per_run: int,
    skip_name_contains: frozenset[str] = frozenset(),
) -> SweepSummary:
    """
    One rename sweep: select, send per vendor, audit every answered server.

    Args:
        repo (MongoServerRepository): The servers collection.
        audit (AuditService): Writes `RENAME_REQUESTED`, and answers the cooldown.
        client (RenamerClient): The renamer.
        dry_run (bool): Report-only.
        cooldown (timedelta): How long an answered server is left alone.
        max_per_run (int): The most servers sent in one run.
        skip_name_contains (frozenset[str]): Lower-cased strings that keep a server out.

    Returns:
        SweepSummary: The counts.
    """
    now = utcnow()
    summary = SweepSummary()
    docs = await repo.rename_candidates(list(_MANAGED))
    cooled = await audit.server_ids_with_event_since(EventType.RENAME_REQUESTED, now - cooldown)
    picked = select_candidates(
        docs,
        cooled=cooled,
        now=now,
        max_per_run=max_per_run,
        summary=summary,
        skip_name_contains=skip_name_contains,
    )
    logger.info(
        "rename_fleet.selected",
        to_send=[f"{c.name} ({c.identity})" for c in picked],
        cooldown_servers=len(cooled),
        **summary.counts(),
    )
    for vendor in (Vendor.DELL.value, Vendor.CISCO.value):
        mine = [c for c in picked if c.vendor == vendor]
        if mine:
            await _send(repo, client, audit, vendor, mine, dry_run=dry_run, summary=summary)
    return summary


async def _run() -> int:
    """
    One sweep against the configured database and renamer.

    Returns:
        int: The process exit code.
    """
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )
    if not settings.renamer_url:
        logger.info("rename_fleet.disabled", reason="INVENTORY_RENAMER_URL is empty")
        return 0
    client = RenamerClient(
        base_url=settings.renamer_url,
        username=settings.renamer_username,
        password=settings.renamer_password.get_secret_value(),
        verify=build_verify(
            verify_tls=settings.renamer_verify_tls, ca_bundle=settings.renamer_ca_bundle
        ),
        timeout_seconds=settings.renamer_timeout_seconds,
        chunk_size=settings.renamer_chunk_size,
        budget_seconds=settings.renamer_run_budget_seconds,
    )
    logger.info(
        "rename_fleet.started",
        dry_run=settings.renamer_report_only,
        renamer_url=settings.renamer_url,
        verify_tls=settings.renamer_verify_tls,
        chunk_size=settings.renamer_chunk_size,
        max_per_run=settings.renamer_max_per_run,
        cooldown_hours=settings.renamer_cooldown_hours,
        run_budget_seconds=settings.renamer_run_budget_seconds,
        skip_name_contains=settings.renamer_skip_name_contains,
    )
    started = time.monotonic()
    mongo = MongoClientHolder(settings)
    try:
        await mongo.connect()
        summary = await sweep(
            MongoServerRepository(mongo, cursor_secret=settings.cursor_secret),
            AuditService(repo=MongoAuditEventRepository(mongo)),
            client,
            dry_run=settings.renamer_report_only,
            cooldown=timedelta(hours=settings.renamer_cooldown_hours),
            max_per_run=settings.renamer_max_per_run,
            skip_name_contains=frozenset(
                p.strip().lower()
                for p in settings.renamer_skip_name_contains.split(",")
                if p.strip()
            ),
        )
    except PyMongoError:
        logger.exception("rename_fleet.mongo_failed")
        return 1
    finally:
        await client.close()
        await mongo.close()
    logger.info(
        "rename_fleet.completed",
        dry_run=settings.renamer_report_only,
        duration_seconds=round(time.monotonic() - started, 1),
        skipped_examples=summary.examples,
        **summary.counts(),
    )
    return 3 if summary.failed or summary.unsent else 0


def main() -> None:
    """Entry point for `python -m tools.rename_fleet`."""
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
