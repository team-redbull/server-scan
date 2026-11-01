"""The `api_tokens` collection: one document per user, keyed by username (ADR-0053)."""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.api_token import ApiToken
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import API_TOKENS_COLLECTION

_Document = dict[str, Any]


class MongoApiTokenRepository:
    """MongoDB-backed store for personal API tokens."""

    def __init__(self, mongo: MongoClientHolder) -> None:
        """
        Store the shared Mongo client holder.

        Args:
            mongo (MongoClientHolder): The connected client holder.
        """
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[API_TOKENS_COLLECTION]

    async def replace(self, token: ApiToken) -> None:
        """
        Store a user's token, replacing the one they had.

        Args:
            token (ApiToken): The new token.
        """
        await self._collection.replace_one(
            {"_id": token.username}, token.model_dump(mode="json", by_alias=True), upsert=True
        )

    async def get(self, username: str) -> ApiToken | None:
        """
        Read one user's token.

        Args:
            username (str): The owner.

        Returns:
            ApiToken | None: The token, or `None` if they have none.
        """
        doc = await self._collection.find_one({"_id": username})
        return ApiToken.model_validate(doc) if doc else None

    async def find_by_hash(self, token_hash: str) -> ApiToken | None:
        """
        Find the token a presented secret hashes to.

        Args:
            token_hash (str): SHA-256 of the presented token.

        Returns:
            ApiToken | None: The token, or `None` if none matches.
        """
        doc = await self._collection.find_one({"token_hash": token_hash})
        return ApiToken.model_validate(doc) if doc else None

    async def list_all(self) -> list[ApiToken]:
        """
        List every stored token, newest first.

        Returns:
            list[ApiToken]: One per user who has one.
        """
        docs = await self._collection.find({}).sort("created_at", -1).to_list(length=None)
        return [ApiToken.model_validate(doc) for doc in docs]

    async def delete(self, username: str) -> bool:
        """
        Remove a user's token.

        Args:
            username (str): The owner.

        Returns:
            bool: Whether there was one to remove.
        """
        return (await self._collection.delete_one({"_id": username})).deleted_count > 0

    async def touch(self, username: str, when_iso: str) -> None:
        """
        Record that a request used the token.

        Args:
            username (str): The owner.
            when_iso (str): The time, as the ISO string every datetime is stored as.
        """
        await self._collection.update_one({"_id": username}, {"$set": {"last_used_at": when_iso}})
