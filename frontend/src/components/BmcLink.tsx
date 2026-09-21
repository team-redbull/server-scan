function ExternalLinkIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      className="size-3.5 fill-none stroke-current"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <path
        d="M6 4H4a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1v-2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M10 3h3v3M13 3 7 9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Opens the BMC's own web console (iDRAC/iLO/UCSM/whatever the vendor
 * calls it) in a new tab. `host` is a bare host — https:// is the one
 * scheme every vendor's BMC web UI actually serves. No host read this run
 * renders nothing, not a dead button. `labeled` adds the word "BMC" for
 * places with room (the detail header); the table keeps it icon-only. */
export function BmcLink({
  host,
  serverName,
  labeled = false,
}: {
  host: string | null;
  serverName: string;
  labeled?: boolean;
}) {
  if (!host) return null;

  const label = `Open BMC console for ${serverName}`;
  return (
    <a
      href={`https://${host}`}
      target="_blank"
      rel="noopener noreferrer"
      title={label}
      aria-label={label}
      className={`inline-flex h-7 items-center justify-center gap-1.5 rounded-md border border-[var(--border-subtle)] text-[var(--text-muted)] transition-colors duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] ${labeled ? "px-2.5 text-xs font-medium" : "w-7"}`}
    >
      <ExternalLinkIcon />
      {labeled && "BMC"}
    </a>
  );
}
