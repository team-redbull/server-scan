import type { ReservationState } from "@/types/server";

/**
 * Which MCE is installing this server, when one is.
 *
 * Renders NOTHING when the lock is not held, which is nearly every row — the
 * column it sits in must cost no width in the normal case. See
 * docs/adr/0035-install-reservation-lock.md, decision 6, for why the cluster
 * name is the part worth showing.
 *
 * Deliberately NOT folded into `InstallationBadge`. That badge reports
 * `openshift.lifecycle_state`, which is a READING derived from what the
 * collectors observed, and a server mid-install is still genuinely `AVAILABLE`
 * to them — nothing has reported the node yet. Overwriting the badge would make
 * an intent look like an observation, which is the same mistake the backend
 * avoided by not adding an `INSTALLING` lifecycle state.
 */
export function ReservationBadge({
  reservation,
  full = false,
}: {
  reservation: ReservationState;
  full?: boolean;
}) {
  if (!reservation.held) return null;

  const target = reservation.mce_cluster ?? "an unnamed MCE";
  // Everything known about the claim, for the hover — the row shows the
  // cluster, and this is where "since when, by what, for which InfraEnv" lives
  // without spending width on it.
  const detail = [
    `Installing to ${target}`,
    reservation.infra_env ? `InfraEnv ${reservation.infra_env}` : null,
    reservation.holder ? `held by ${reservation.holder}` : null,
    reservation.expires_at
      ? `lock expires ${new Date(reservation.expires_at).toLocaleString()}`
      : "lock has no expiry",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <span
      title={detail}
      aria-label={detail}
      className="inline-flex items-center gap-1 rounded-full bg-[var(--tint-info)] px-2 py-0.5 text-xs font-medium text-[var(--text-on-info)]"
    >
      {/* A spinner would imply this page is watching the install; it is not —
          the row is as fresh as the last poll. A static arrow says "headed
          there" without claiming liveness. */}
      <span aria-hidden="true">→</span>
      {full ? `Installing to ${target}` : target}
    </span>
  );
}
