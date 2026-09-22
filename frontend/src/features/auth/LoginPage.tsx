import { useState } from "react";
import type { CSSProperties, FormEvent } from "react";

import { ApiError } from "@/api/client";
import { useLoginMutation } from "@/features/auth/useAuth";

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.problem.status) {
      case 401:
        return "Wrong username or password.";
      case 403:
        return "Your account has no permission for this app.";
      case 503:
        return "The directory is unreachable right now — try again.";
      default:
        return error.problem.detail;
    }
  }
  return error instanceof Error ? error.message : "Login failed.";
}

/** A minimal line-art server rack — the ambient background motif, not a
 * data icon, so it's built inline rather than added as a real asset. */
function RackIcon({ className, style }: { className?: string; style?: CSSProperties }) {
  return (
    <svg viewBox="0 0 48 64" fill="none" aria-hidden="true" className={className} style={style}>
      <rect x="2" y="2" width="44" height="60" rx="4" stroke="currentColor" strokeWidth="2" />
      {[12, 26, 40].map((y) => (
        <g key={y}>
          <rect x="7" y={y} width="34" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
          <circle cx="13" cy={y + 5} r="1.6" fill="currentColor" />
          <line x1="20" y1={y + 5} x2="35" y2={y + 5} stroke="currentColor" strokeWidth="1.5" />
        </g>
      ))}
    </svg>
  );
}

/** One ambient rack, floating and rotating slowly (`@keyframes login-float`,
 * `index.css`) behind the card. Purely decorative: `aria-hidden`, and
 * `prefers-reduced-motion` already zeroes every animation duration globally. */
function FloatingRack({
  className,
  style,
}: {
  className: string;
  style: CSSProperties;
}) {
  return (
    <RackIcon
      className={`absolute animate-[login-float_var(--login-float-duration,9s)_ease-in-out_infinite] text-[var(--border-strong)] ${className}`}
      style={style}
    />
  );
}

/** The only route rendered when `login_required && !authenticated`
 * (`AppLayout`) — no nav, no outlet. */
export function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const mutation = useLoginMutation();

  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate({ username, password });
  }

  return (
    <div className="relative flex min-h-dvh items-center justify-center overflow-hidden bg-[var(--surface-page)] px-4">
      {/* Ambient glow: red and black, not the app's blue "info" accent. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute top-[-20%] left-1/2 h-[70vh] w-[70vh] -translate-x-1/2 rounded-full bg-[var(--color-status-critical)] opacity-[0.16] blur-[120px]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute bottom-[-25%] left-[10%] h-[50vh] w-[50vh] rounded-full bg-black opacity-40 blur-[120px]"
      />

      {/* Floating rack motif, scattered around the card, low opacity so the
          card stays the focal point. */}
      <FloatingRack
        className="top-[12%] left-[8%] size-16 opacity-[0.14] sm:size-20"
        style={{ "--login-float-duration": "10s" } as CSSProperties}
      />
      <FloatingRack
        className="top-[18%] right-[10%] size-12 opacity-[0.1] sm:size-14"
        style={{ "--login-float-duration": "13s", animationDelay: "1.2s" } as CSSProperties}
      />
      <FloatingRack
        className="bottom-[15%] left-[14%] size-10 opacity-[0.12] sm:size-12"
        style={{ "--login-float-duration": "11s", animationDelay: "0.6s" } as CSSProperties}
      />
      <FloatingRack
        className="right-[6%] bottom-[10%] size-20 opacity-[0.1] sm:size-24"
        style={{ "--login-float-duration": "14s", animationDelay: "2s" } as CSSProperties}
      />
      <FloatingRack
        className="top-[6%] right-[26%] size-8 opacity-[0.1] sm:size-10"
        style={{ "--login-float-duration": "9s", animationDelay: "0.3s" } as CSSProperties}
      />
      <FloatingRack
        className="top-[42%] left-[3%] size-14 opacity-[0.09] sm:size-16"
        style={{ "--login-float-duration": "12s", animationDelay: "1.6s" } as CSSProperties}
      />
      <FloatingRack
        className="top-[48%] right-[3%] size-9 opacity-[0.11] sm:size-11"
        style={{ "--login-float-duration": "15s", animationDelay: "0.9s" } as CSSProperties}
      />
      <FloatingRack
        className="bottom-[6%] left-[32%] size-11 opacity-[0.1] sm:size-14"
        style={{ "--login-float-duration": "10.5s", animationDelay: "2.4s" } as CSSProperties}
      />
      <FloatingRack
        className="bottom-[32%] right-[18%] size-8 opacity-[0.12] sm:size-9"
        style={{ "--login-float-duration": "13.5s", animationDelay: "1.8s" } as CSSProperties}
      />

      <form
        onSubmit={submit}
        className="relative w-full max-w-sm rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--surface-raised)]/80 p-8 shadow-2xl shadow-black/40 backdrop-blur-xl"
      >
        <div className="mb-8 flex flex-col items-center text-center">
          <img src="/redbull-logo.svg" alt="Red Bull" className="h-20 w-auto" />
          <h1 className="mt-5 text-4xl font-black tracking-tight text-[var(--text-primary)]">
            SERVER<span className="text-[var(--color-status-critical)]">SCAN</span>
          </h1>
          <p className="mt-2 text-sm text-[var(--text-secondary)]">Log in to continue</p>
        </div>

        <label htmlFor="login-username" className="block text-xs text-[var(--text-secondary)]">
          Username
        </label>
        <input
          id="login-username"
          type="text"
          autoComplete="username"
          value={username}
          onChange={(event) => {
            setUsername(event.target.value);
          }}
          className="mt-1.5 w-full rounded-full border border-[var(--border-subtle)] bg-[var(--surface-sunken)] px-4 py-2 text-sm text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-critical)]"
        />

        <label
          htmlFor="login-password"
          className="mt-4 block text-xs text-[var(--text-secondary)]"
        >
          Password
        </label>
        <input
          id="login-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => {
            setPassword(event.target.value);
          }}
          className="mt-1.5 w-full rounded-full border border-[var(--border-subtle)] bg-[var(--surface-sunken)] px-4 py-2 text-sm text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-critical)]"
        />

        {mutation.isError && (
          <p role="alert" className="mt-3 text-xs text-[var(--text-on-critical)]">
            {errorMessage(mutation.error)}
          </p>
        )}

        <button
          type="submit"
          disabled={mutation.isPending || !username || !password}
          className="mt-6 w-full rounded-full border border-[var(--border-strong)] bg-[var(--color-status-critical)] px-3 py-2.5 text-sm font-semibold text-white transition-colors duration-[var(--duration-fast)] ease-[var(--ease-out-strong)] hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:brightness-100"
        >
          {mutation.isPending ? "Logging in…" : "Log in"}
        </button>
      </form>
    </div>
  );
}
