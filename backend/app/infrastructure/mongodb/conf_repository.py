"""MongoDB repository for the `confs` collection (docs/adr/0061-confs.md).

Another system writes this collection, so every read projects `_id` away and
looks a conf up by its `conf_id` field, never by `_id`.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from pydantic import ValidationError
from pymongo import ReplaceOne
from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.conf import Conf
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import CONFS_COLLECTION

logger = structlog.get_logger(__name__)

_Document = dict[str, Any]

_NO_ID = {"_id": 0}
_DIGITS = re.compile(r"(\d+)")


def conf_sort_key(conf_id: str) -> tuple[tuple[int, int, str], ...]:
    """
    Order conf ids naturally, so `CONF9` comes before `CONF10`.

    Args:
        conf_id (str): The id, e.g. `"CONF200.2"`.

    Returns:
        tuple[tuple[int, int, str], ...]: A key where digit runs compare as numbers.
    """
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
        for part in _DIGITS.split(conf_id)
        if part
    )


class MongoConfRepository:
    """Reads (and, for the fake seeder only, writes) conf documents."""

    def __init__(self, mongo: MongoClientHolder) -> None:
        """
        Store the shared Mongo client holder.

        Args:
            mongo (MongoClientHolder): The connected client holder.
        """
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[CONFS_COLLECTION]

    async def list_all(self) -> tuple[list[Conf], list[str]]:
        """
        List every conf, in natural `conf_id` order.

        A document that does not match the schema is logged and skipped
        rather than failing the whole list.

        Returns:
            tuple[list[Conf], list[str]]: The readable confs, and the
                `conf_id` (or `"?"`) of each document that was skipped.
        """
        docs = await self._collection.find({}, _NO_ID).to_list(length=None)
        confs: list[Conf] = []
        unreadable: list[str] = []
        for doc in docs:
            try:
                confs.append(Conf.model_validate(doc))
            except ValidationError as exc:
                conf_id = str(doc.get("conf_id") or "?")
                unreadable.append(conf_id)
                logger.warning(
                    "confs.unreadable_document", conf_id=conf_id, errors=exc.error_count()
                )
        confs.sort(key=lambda conf: conf_sort_key(conf.conf_id))
        unreadable.sort(key=conf_sort_key)
        return confs, unreadable

    async def get(self, conf_id: str) -> Conf | None:
        """
        Look up one conf by its `conf_id`.

        Args:
            conf_id (str): The conf's id.

        Returns:
            Conf | None: The conf, or None if no document has that id.

        Raises:
            ValidationError: The stored document does not match the schema.
        """
        doc = await self._collection.find_one({"conf_id": conf_id}, _NO_ID)
        if doc is None:
            return None
        return Conf.model_validate(doc)

    async def upsert_many(self, confs: list[Conf]) -> int:
        """
        Replace-or-insert each conf by `conf_id`; only the fake seeder calls this.

        Args:
            confs (list[Conf]): The confs to write.

        Returns:
            int: How many documents were inserted or changed.
        """
        if not confs:
            return 0
        result = await self._collection.bulk_write(
            [
                ReplaceOne({"conf_id": conf.conf_id}, conf.model_dump(mode="json"), upsert=True)
                for conf in confs
            ]
        )
        return result.upserted_count + result.modified_count
