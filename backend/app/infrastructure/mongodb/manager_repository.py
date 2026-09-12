"""Minimal MongoDB repository for the `managers` collection.

Same rationale as `site_repository.py`: managers are a small, human-curated
reference collection, so a plain `list_all()` is the right shape for Phase
1 rather than cursor pagination.
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.manager import Manager, ManagerRun
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import MANAGERS_COLLECTION

_Document = dict[str, Any]


class MongoManagerRepository:
    """MongoDB-backed store for manager (vendor-connection) projections."""

    def __init__(self, mongo: MongoClientHolder) -> None:
        """
        Store the shared Mongo client holder.

        Args:
            mongo (MongoClientHolder): The connected client holder.
        """
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[MANAGERS_COLLECTION]

    async def upsert(self, manager: Manager) -> Manager:
        """
        Replace-or-insert a manager by `_id`.

        Args:
            manager (Manager): The manager to persist.

        Returns:
            Manager: The same manager, for chaining.
        """
        doc = manager.model_dump(by_alias=True, mode="json", exclude={"last_run"})
        doc.pop("_id")
        # `$set`, not replace: the config projection must not blank the
        # run record `record_run` keeps beside it.
        await self._collection.update_one({"_id": manager.id}, {"$set": doc}, upsert=True)
        return manager

    async def record_run(self, manager_id: str, run: ManagerRun) -> None:
        """
        Store what a collector run reported, without touching its configuration.

        Args:
            manager_id (str): The manager the run was for.
            run (ManagerRun): The run's outcome.
        """
        await self._collection.update_one(
            {"_id": manager_id}, {"$set": {"last_run": run.model_dump(mode="json")}}
        )

    async def get_by_id(self, manager_id: str) -> Manager | None:
        """
        Look up one manager by its id.

        Args:
            manager_id (str): The manager's id.

        Returns:
            Manager | None: The manager, or None if not found.
        """
        doc = await self._collection.find_one({"_id": manager_id})
        if doc is None:
            return None
        return Manager.model_validate(doc)

    async def list_all(self) -> list[Manager]:
        """
        List every manager.

        Returns:
            list[Manager]: All managers in the collection.
        """
        docs = await self._collection.find({}).to_list(length=None)
        return [Manager.model_validate(doc) for doc in docs]
