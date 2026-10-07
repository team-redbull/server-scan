import { useSyncExternalStore } from "react";

export interface Toast {
  title: string;
  detail: string;
  ok: boolean;
}

interface ToastState {
  toast: Toast | null;
  open: boolean;
}

const VISIBLE_MS = 2000;

// `toast` is kept after `open` goes false so the exit transition still has content.
let state: ToastState = { toast: null, open: false };
let timer: ReturnType<typeof setTimeout> | undefined;
const listeners = new Set<() => void>();

function set(next: ToastState): void {
  state = next;
  listeners.forEach((listener) => listener());
}

/**
 * Show one toast, replacing any visible one in place and restarting its timer.
 *
 * Args:
 *     toast (Toast): The text to show and whether it reports a success.
 */
export function showToast(toast: Toast): void {
  clearTimeout(timer);
  set({ toast, open: true });
  timer = setTimeout(() => set({ ...state, open: false }), VISIBLE_MS);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Read the current toast state.
 *
 * Returns:
 *     ToastState: The last toast shown and whether it is still visible.
 */
export function useToastState(): ToastState {
  return useSyncExternalStore(subscribe, () => state);
}
