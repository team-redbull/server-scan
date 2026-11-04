import type { ReactNode } from "react";

import { useIsAdmin } from "@/features/auth/useAuth";

/** Renders `children` only for an admin. A non-admin who pastes a link to `/jobs` lands on
 * this message, not on a page of 403s. The API enforces the rule; this is the courtesy. */
export function AdminGate({ children }: { children: ReactNode }) {
  if (!useIsAdmin()) {
    return (
      <main className="mx-auto max-w-3xl p-8">
        <h1 className="text-2xl font-semibold">Jobs</h1>
        <p className="mt-3 text-[var(--text-secondary)]">
          Running jobs is available to admins only.
        </p>
      </main>
    );
  }
  return children;
}
