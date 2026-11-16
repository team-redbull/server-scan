"""Request schemas for the install-lock endpoints on `/api/v1/servers/{id}`.

Responses reuse `ServerDetail`, as the maintenance endpoints do: a caller taking
the lock wants the resulting server state, not the sub-document alone.

`mce_cluster` is REQUIRED on a reserve. The lock exists because two MCEs can
draw from one InfraEnv pool, so a reservation that does not say which cluster is
installing answers half the question — and the inventory row renders exactly
that field. Making it optional would let a caller take a lock nobody can
attribute.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import HealthSeverity, ManagerType, Vendor


class ReserveServerRequest(BaseModel):
    """The request body for taking the install lock on a server."""

    holder: str = Field(min_length=1, max_length=64)
    mce_cluster: str = Field(min_length=1, max_length=253)
    infra_env: str | None = Field(default=None, max_length=253)
    namespace: str | None = Field(default=None, max_length=253)
    workflow_id: str | None = Field(default=None, max_length=253)
    # Bounded at both ends: too short expires mid-install, too long stops the
    # expiry being a remedy. ADR-0035, decision 3.
    ttl_seconds: int = Field(default=7200, ge=300, le=86_400)


class ReleaseServerRequest(BaseModel):
    """The request body for releasing the install lock.

    Both fields optional so a release with neither is an operator override —
    ADR-0035, "Consequences".
    """

    holder: str | None = Field(default=None, max_length=64)
    workflow_id: str | None = Field(default=None, max_length=253)


class ReserveAvailableRequest(BaseModel):
    """The body of `POST /servers/available`: what to find, and who is taking it (ADR-0063).

    Exactly one of `name`/`pattern`. There is no `count`: one server per call, and an unknown
    field (a stale `count`, a misspelt `max_health`) is a 422 instead of being ignored.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=253)
    pattern: str | None = Field(default=None, min_length=1, max_length=1024)
    vendor: Vendor | None = None
    source_provider: ManagerType | None = None
    max_health: HealthSeverity = HealthSeverity.MAJOR
    min_nic_macs: int = Field(default=0, ge=0, le=16)
    holder: str = Field(min_length=1, max_length=64)
    mce_cluster: str = Field(min_length=1, max_length=253)
    infra_env: str | None = Field(default=None, max_length=253)
    namespace: str | None = Field(default=None, max_length=253)
    workflow_id: str | None = Field(default=None, max_length=253)
    ttl_seconds: int = Field(default=7200, ge=300, le=86_400)
