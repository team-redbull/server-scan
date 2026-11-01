import { CopyButton } from "@/components/CopyButton";

/** The one command that connects Claude Code to this server's MCP endpoint (ADR-0054). The token
 * is filled in only while a freshly created one is on screen; otherwise the user pastes theirs. */
export function McpSection({ origin, token }: { origin: string; token: string | null }) {
  const command = `claude mcp add --transport http server-scan ${origin}/mcp --header "Authorization: Bearer ${token ?? "<your token>"}"`;

  return (
    <section aria-labelledby="skill-mcp" className="mt-14">
      <h2
        id="skill-mcp"
        className="text-2xl leading-tight font-semibold tracking-[-0.02em] text-[var(--text-primary)]"
      >
        Or connect it as an MCP server
      </h2>
      <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--text-secondary)]">
        One command, nothing to unzip and no file to edit. Claude gets read-only tools to list and
        find servers, summarise the fleet, read a server&rsquo;s health and, with the auditor role,
        its events. It uses your token, so it sees only what you may.
      </p>
      <div className="mt-4 flex items-start gap-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--surface-sunken)] py-2 pr-2 pl-4">
        <code className="min-w-0 flex-1 overflow-x-auto py-1 font-mono text-[0.8rem] whitespace-nowrap text-[var(--text-primary)]">
          {command}
        </code>
        <CopyButton text={command} label="Copy the MCP command" />
      </div>
      {token === null && (
        <p className="mt-2 text-xs text-[var(--text-muted)]">
          Create your token above and this command fills it in.
        </p>
      )}
    </section>
  );
}
