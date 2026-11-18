import { useLayoutEffect, useRef, useState } from "react";

const MIN_HEIGHT = 240;

/**
 * Caps a box so its page never scrolls: the window height less the box's top and everything
 * under it (the rest of its parent, `main`'s bottom padding, the footer). Measured from layout,
 * never from the scroll position. Traps: `.claude/rules/frontend.md`.
 *
 * @param dep (unknown): a value whose change re-measures (the row count).
 * @returns The ref for the scroll box and the `style` to put on it.
 */
export function useFillViewport<T extends HTMLElement>(dep: unknown) {
  const ref = useRef<T>(null);
  const [maxHeight, setMaxHeight] = useState<number | undefined>(undefined);
  useLayoutEffect(() => {
    const update = () => {
      const el = ref.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const column = el.parentElement;
      const main = el.closest("main");
      const below =
        (column ? column.getBoundingClientRect().bottom - rect.bottom : 0) +
        (main ? parseFloat(getComputedStyle(main).paddingBottom) : 0) +
        (document.querySelector("footer")?.getBoundingClientRect().height ?? 0);
      const top = rect.top + window.scrollY;
      setMaxHeight(Math.max(MIN_HEIGHT, Math.round(window.innerHeight - top - below)));
    };
    update();
    window.addEventListener("resize", update);
    const column = ref.current?.parentElement;
    const observer =
      typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(update);
    if (column) observer?.observe(column);
    return () => {
      window.removeEventListener("resize", update);
      observer?.disconnect();
    };
  }, [dep]);
  return { ref, style: maxHeight === undefined ? undefined : { maxHeight } };
}
