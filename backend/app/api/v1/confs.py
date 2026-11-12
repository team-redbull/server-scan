"""`GET /api/v1/confs` — the server configurations (specs) in the `confs` collection.

Read-only: another system writes the collection (docs/adr/0061-confs.md). It
holds tens to hundreds of documents, so the list is unpaginated and uncached.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import ValidationError

from app.api.v1.confs_schemas import ConfListResponse
from app.dependencies import get_mongo_holder
from app.domain.models.conf import Conf
from app.errors import AppError, NotFoundError
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.conf_repository import MongoConfRepository

router = APIRouter(prefix="/api/v1", tags=["confs"])


async def _conf_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
) -> MongoConfRepository:
    """
    Build the conf repository for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.

    Returns:
        MongoConfRepository: A repository bound to that client.
    """
    return MongoConfRepository(mongo)


@router.get("/confs", response_model=ConfListResponse)
async def list_confs(
    repo: Annotated[MongoConfRepository, Depends(_conf_repo)],
) -> ConfListResponse:
    """
    List every conf, in natural `conf_id` order.

    Args:
        repo (MongoConfRepository): The conf repository.

    Returns:
        ConfListResponse: The readable confs, and the ids of any skipped.
    """
    items, unreadable = await repo.list_all()
    return ConfListResponse(items=items, unreadable=unreadable)


@router.get("/confs/{conf_id}", response_model=Conf)
async def get_conf(
    conf_id: str,
    repo: Annotated[MongoConfRepository, Depends(_conf_repo)],
) -> Conf:
    """
    Get one conf by its `conf_id`.

    Args:
        conf_id (str): The conf's id, e.g. `CONF200.2`.
        repo (MongoConfRepository): The conf repository.

    Returns:
        Conf: The conf.

    Raises:
        NotFoundError: No conf has that id.
        AppError: The stored document does not match the schema (500).
    """
    try:
        conf = await repo.get(conf_id)
    except ValidationError as exc:
        raise AppError(
            f"The stored conf {conf_id!r} does not match the conf schema.",
            details={"conf_id": conf_id, "errors": exc.error_count()},
        ) from exc
    if conf is None:
        raise NotFoundError(f"No conf with id {conf_id!r}.", details={"conf_id": conf_id})
    return conf
