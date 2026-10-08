import { useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { actorLabel } from "@/lib/actor";
import type { EventActor } from "@/types/events";

interface Choice {
  id: string;
  label: string;
  count: number | null;
}

const ALL: Choice = { id: "", label: "All", count: null };

/** The Events page's User filter: a searchable combobox. Typing narrows the
 * list by the readable label or the raw id; Enter or a click picks one, and
 * "All" clears the filter. `value` is the actor id (kept in the URL), shown as
 * its label while the field is not being edited. */
export function ActorCombobox({
  id,
  actors,
  value,
  onChange,
  className,
}: {
  id: string;
  actors: EventActor[];
  value: string;
  onChange: (actorId: string) => void;
  className: string;
}) {
  const listId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState<string | null>(null);
  const [active, setActive] = useState(0);

  const choices = useMemo<Choice[]>(
    () =>
      actors.map((a) => ({
        id: a.id,
        label: actorLabel(a).label,
        count: a.event_count,
      })),
    [actors],
  );
  const needle = (query ?? "").trim().toLowerCase();
  const visible = useMemo<Choice[]>(
    () =>
      needle
        ? choices.filter(
            (c) =>
              c.label.toLowerCase().includes(needle) ||
              c.id.toLowerCase().includes(needle),
          )
        : [ALL, ...choices],
    [choices, needle],
  );
  const selectedLabel = choices.find((c) => c.id === value)?.label ?? value;
  const text = query ?? selectedLabel;

  function pick(choice: Choice) {
    onChange(choice.id);
    setQuery(null);
    setOpen(false);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((i) => Math.max(0, Math.min(visible.length - 1, i + step)));
    } else if (event.key === "Enter" && open) {
      event.preventDefault();
      const choice = visible[active];
      if (choice) pick(choice);
    } else if (event.key === "Escape") {
      setOpen(false);
      setQuery(null);
    }
  }

  return (
    <div
      ref={rootRef}
      className="relative"
      onBlur={(event) => {
        if (rootRef.current?.contains(event.relatedTarget)) return;
        setOpen(false);
        if (query === "") onChange("");
        setQuery(null);
      }}
    >
      <input
        id={id}
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open ? `${listId}-${String(active)}` : undefined}
        autoComplete="off"
        value={text}
        placeholder="All"
        onFocus={(event) => {
          setOpen(true);
          event.currentTarget.select();
        }}
        onChange={(event) => {
          setQuery(event.target.value);
          setActive(0);
          setOpen(true);
        }}
        onKeyDown={onKeyDown}
        className={className}
      />
      {open && (
        <ul
          id={listId}
          role="listbox"
          aria-label="Users"
          className="absolute z-20 mt-1 max-h-60 w-full overflow-auto rounded-md border border-[var(--border-strong)] bg-[var(--surface-raised)] py-1 text-sm shadow-lg"
        >
          {visible.length === 0 && (
            <li className="px-2.5 py-1.5 text-[var(--text-muted)]">
              No matching user
            </li>
          )}
          {visible.map((choice, index) => (
            <li
              key={choice.id || "all"}
              id={`${listId}-${String(index)}`}
              role="option"
              aria-selected={choice.id === value}
              onMouseDown={(event) => {
                event.preventDefault();
              }}
              onClick={() => {
                pick(choice);
              }}
              onMouseEnter={() => {
                setActive(index);
              }}
              className={`flex cursor-pointer items-center justify-between gap-3 px-2.5 py-1.5 ${
                index === active
                  ? "bg-[var(--surface-hover)] text-[var(--text-primary)]"
                  : "text-[var(--text-secondary)]"
              } ${choice.id === value ? "font-medium" : ""}`}
            >
              <span className="truncate">{choice.label}</span>
              {choice.count !== null && (
                <span className="shrink-0 text-xs text-[var(--text-muted)]">
                  {choice.count}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
