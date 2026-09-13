import { createBrowserRouter } from "react-router";

import { AppLayout } from "@/components/AppLayout";
import { RouteErrorBoundary } from "@/components/RouteErrorBoundary";
import { InventoryPage } from "@/features/inventory/InventoryPage";
import { RulesPage } from "@/features/rules/RulesPage";
import { ServerDetailPage } from "@/features/servers/ServerDetailPage";
import { SitesOverviewPage } from "@/features/sites/SitesOverviewPage";
import { StatusPage } from "@/routes/StatusPage";

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    errorElement: <RouteErrorBoundary />,
    children: [
      {
        path: "/",
        element: <SitesOverviewPage />,
      },
      {
        path: "/servers",
        element: <InventoryPage />,
      },
      {
        path: "/servers/:id",
        element: <ServerDetailPage />,
      },
      {
        // Read-only on purpose (docs/architecture.md, "Slice 5").
        path: "/rules",
        element: <RulesPage />,
      },
      {
        path: "/status",
        element: <StatusPage />,
      },
    ],
  },
]);
