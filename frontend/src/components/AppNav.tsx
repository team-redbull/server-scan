import { Link, useLocation } from "react-router";

import { useAuth, useLogoutMutation } from "@/features/auth/useAuth";

const LINKS = [
  { to: "/", label: "Sites" },
  { to: "/servers", label: "Servers" },
  { to: "/rules", label: "Rules & Policies" },
  { to: "/architecture", label: "Architecture" },
];

/** The signed-in username, role badge and "Log out" — only when
 * `login_required` (this repo's own default has it off, so this renders
 * nothing there). Logging out invalidates `["auth","me"]`; `AppLayout`
 * picks that up and falls back to `LoginPage` on its own. */
function AccountMenu() {
  const { data: me } = useAuth();
  const logout = useLogoutMutation();

  if (!me?.login_required || !me.authenticated) {
    return null;
  }

  return (
    <div className="ml-auto flex items-center gap-3 text-sm">
      <span className="text-[var(--text-secondary)]">{me.username}</span>
      <span className="rounded-full border border-[var(--border-subtle)] px-2 py-0.5 text-xs text-[var(--text-muted)]">
        {me.role}
      </span>
      <button
        type="button"
        onClick={() => {
          logout.mutate();
        }}
        className="text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
      >
        Log out
      </button>
    </div>
  );
}

/** Top-level nav. `pathname === to`, not `startsWith`, so `/rules` does
 * not light up on `/`; a nested route therefore does not light its parent. */
export function AppNav() {
  const location = useLocation();

  return (
    <nav className="border-b border-[var(--border-subtle)] bg-[var(--surface-raised)]">
      <div className="mx-auto flex max-w-7xl items-center gap-6 px-8">
        {/* `-ml-*` pulls the logo outside the container's `px-8`; `h-*` on
            the img sizes it and the wrapper's `gap-*` spaces it. */}
        <Link to="/" className="-ml-2 flex shrink-0 items-center py-1">
          <img src="/redbull-logo.svg" alt="Red Bull" className="h-14 w-auto" />
        </Link>
        {LINKS.map((link) => {
          const isActive =
            link.to === "/" ? location.pathname === "/" : location.pathname.startsWith(link.to);
          return (
            <Link
              key={link.to}
              to={link.to}
              className={`relative py-3 text-sm font-medium ${
                isActive
                  ? "text-[var(--text-primary)]"
                  : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
              }`}
            >
              {link.label}
              {/* A bar, not just a colour change, so "where am I" does not
                  depend on telling two greys apart. */}
              {isActive && (
                <span
                  aria-hidden="true"
                  className="absolute inset-x-0 -bottom-px h-0.5 bg-[var(--color-status-info)]"
                />
              )}
            </Link>
          );
        })}
        <AccountMenu />
      </div>
    </nav>
  );
}
