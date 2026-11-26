"""Vendor-neutral network model.

MAC addresses stored here are always already normalized
(`app.domain.value_objects.normalize_mac`) before a document is persisted —
callers building a `Server` construct these sub-models from normalized
values, they don't normalize inside the model itself, so a model
constructed directly in a test can also exercise un-normalized input
without a validator fighting it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.enums import LinkState


class BmcInfo(BaseModel):
    """A server's baseboard management controller address, parsed into parts."""

    address_raw: str | None = None
    scheme: str | None = None
    host: str | None = None
    host_is_ip: bool = False
    port: int | None = None
    path: str | None = None
    mac: str | None = None


class NetworkInterface(BaseModel):
    """One network interface reported for a server."""

    name: str
    mac: str | None = None
    speed_mbps: int | None = None
    link_state: LinkState = LinkState.UNKNOWN
    # True: this run could not read the link; `link_state` was kept from an earlier one (ADR-0022).
    link_state_kept: bool = False
    # `controller/port/partition` on Dell (`1/1/1`), the BMC's own raw
    # identifier elsewhere. See `app.domain.ports.provider.ProviderNic`.
    location: str | None = None


class NetworkInfo(BaseModel):
    """A server's BMC address and its network interfaces."""

    bmc: BmcInfo = Field(default_factory=BmcInfo)
    interfaces: list[NetworkInterface] = Field(default_factory=list)
