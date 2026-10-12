import { useEffect, useRef, useState } from "react";

/**
 * A filter dropdown that takes one value, several, or none (= all): a button
 * showing the choice and a checklist under it. Replaces the single-value
 * `<select>`s so "Dell and HP, available" is one filter, not two views.
 */

export interface MultiSelectOption {
  value: string;
  label: string;
  /** Rows this option would add or keep under every other filter; omitted until the fleet loads. */
  count?: number | undefined;
}

interface MultiSelectProps {
  /** Ties the visible `<label htmlFor>` to the button. */
  id: string;
  label: string;
  options: MultiSelectOption[];
  selected: string[];
  /** One option ticked or unticked. The caller applies it to the live URL, never to a
   * render-time list, so two quick clicks cannot lose the first. */
  onToggle: (value: string) => void;
  /** Back to "all". */
  onClear: () => void;
  /** Button text while nothing is ticked, e.g. `All (2,504)`. */
  allLabel: string;
  className: string;
}

/** The button's text for the current choice. */
function summary(
  options: MultiSelectOption[],
  selected: string[],
  allLabel: string,
): string {
  if (selected.length === 0) return allLabel;
  const labels = selected.map(
    (value) => options.find((o) => o.value === value)?.label ?? value,
  );
  return labels.length <= 2 ? labels.join(", ") : `${labels.length} selected`;
}

export function MultiSelect({
  id,
  label,
  options,
  selected,
  onToggle,
  onClear,
  allLabel,
  className,
}: MultiSelectProps) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => {
      document.removeEventListener("mousedown", away);
    };
  }, [open]);

  return (
    <div
      ref={root}
      className="relative"
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          setOpen(false);
          button.current?.focus();
        }
      }}
    >
      <button
        ref={button}
        id={id}
        type="button"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => {
          setOpen((o) => !o);
        }}
        className={`${className} flex items-center justify-between gap-2 text-left`}
      >
        <span className="min-w-0 truncate">
          {summary(options, selected, allLabel)}
        </span>
        <span aria-hidden="true" className="shrink-0 text-[var(--text-muted)]">
          ▾
        </span>
      </button>
      {open && (
        <div
          role="group"
          aria-label={`${label} options`}
          className="absolute top-full left-0 z-20 mt-1 max-h-72 w-max max-w-[22rem] min-w-full overflow-y-auto rounded-md border border-[var(--border-strong)] bg-[var(--surface-raised)] p-1 shadow-lg"
        >
          {selected.length > 0 && (
            <button
              type="button"
              onClick={onClear}
              className="w-full rounded px-2 py-1 text-left text-xs text-[var(--color-status-info)] hover:bg-[var(--surface-hover)]"
            >
              Clear (show all)
            </button>
          )}
          {options.map((option) => (
            <label
              key={option.value}
              className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-xs font-normal text-[var(--text-primary)] hover:bg-[var(--surface-hover)]"
            >
              <input
                type="checkbox"
                checked={selected.includes(option.value)}
                onChange={() => {
                  onToggle(option.value);
                }}
              />
              <span className="min-w-0 flex-1">{option.label}</span>{" "}
              {option.count !== undefined && (
                <span className="tabular shrink-0 text-[var(--text-muted)]">
                  {option.count}
                </span>
              )}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
