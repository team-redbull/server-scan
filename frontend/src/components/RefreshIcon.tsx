export function RefreshIcon({ spinning }: { spinning: boolean }) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={`size-3.5 fill-none stroke-current ${spinning ? "animate-spin" : ""}`}
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" strokeLinecap="round" />
      <path d="M12.5 1.5v3h-3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
