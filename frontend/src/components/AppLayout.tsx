import { Outlet } from "react-router";

import { AppFooter } from "@/components/AppFooter";
import { AppNav } from "@/components/AppNav";
import { useAuth } from "@/features/auth/useAuth";
import { LoginPage } from "@/features/auth/LoginPage";

/** Nav above every route's outlet, footer below; routes keep their own
 * `<main>`. `min-h-dvh` + `flex-1` pin the footer to the viewport bottom
 * on a short page.
 *
 * Gated on `GET /auth/me`: `login_required && !authenticated` renders only
 * `LoginPage` — no nav, no outlet — until a session exists. Auth disabled
 * (this repo's own default) always resolves `authenticated: true`, so
 * nothing here changes from before this feature existed. */
export function AppLayout() {
  const { data: me, isPending } = useAuth();

  if (isPending) {
    return null;
  }

  if (me?.login_required && !me.authenticated) {
    return <LoginPage />;
  }

  return (
    <div className="flex min-h-dvh flex-col">
      <AppNav />
      <div className="flex-1">
        <Outlet />
      </div>
      <AppFooter />
    </div>
  );
}
