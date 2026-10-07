import { useToastState } from "@/lib/toast";

/**
 * The one toast host, mounted once in `AppLayout`. A floating translucent
 * pill at the bottom right: it enters from below with a strong ease-out and
 * leaves faster than it came, and a second toast swaps its text in place
 * instead of restarting the animation. It is `aria-hidden`: `CopyButton`
 * already announces the result in its own live region.
 */
export function Toaster() {
  const { toast, open } = useToastState();

  return (
    <div
      aria-hidden="true"
      className="pointer-events-none fixed inset-x-0 bottom-6 z-50 flex justify-end px-6"
    >
      <div
        data-open={open}
        className="flex max-w-full items-center gap-2 rounded-full border border-[var(--border-strong)] bg-[color-mix(in_oklch,var(--surface-raised)_72%,transparent)] py-2 pr-4 pl-3 text-sm shadow-[0_8px_30px_rgb(0_0_0/0.45)] backdrop-blur-xl backdrop-saturate-150 transition-[opacity,transform,filter] duration-150 ease-[var(--ease-out-strong)] data-[open=true]:translate-y-0 data-[open=true]:scale-100 data-[open=true]:opacity-100 data-[open=true]:blur-none data-[open=true]:duration-200 data-[open=false]:translate-y-2 data-[open=false]:scale-95 data-[open=false]:opacity-0 data-[open=false]:blur-[2px]"
      >
        {toast && (
          <>
            <span
              aria-hidden="true"
              className={`grid size-5 shrink-0 place-items-center rounded-full ${toast.ok ? "bg-[var(--tint-healthy)] text-[var(--text-on-healthy)]" : "bg-[var(--tint-critical)] text-[var(--text-on-critical)]"}`}
            >
              <svg viewBox="0 0 16 16" className="size-3 fill-none stroke-current" strokeWidth="2">
                <path
                  d={toast.ok ? "m3.5 8.5 3 3 6-7" : "m4.5 4.5 7 7m0-7-7 7"}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </span>
            <span className="truncate text-[var(--text-secondary)]">
              {toast.title}{" "}
              <span className="font-medium text-[var(--text-primary)]">{toast.detail}</span>
            </span>
          </>
        )}
      </div>
    </div>
  );
}
