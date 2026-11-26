"""CLI: copy every physical server's warranty from the operator's warranty collection (ADR-0070).

Run monthly by the `warranty-sync` CronJob. A new server is looked up when a collector creates it,
and "Fetch now" looks its server up again, so this pass catches renewals and late records.

Usage:
    uv run python -m tools.sync_warranty

Exit codes: 0 done (or warranty off), 1 the collection could not be read.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

import structlog

from app.config import get_settings
from app.domain.enums import ServerType, Vendor, server_type_of
from app.domain.models.warranty import Warranty
from app.domain.services.normalize import normalize_text
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.warranty_source import warranty_source

logger = structlog.get_logger(__name__)

_MAX_ATTEMPTS = 3


@dataclass(slots=True)
class SyncSummary:
    """
    What one sync pass did.

    Attributes:
        updated (int): Servers whose warranty was written.
        unchanged (int): Servers already holding the wanted warranty.
        no_record (int): Physical servers with no document for their serial.
        unread_date (int): Servers whose document had no readable date.
        conflicts (int): Servers still changing after every CAS attempt.
    """

    updated: int = 0
    unchanged: int = 0
    no_record: int = 0
    unread_date: int = 0
    conflicts: int = 0


def wanted_warranty(doc: dict[str, Any], records: dict[str, Warranty]) -> Warranty | None:
    """
    The warranty a server should hold, from its projected document and every record.

    Args:
        doc (dict[str, Any]): A `warranty_targets` document.
        records (dict[str, Warranty]): Normalized serial -> warranty.

    Returns:
        Warranty | None: None for a VM or a serial-less server.
    """
    identity = doc.get("identity") or {}
    serial = normalize_text(identity.get("serial"))
    try:
        physical = server_type_of(Vendor(identity.get("vendor"))) is ServerType.PHYSICAL
    except ValueError:
        logger.warning("warranty.unknown_vendor", server_id=doc.get("_id"))
        return None
    if not physical or not serial:
        return None
    return records.get(serial) or Warranty(found=False)


async def sync_warranty(repo: MongoServerRepository, records: dict[str, Warranty]) -> SyncSummary:
    """
    Write each server's warranty where it differs from the stored one.

    Args:
        repo (MongoServerRepository): The servers collection.
        records (dict[str, Warranty]): Normalized serial -> warranty.

    Returns:
        SyncSummary: The counts.
    """
    summary = SyncSummary()
    for doc in await repo.warranty_targets():
        wanted = wanted_warranty(doc, records)
        if wanted is not None and not wanted.found:
            summary.no_record += 1
        if wanted is not None and wanted.found and wanted.expires_on is None:
            summary.unread_date += 1
        new = wanted.model_dump(mode="json") if wanted is not None else None
        if (doc.get("warranty") or None) == new:
            summary.unchanged += 1
            continue
        revision = doc.get("revision", 0)
        for _ in range(_MAX_ATTEMPTS):
            if await repo.set_warranty(doc["_id"], new, expected_revision=revision):
                summary.updated += 1
                break
            fresh = await repo.get_by_id(doc["_id"])
            if fresh is None:
                break
            fresh_dump = fresh.warranty.model_dump(mode="json") if fresh.warranty else None
            if fresh.warranty == wanted or fresh_dump != (doc.get("warranty") or None):
                summary.unchanged += 1
                break
            revision = fresh.revision
        else:
            summary.conflicts += 1
            logger.warning("warranty.sync_conflict", server_id=doc["_id"])
    return summary


async def _run() -> int:
    """
    One sync pass against the configured databases.

    Returns:
        int: The process exit code.
    """
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )
    source = warranty_source(
        settings.warranty_mongo_uri, settings.warranty_database, settings.warranty_collection
    )
    if source is None:
        logger.info("warranty.sync_disabled", reason="INVENTORY_WARRANTY_MONGO_URI is empty")
        return 0
    records = await source.all()
    if records is None:
        return 1
    if not records:
        logger.error(
            "warranty.sync_refused",
            reason="the collection is empty or misnamed; writing would mark every server no record",
            database=settings.warranty_database,
            collection=settings.warranty_collection,
        )
        return 1
    mongo = MongoClientHolder(settings)
    await mongo.connect()
    try:
        repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
        summary = await sync_warranty(repo, records)
    finally:
        await mongo.close()
    logger.info(
        "warranty.sync_completed",
        records=len(records),
        updated=summary.updated,
        unchanged=summary.unchanged,
        no_record=summary.no_record,
        unread_date=summary.unread_date,
        conflicts=summary.conflicts,
    )
    return 0


def main() -> None:
    """Entry point for `python -m tools.sync_warranty`."""
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
