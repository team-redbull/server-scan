import { Fragment, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import type { HealthReason } from "@/features/events/describeEvent";
import {
  describeEvent,
  healthSections,
  topReason,
  transition,
} from "@/features/events/describeEvent";
import { actorLabel } from "@/lib/actor";
import type { ActorKind } from "@/lib/actor";
import { formatRelative, formatTimestamp } from "@/lib/datetime";
import type { AuditEventResponse } from "@/types/events";

const TYPE_TINT: [prefix: string, tint: string][] = [
  ["HEALTH_", "--tint-warning"],
  ["MAINTENANCE_", "--tint-maintenance"],
  ["SERVER_PRUNED", "--tint-critical"],
  ["SERVER_RESERVATION_REFUSED", "--tint-critical"],
  ["SERVER_RES", "--tint-info"],
  ["SERVER_RELEASED", "--tint-info"],
  ["OPENSHIFT_", "--tint-info"],
  ["CLASSIFICATION_", "--tint-major"],
  ["SERVER_CREATED", "--tint-healthy"],
];

function tintFor(eventType: string): string {
  return (
    TYPE_TINT.find(([prefix]) => eventType.startsWith(prefix))?.[1] ??
    "--surface-hover"
  );
}

const SEVERITY_TINT: Record<string, string> = {
  CRITICAL: "--tint-critical",
  MAJOR: "--tint-major",
  WARNING: "--tint-warning",
  HEALTHY: "--tint-healthy",
};

function SeverityChip({ severity }: { severity: string }) {
  return (
    <span
      className="rounded-full px-1.5 py-0.5 text-[0.7rem] font-medium text-[var(--text-primary)]"
      style={{
        backgroundColor: `var(${SEVERITY_TINT[severity] ?? "--surface-hover"})`,
      }}
    >
      {severity}
    </span>
  );
}

/** `FROM → TO` as severity chips; the reasons live in the expanded Details. */
function HealthTransition({ data }: { data: Record<string, unknown> }) {
  const label = transition(data);
  const [from, to] = label.split(" → ");
  return (
    <div className="mt-1">
      <span
        role="group"
        aria-label={label}
        className="inline-flex items-center gap-1.5"
      >
        <SeverityChip severity={from ?? "?"} />
        <span aria-hidden="true">→</span>
        <SeverityChip severity={to ?? "?"} />
      </span>
    </div>
  );
}

const FIELD_LABELS = [
  "Policy key",
  "Policy name",
  "Category",
  "Severity",
  "Message",
];

function Value({ value }: { value: string | null }) {
  return <>{value ?? "—"}</>;
}

/** One reason with every field labelled; a missing value reads as an em dash. */
function ReasonBlock({ reason }: { reason: HealthReason }) {
  const values = [
    reason.policy_key,
    reason.policy_name,
    reason.category,
    reason.severity,
    reason.message,
  ];
  return (
    <dl className="grid grid-cols-[6rem_1fr] gap-x-3 gap-y-0.5 rounded border border-[var(--border)] px-2 py-1">
      {FIELD_LABELS.map((label, i) => (
        <Fragment key={label}>
          <dt className="text-[var(--text-secondary)]">{label}</dt>
          <dd className="break-words">
            {label === "Severity" && values[i] ? (
              <SeverityChip severity={values[i]} />
            ) : (
              <Value value={values[i] ?? null} />
            )}
          </dd>
        </Fragment>
      ))}
    </dl>
  );
}

function HealthSide({
  label,
  severity,
  reasons,
}: {
  label: "FROM" | "TO";
  severity: string;
  reasons: HealthReason[];
}) {
  // Only the worst reason per side (first on a tie); the rest stay in the JSON.
  const top = topReason(reasons);
  return (
    <section aria-label={label} className="space-y-1">
      <div className="flex items-center gap-1.5 font-medium text-[var(--text-secondary)]">
        <span>{label}</span>
        <SeverityChip severity={severity} />
      </div>
      {top === null ? (
        <div className="text-[var(--text-secondary)]">nothing failing</div>
      ) : (
        <ReasonBlock reason={top} />
      )}
    </section>
  );
}

/** `FROM → TO` for the other status-change events (classification, OpenShift
 * state); nothing for an event that has no transition. The sentence is in Details. */
function PlainTransition({ data }: { data: Record<string, unknown> }) {
  if (typeof data.from !== "string" || typeof data.to !== "string") return null;
  return (
    <div className="mt-1 text-xs text-[var(--text-secondary)]">
      {transition(data)}
    </div>
  );
}

/** The FROM status with the reasons that caused it, then the TO status with its reasons. */
function HealthWhy({ data }: { data: Record<string, unknown> }) {
  const { from, to } = healthSections(data);
  if (from === null && to === null) return null;
  const [fromSeverity, toSeverity] = transition(data).split(" → ");
  return (
    <div className="mb-2 space-y-3 text-xs text-[var(--text-primary)]">
      {from !== null && (
        <HealthSide
          label="FROM"
          severity={fromSeverity ?? "?"}
          reasons={from}
        />
      )}
      {to !== null && (
        <HealthSide label="TO" severity={toSeverity ?? "?"} reasons={to} />
      )}
    </div>
  );
}

const KIND_TINT: Record<ActorKind, string> = {
  user: "--tint-info",
  token: "--tint-major",
  system: "--surface-hover",
};

/** An actor's label with its user / token / system chip. */
export function ActorCell({ actor }: { actor: AuditEventResponse["actor"] }) {
  const { label, kind } = actorLabel(actor);
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span>{label}</span>
      <span
        className="rounded-full px-1.5 py-0.5 text-[0.7rem] font-medium text-[var(--text-primary)]"
        style={{ backgroundColor: `var(${KIND_TINT[kind]})` }}
      >
        {kind}
      </span>
    </span>
  );
}

