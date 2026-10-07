import type { ReactNode } from "react";

import { useCanReadAudit } from "@/features/auth/useAuth";

/** Renders `children` only for a caller who may read the audit trail. A
 * viewer who pastes a link to `/events` lands on this message, not on a list
 * that would only be a wall of 403s. */
export function AuditGate({ children }: { children: ReactNode }) {
  if (!useCanReadAudit()) {
    return (
      <main className="mx-auto max-w-3xl p-8">
        <h1 className="text-2xl font-semibold">Audit trail</h1>
        <p className="mt-3 text-[var(--text-secondary)]">
          The audit trail is available to admins and auditors only.
        </p>
      </main>
    );
  }
  return children;
}
