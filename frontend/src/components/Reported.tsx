import type { ReactNode } from "react";

/** Unread this run, with nothing carried forward: the value is the model's
 * zero, not a reading. */
export const NOT_READ_TITLE = "The most recent collection could not read this.";

/** Unread this run, carried forward from an earlier one. */
export const STALE_TITLE = "Not confirmed by the most recent collection.";

/** A visible "unconfirmed" marker — real text, not a `title` tooltip or
 * opacity alone, so keyboard and screen-reader users get the same fact. */
export function UnconfirmedMarker() {
  return (
    <span className="ml-1.5 align-middle text-[10px] font-medium tracking-wide text-amber-600 uppercase dark:text-amber-400">
      unconfirmed
    </span>
  );
}

/** "Not reported" in place of an unread zero, or the carried-forward value
 * dimmed and marked "unconfirmed"; a field that was read renders untouched. */
export function Reported({
  unread,
  empty,
  inline = false,
  children,
}: {
  unread: boolean;
  /** Whether the stored value is the model's zero (`0`, `[]`). */
  empty: boolean;
  inline?: boolean;
  children: ReactNode;
}) {
  if (!unread) {
    return <>{children}</>;
  }
  if (empty) {
    return inline ? (
      <span className="text-gray-500" title={NOT_READ_TITLE}>
        Not reported
      </span>
    ) : (
      <p className="mt-2 text-sm text-gray-500" title={NOT_READ_TITLE}>
        Not reported
      </p>
    );
  }
  const Tag = inline ? "span" : "div";
  return (
    <Tag className="opacity-70" title={STALE_TITLE}>
      {children}
      <UnconfirmedMarker />
    </Tag>
  );
}