interface EventTimelineProps {
  events: AuditEventResponse[];
  /** Hide the Server column — the per-server tab already is that server. */
  showServer?: boolean;
  footer?: ReactNode;
}

/** A newest-first table of audit events: When, Server, What happened, User. */
export function EventTimeline({
  events,
  showServer = false,
  footer,
}: EventTimelineProps) {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const columns = showServer ? 4 : 3;

  function toggle(id: string) {
    setOpen((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  return (
    <div>
      <div className="overflow-x-auto rounded-md border border-[var(--border-subtle)]">
        {/* Fixed column widths, and the details in their own full-width row:
            opening one must never resize or move another column. */}
        <table className="w-full table-fixed text-left text-sm">
          <colgroup>
            {showServer && <col className="w-64" />}
            <col />
            <col className="w-48" />
            <col className="w-52" />
          </colgroup>
          <thead className="bg-[var(--surface-raised)] text-xs text-[var(--text-secondary)]">
            <tr>
              {showServer && <th className="px-3 py-2 font-medium">Server</th>}
              <th className="px-3 py-2 font-medium">What happened</th>
              <th className="px-3 py-2 font-medium">When</th>
              <th className="px-3 py-2 font-medium">User</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <Fragment key={event.id}>
                <tr className="border-t border-[var(--border-subtle)] align-top">
                  {showServer && (
                    <td className="px-3 py-2">
                      {event.server_id ? (
                        event.server_name ? (
                          <Link
                            to={`/servers/${encodeURIComponent(event.server_id)}`}
                            className="text-[var(--color-status-info)] hover:underline"
                          >
                            {event.server_name}
                          </Link>
                        ) : (
                          <span
                            title={event.server_id}
                            className="text-[var(--text-muted)]"
                          >
                            Unknown server
                          </span>
                        )
                      ) : (
                        <span className="text-[var(--text-muted)]">-</span>
                      )}
                    </td>
                  )}
                  <td className="px-3 py-2">
                    <span
                      className="mr-2 rounded-full px-2 py-0.5 text-xs font-medium text-[var(--text-primary)]"
                      style={{
                        backgroundColor: `var(${tintFor(event.event_type)})`,
                      }}
                    >
                      {event.event_type}
                    </span>
                    {event.event_type === "HEALTH_CHANGED" ? (
                      <HealthTransition data={event.data} />
                    ) : (
                      <PlainTransition data={event.data} />
                    )}
                    <button
                      type="button"
                      aria-expanded={open.has(event.id)}
                      onClick={() => {
                        toggle(event.id);
                      }}
                      className="mt-1 block cursor-pointer text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
                    >
                      {open.has(event.id) ? "▾ Hide details" : "▸ Details"}
                    </button>
                  </td>
                  <td className="px-3 py-2 text-xs whitespace-nowrap text-[var(--text-muted)]">
                    <time
                      dateTime={event.created_at}
                      title={formatRelative(event.created_at)}
                    >
                      {formatTimestamp(event.created_at)}
                    </time>
                  </td>
                  <td className="px-3 py-2 text-xs text-[var(--text-secondary)]">
                    <ActorCell actor={event.actor} />
                  </td>
                </tr>
                {open.has(event.id) && (
                  <tr>
                    <td
                      colSpan={columns}
                      className="bg-[var(--surface-sunken)] px-3 py-2"
                    >
                      {event.event_type === "HEALTH_CHANGED" ? (
                        <HealthWhy data={event.data} />
                      ) : (
                        <p className="mb-2 text-xs text-[var(--text-primary)]">
                          {describeEvent(event)}
                        </p>
                      )}
                      <pre className="text-xs break-words whitespace-pre-wrap text-[var(--text-secondary)]">
                        {JSON.stringify(event.data, null, 2)}
                      </pre>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {footer}
    </div>
  );
}
