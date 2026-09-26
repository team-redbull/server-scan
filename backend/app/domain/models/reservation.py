"""Reservation state, embedded on `Server` — the install lock.

WHY THIS EXISTS. `/servers/available` hands out candidates without reserving
them (ADR-0032 decision 3 accepted that explicitly), and nothing changes a
server's lifecycle state until a CLUSTER reports the node minutes later. So
between a caller drawing a server and that node appearing there is a window in
which the same machine is drawn again — by a second run, or by a different MCE
entirely. Each creates a BareMetalHost for it, and both succeed.

Nothing but this service can close that window. The installing workflows run on
different clusters and cannot see each other's BareMetalHosts, so server-scan is
the only thing both sides share.

WHY NOT A LIFECYCLE STATE. `openshift.lifecycle_state` is a READING: it is
derived from what the collectors observe about cluster membership, and it
defaults to AVAILABLE. An INSTALLING value written here would be reset by the
next collection, because the server genuinely is not in a cluster yet — an
install takes far less time than the 6-hour cron. Mixing an intent into an
observation also cuts against ADR-0027's "unknown is not a reading". So this
follows `Maintenance` instead: an operator-set sub-document, orthogonal to both
classification and health, that ingest carries forward verbatim.

WHY IT EXPIRES. A reservation with no expiry leaks a server out of the fleet on
every crashed run, permanently and invisibly. `expires_at` means the worst case
is a machine unavailable for the rest of the window rather than for good, and it
is what makes the lock safe to take before the work rather than after.

WHO HOLDS IT, AND FOR WHICH MCE. `mce_cluster` is not bookkeeping: two MCEs
drawing from one InfraEnv pool is the case this exists for, so the answer to
"who has this server" is only useful if it says which cluster is installing it.
Together with `infra_env` and `workflow_id` it makes a held server traceable
from the fleet list back to the exact run holding it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.utils.timeutil import utcnow


class Reservation(BaseModel):
    """The install-lock state embedded on a `Server` document.

    Attributes:
        holder (str | None): What took the lock, e.g. "install-server".
        mce_cluster (str | None): The MCE cluster this server is being
            installed into. The field that makes the lock legible.
        infra_env (str | None): The InfraEnv being filled.
        namespace (str | None): Where the BareMetalHost is being created.
        workflow_id (str | None): The run holding it, for tracing back.
        created_at (datetime | None): When it was taken.
        expires_at (datetime | None): When it stops being honoured. None means
            a reservation that never expires, which `reserve` never writes.
    """

    holder: str | None = None
    mce_cluster: str | None = None
    infra_env: str | None = None
    namespace: str | None = None
    workflow_id: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None

    def is_live(self, *, now: datetime | None = None) -> bool:
        """Whether this reservation still excludes the server from a draw.

        An expired one is left on the document rather than cleared — see
        docs/adr/0035-install-reservation-lock.md, decision 3.

        Args:
            now (datetime | None): The comparison instant; defaults to now.

        Returns:
            bool: True while the lock is held and unexpired.
        """
        if self.holder is None:
            return False
        if self.expires_at is None:
            # A hand-edited document: `reserve` never writes this. Honoured
            # rather than second-guessed — ADR-0035, decision 7.
            return True
        return (now or utcnow()) < self.expires_at
