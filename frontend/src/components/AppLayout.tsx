import { Outlet } from "react-router";

import { AppFooter } from "@/components/AppFooter";
import { AppNav } from "@/components/AppNav";

/** Nav above every route's outlet, footer below; routes keep their own
 * `<main>`. `min-h-dvh` + `flex-1` pin the footer to the viewport bottom
 * on a short page. */
export function AppLayout() {
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
