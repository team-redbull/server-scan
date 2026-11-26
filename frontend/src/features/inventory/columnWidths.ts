import type { ServerRow } from "@/types/server";

/** Measures one string's rendered width in px, in the cells' font. */
export type MeasureText = (text: string) => number;

/** Columns whose width follows the longest value in the fleet. */
export const FITTED_COLUMNS = ["mce_name", "cluster_name", "model"] as const;
export type FittedColumn = (typeof FITTED_COLUMNS)[number];

/** Fixed widths in px: these cells hold icons and badges, not free text. */
export const FIXED_WIDTH: Record<string, number> = {
  refresh: 44,
  bmc_host: 88,
  openshift_state: 112,
  health: 160,
  maintenance: 112,
};

/** Never narrower than the header needs, never wider than the cap: a longer value wraps, it does not squeeze Name. */
export const FITTED_MIN: Record<FittedColumn, number> = {
  mce_name: 96,
  cluster_name: 96,
  model: 96,
};
export const FITTED_MAX = 224;

/** Name keeps at least this much; a longer name wraps onto a second line rather than being cut. */
export const NAME_MIN = 220;

/** `px-2` on each side of a cell, plus a little room so the last glyph is not flush with the edge. */
const CELL_PADDING = 16 + 4;

/**
 * Width of each fitted column: its longest value across the whole fleet, measured in the cell font.
 * Measured over every row, never the page or the filtered set, so sorting, paging and filtering
 * cannot move a column (`.claude/rules/frontend.md`, fixed widths).
 */
export function fitColumnWidths(
  rows: readonly ServerRow[],
  measure: MeasureText,
): Record<FittedColumn, number> {
  const widths = { ...FITTED_MIN };
  for (const column of FITTED_COLUMNS) {
    let longest = 0;
    const seen = new Set<string>();
    for (const row of rows) {
      const value = row[column];
      if (!value || seen.has(value)) continue;
      seen.add(value);
      longest = Math.max(longest, measure(value));
    }
    const fitted = Math.ceil(longest + CELL_PADDING);
    widths[column] = Math.min(FITTED_MAX, Math.max(FITTED_MIN[column], fitted));
  }
  return widths;
}

/**
 * Shrink the widest fitted columns first (their values wrap) until the row fits `box` px with Name at `NAME_MIN`.
 *
 * @param fitted Each fitted column's full width, from `fitColumnWidths`.
 * @param shown The fitted columns on screen (MCE may be hidden).
 * @param fixed The summed width of the fixed columns on screen.
 * @param box The table box's width in px; 0 (not measured yet) means no shrinking.
 */
export function shrinkToFit(
  fitted: Record<FittedColumn, number>,
  shown: readonly FittedColumn[],
  fixed: number,
  box: number,
): Record<FittedColumn, number> {
  const budget = box - fixed - NAME_MIN;
  const total = (cap: number) =>
    shown.reduce((sum, c) => sum + Math.max(FITTED_MIN[c], Math.min(fitted[c], cap)), 0);
  if (box <= 0 || total(FITTED_MAX) <= budget) return fitted;
  let cap = FITTED_MAX;
  while (cap > 0 && total(cap) > budget) cap -= 1;
  const widths = { ...fitted };
  for (const c of shown) widths[c] = Math.max(FITTED_MIN[c], Math.min(fitted[c], cap));
  return widths;
}

/**
 * The table's minimum width: every column at its width plus Name's minimum.
 *
 * @param widths Each shown column's width in px (Name excluded).
 */
export function tableMinWidth(widths: readonly number[]): number {
  return widths.reduce((sum, width) => sum + width, NAME_MIN);
}

/** About the average `text-sm` glyph; used only where a canvas cannot measure (jsdom). */
const FALLBACK_CHAR_PX = 7.5;

/**
 * A `MeasureText` for `font` (a CSS `font` shorthand), on a canvas created once.
 *
 * @param font The cells' computed font, e.g. `getComputedStyle(td).font`.
 */
export function canvasMeasure(font: string): MeasureText {
  const context =
    typeof document === "undefined"
      ? null
      : document.createElement("canvas").getContext("2d");
  if (!context) {
    return (text) => text.length * FALLBACK_CHAR_PX;
  }
  context.font = font;
  return (text) => context.measureText(text).width;
}
