"""Minimal MongoDB repository for the `membership_runs` collection.

Same rationale as `manager_repository.py`: one small document per
membership CronJob (a cluster's `nodes` job, or an MCE's `agents` job), so
`FleetGaugeRefresher` has something to read even when every observed
hostname was unmatched — see ADR-0029's 2026-09-24 update.
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.openshift import MembershipRun
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import MEMBERSHIP_RUNS_COLLECTION

_Document = dict[str, Any]


class MongoMembershipRunRepository:
    """MongoDB-backed store for the most recent run of each membership job."""

    def __init__(self, mongo: MongoClientHolder) -> None:
        """
        Store the shared Mongo client holder.

        Args:
            mongo (MongoClientHolder): The connected client holder.
        """
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[MEMBERSHIP_RUNS_COLLECTION]

    async def record_run(self, run: MembershipRun) -> None:
        """
        Replace the stored run for this job with the one it just finished.

        Keyed on `kind:reported_by` so a `nodes` cluster and an `agents`
        MCE can never collide even if they share a name.

        Args:
            run (MembershipRun): The run to persist.
        """
        doc_id = f"{run.kind}:{run.reported_by}"
        await self._collection.update_one(
            {"_id": doc_id},
            {"$set": run.model_dump(mode="json")},
            upsert=True,
        )

    async def list_all(self) -> list[MembershipRun]:
        """
        List the most recent run of every membership job.

        Returns:
            list[MembershipRun]: One entry per `kind`/`reported_by` pair.
        """
        docs = await self._collection.find({}).to_list(length=None)
        return [MembershipRun.model_validate(doc) for doc in docs]
