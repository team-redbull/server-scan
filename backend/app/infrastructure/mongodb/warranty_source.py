"""
Reads the operator's warranty collection (ADR-0070): another database, possibly another cluster.

Read-only: one client per process and URI, reused by every lookup; `_id` never read. The
writer's document shape is `Serial`, `Warranty` (`DD-MM-YYYY`) and fields Server Scan already
reads from the hardware itself.
"""

from __future__ import annotations

import asyncio
import re
import weakref
from datetime import date, datetime
from typing import Any

import structlog
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from app.domain.models.warranty import Warranty
from app.domain.services.normalize import is_placeholder_serial, normalize_text

logger = structlog.get_logger(__name__)

_PROJECTION = {"_id": 0, "Serial": 1, "Warranty": 1}
_TIMEOUT_MS = 5_000
_UNREAD_SAMPLES = 10
_clients: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, AsyncMongoClient[dict[str, Any]]]
] = weakref.WeakKeyDictionary()


def _client(uri: str) -> AsyncMongoClient[dict[str, Any]]:
    """
    This event loop's client for `uri`, created on first use (a client is bound to its loop).

    Args:
        uri (str): The warranty database's connection string.

    Returns:
        AsyncMongoClient[dict[str, Any]]: The shared client.
    """
    per_loop = _clients.setdefault(asyncio.get_running_loop(), {})
    if uri not in per_loop:
        per_loop[uri] = AsyncMongoClient(
            uri, serverSelectionTimeoutMS=_TIMEOUT_MS, timeoutMS=30_000
        )
    return per_loop[uri]


def parse_warranty_date(value: object) -> str | None:
    """
    The end date as ISO `YYYY-MM-DD`, from the writer's `DD-MM-YYYY` (or a stored date).

    Args:
        value (object): The document's `Warranty` field.

    Returns:
        str | None: The ISO date, or None for an empty or unreadable value.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%d-%m-%Y").date().isoformat()
    except ValueError:
        return None


def _later(a: str | None, b: str | None) -> str | None:
    """
    The later of two ISO dates; a readable date beats an unread one.

    Args:
        a (str | None): One ISO date or None.
        b (str | None): The other.

    Returns:
        str | None: The later date.
    """
    if a is None or b is None:
        return a or b
    return max(a, b)


class MongoWarrantySource:
    """`WarrantySource` over a MongoDB collection Server Scan does not own."""

    def __init__(self, uri: str, database: str, collection: str) -> None:
        """
        Remember where the collection lives; nothing connects until a read.

        Args:
            uri (str): The warranty database's connection string.
            database (str): Its database name.
            collection (str): Its collection name.
        """
        self._uri = uri
        self._database = database
        self._collection = collection
        self._down = False

    async def _find(self, query: dict[str, Any]) -> list[dict[str, Any]] | None:
        """
        Run one `find`, or None (logged) when the collection cannot be read.

        After one failure this source stops trying: a down database costs a run one timeout.

        Args:
            query (dict[str, Any]): The filter.

        Returns:
            list[dict[str, Any]] | None: The projected documents.
        """
        if self._down:
            return None
        try:
            cursor = _client(self._uri)[self._database][self._collection].find(query, _PROJECTION)
            return await cursor.to_list(length=None)
        except PyMongoError as exc:
            logger.warning(
                "warranty.unreadable",
                database=self._database,
                collection=self._collection,
                error=str(exc),
            )
            self._down = True
            return None

    def _fold(self, docs: list[dict[str, Any]]) -> dict[str, Warranty]:
        """
        Group documents by normalized serial, the latest date winning; log the unread ones.

        Args:
            docs (list[dict[str, Any]]): Projected warranty documents.

        Returns:
            dict[str, Warranty]: Normalized serial -> warranty.
        """
        dates: dict[str, str | None] = {}
        duplicates = 0
        unread: list[str] = []
        for doc in docs:
            raw = doc.get("Serial")
            if isinstance(raw, float) and raw.is_integer():
                raw = int(raw)
            serial = normalize_text(str(raw or ""))
            if not serial:
                continue
            expires = parse_warranty_date(doc.get("Warranty"))
            if expires is None:
                unread.append(str(doc.get("Serial")))
            if serial in dates:
                duplicates += 1
                dates[serial] = _later(dates[serial], expires)
            else:
                dates[serial] = expires
        if unread:
            logger.warning(
                "warranty.date_unread", count=len(unread), serials=unread[:_UNREAD_SAMPLES]
            )
        if duplicates:
            logger.info("warranty.duplicate_serials", count=duplicates)
        return {
            serial: Warranty(found=True, expires_on=expires) for serial, expires in dates.items()
        }

    async def all(self) -> dict[str, Warranty] | None:
        """
        Every serial's warranty.

        Returns:
            dict[str, Warranty] | None: Normalized serial -> warranty; None if unreadable.
        """
        docs = await self._find({})
        return None if docs is None else self._fold(docs)

    async def one(self, serial: str) -> Warranty | None:
        """
        One serial's warranty, matched case-insensitively.

        Args:
            serial (str): The serial as collected.

        Returns:
            Warranty | None: `found=False` when absent; None if unreadable.
        """
        wanted = normalize_text(serial)
        if not wanted or is_placeholder_serial(serial):
            return None
        tokens = "\\s+".join(re.escape(token) for token in wanted.split())
        query: dict[str, Any] = {"Serial": {"$regex": f"^\\s*{tokens}\\s*$", "$options": "i"}}
        if wanted.isdigit():
            query = {"$or": [query, {"Serial": {"$in": [int(wanted), float(wanted)]}}]}
        docs = await self._find(query)
        if docs is None:
            return None
        found = self._fold(docs).get(wanted)
        return found or Warranty(found=False)


def warranty_source(uri: str, database: str, collection: str) -> MongoWarrantySource | None:
    """
    The configured warranty source, or None when no URI is set (warranty off).

    Args:
        uri (str): `INVENTORY_WARRANTY_MONGO_URI`.
        database (str): `INVENTORY_WARRANTY_DATABASE`.
        collection (str): `INVENTORY_WARRANTY_COLLECTION`.

    Returns:
        MongoWarrantySource | None: The source, or None.
    """
    return MongoWarrantySource(uri, database, collection) if uri else None
