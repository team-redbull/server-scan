"""The collector connection seam.

A collector needs three things to reach a vendor manager: where it is, and
a username and password to present. All three are configuration, not
inventory data, so they come from one place per manager *type* — see
`app.infrastructure.credentials.env.EnvConnectionResolver`.

Resolution is by `ManagerType`, not by a per-manager reference. That is a
deliberate narrowing: this platform runs one endpoint per vendor type
(one UCS Manager, one OneView appliance, one OME), and keying the lookup
on the type means onboarding a vendor is "fill in three values", with no
document to create first and nothing that can disagree with itself. UCS
Manager's own multi-domain story is the `UCS_CENTRAL` parent, which
enumerates its domains at collection time — see `Manager`'s docstring —
not a list of endpoints someone maintains by hand.

`CredentialResolver` is a `Protocol` rather than a concrete class so a
deployment that wants a real secret store (Vault, External Secrets) can
supply its own implementation without touching collector code — the same
seam `ServerInventoryProvider` gives real-vs-fake collectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.enums import ManagerType


class ManagerNotConfiguredError(Exception):
    """
    No connection details are configured for a manager type.

    Not an `AppError`: raised in a collector process that never serves HTTP.
    """


@dataclass(frozen=True, slots=True)
class ManagerConnection:
    """
    Everything needed to reach one vendor manager.

    `endpoint` is a bare host or IP, never a URL — each vendor SDK builds its own.
    """

    endpoint: str
    username: str
    password: str

    def __repr__(self) -> str:
        """
        Redact the password, so a log line, traceback or debugger session never prints it.

        Returns:
            str: A representation with the password masked as `'***'`.
        """
        return (
            f"ManagerConnection(endpoint={self.endpoint!r}, "
            f"username={self.username!r}, password='***')"
        )


class CredentialResolver(Protocol):
    """Resolves one manager type's connection details from wherever a deployment keeps them."""

    def resolve(self, manager_type: ManagerType) -> ManagerConnection:
        """
        Resolve the connection details for one manager type.

        Args:
            manager_type (ManagerType): The vendor manager type to connect to.

        Returns:
            ManagerConnection: The endpoint and login for that manager type.

        Raises:
            ManagerNotConfiguredError: If any of the three values is missing.
                Never returns a partially-populated `ManagerConnection`,
                because a blank password reaches the vendor as a real login
                attempt and fails as "bad credentials" rather than as the
                configuration error it actually is.
        """
        ...
