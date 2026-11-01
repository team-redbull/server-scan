import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { createMyToken, getMyToken, revokeMyToken, tokenLineName } from "@/api/personalToken";
import type { CreatedToken } from "@/api/personalToken";
import { queryKeys } from "@/api/queryKeys";
import { CopyButton } from "@/components/CopyButton";
import { useAuth } from "@/features/auth/useAuth";
import { formatRelative, formatTimestamp } from "@/lib/datetime";

const BUTTON =
  "rounded-full px-4 py-2 text-sm font-semibold transition-transform duration-100 ease-out active:scale-[0.97] motion-reduce:transition-none motion-reduce:active:scale-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)]";

/** The signed-in user's one personal API token (ADR-0053). Nothing renders when login is off:
 * with no login every caller is already admin and the API ignores the header. */
export function TokenSection({
  fresh,
  onFresh,
}: {
  fresh: CreatedToken | null;
  onFresh: (created: CreatedToken | null) => void;
}) {
  const { data: me } = useAuth();
  const queryClient = useQueryClient();
  const enabled = Boolean(me?.login_required && me.authenticated);

  const { data: token } = useQuery({
    queryKey: queryKeys.personalToken.all,
    queryFn: getMyToken,
    enabled,
  });
  const done = () => queryClient.invalidateQueries({ queryKey: queryKeys.personalToken.all });
  const create = useMutation({
    mutationFn: createMyToken,
    onSuccess: (created) => {
      onFresh(created);
      void done();
    },
  });
  const revoke = useMutation({
    mutationFn: revokeMyToken,
    onSuccess: () => {
      onFresh(null);
      void done();
    },
  });

  if (!enabled || token === undefined) return null;

  const line = tokenLineName(token?.role ?? (me?.role === "VIEWER" ? "VIEWER" : "AUDITOR"));

  return (
    <section
      aria-labelledby="skill-token"
      className="mt-6 rounded-2xl border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-7"
    >
      <h2
        id="skill-token"
        className="text-2xl leading-tight font-semibold tracking-[-0.02em] text-[var(--text-primary)]"
      >
        Your token
      </h2>
      <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--text-secondary)]">
        One personal token for <span className="font-mono">{me?.username}</span>. Everything done with
        it is recorded under your name. It is read-only, lasts 90 days, and a new one replaces the old.
      </p>

      {fresh && (
        <div className="mt-5 rounded-xl border border-[var(--color-status-warning)] bg-[var(--surface-sunken)] p-4">
          <p className="text-sm font-medium text-[var(--text-primary)]">
            Copy it now — it is shown only once.
          </p>
          <div className="mt-2 flex items-center gap-2">
            <code className="min-w-0 flex-1 overflow-x-auto font-mono text-[0.8rem] whitespace-nowrap text-[var(--text-primary)]">
              {line}={fresh.token}
            </code>
            <CopyButton text={`${line}=${fresh.token}`} label="Copy the token line" />
          </div>
          <p className="mt-2 text-xs text-[var(--text-secondary)]">
            Paste it over the <span className="font-mono">{line}</span> line in SKILL.md.
          </p>
        </div>
      )}

      {token ? (
        <dl className="mt-5 grid max-w-xl grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 text-sm">
          <dt className="text-[var(--text-muted)]">Token</dt>
          <dd className="font-mono text-[var(--text-primary)]">{token.prefix}…</dd>
          <dt className="text-[var(--text-muted)]">Access</dt>
          <dd className="text-[var(--text-primary)]">{token.role.toLowerCase()}</dd>
          <dt className="text-[var(--text-muted)]">Expires</dt>
          <dd className="text-[var(--text-primary)]" title={formatTimestamp(token.expires_at)}>
            {formatRelative(token.expires_at)}
          </dd>
          <dt className="text-[var(--text-muted)]">Last used</dt>
          <dd className="text-[var(--text-primary)]">
            {token.last_used_at ? formatRelative(token.last_used_at) : "never"}
          </dd>
        </dl>
      ) : (
        <p className="mt-5 text-sm text-[var(--text-secondary)]">You have no token yet.</p>
      )}

      <div className="mt-5 flex flex-wrap gap-3">
        <button
          type="button"
          disabled={create.isPending}
          onClick={() => {
            create.mutate();
          }}
          className={`${BUTTON} bg-[var(--color-status-info)] text-white hover:brightness-110`}
        >
          {token ? "Replace my token" : "Create my token"}
        </button>
        {token && (
          <button
            type="button"
            disabled={revoke.isPending}
            onClick={() => {
              revoke.mutate();
            }}
            className={`${BUTTON} border border-[var(--border-strong)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]`}
          >
            Revoke
          </button>
        )}
      </div>
      {(create.isError || revoke.isError) && (
        <p role="alert" className="mt-3 text-sm text-[var(--text-secondary)]">
          That did not work. Try again.
        </p>
      )}
    </section>
  );
}
