import { createBrowserRouter } from "react-router";

import { AppLayout } from "@/components/AppLayout";
import { AdminGate } from "@/components/AdminGate";
import { AuditGate } from "@/components/AuditGate";
import { RouteErrorBoundary } from "@/components/RouteErrorBoundary";
import { ArchitecturePage } from "@/features/architecture/ArchitecturePage";
import { ConfsPage } from "@/features/confs/ConfsPage";
import { EventsPage } from "@/features/events/EventsPage";
import { InventoryPage } from "@/features/inventory/InventoryPage";
import { JobsPage } from "@/features/jobs/JobsPage";
import { RulesPage } from "@/features/rules/RulesPage";
import { SkillPage } from "@/features/skill/SkillPage";
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
        // Read-only: another system writes the `confs` collection (docs/adr/0061-confs.md).
        path: "/confs",
        element: <ConfsPage />,
      },
      {
        path: "/events",
        element: (
          <AuditGate>
            <EventsPage />
          </AuditGate>
        ),
      },
      {
        // Run a collector or prune CronJob and read its logs; admin only (docs/adr/0055).
        path: "/jobs",
        element: (
          <AdminGate>
            <JobsPage />
          </AdminGate>
        ),
      },
      {
        // Read-only on purpose (docs/architecture.md, "Slice 5").
        path: "/rules",
        element: <RulesPage />,
      },
      {
        // Pre-built diagrams under public/architecture/ (archify); no data fetch.
        path: "/architecture",
        element: <ArchitecturePage />,
      },
      {
        // Read live from the API: editing the skill changes this page, not the SPA.
        path: "/skill",
        element: <SkillPage />,
      },
      {
        path: "/status",
        element: <StatusPage />,
      },
    ],
  },
]);
