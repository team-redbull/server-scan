import { expect, test } from "@playwright/test";
import type { APIRequestContext } from "@playwright/test";

const STAMP = /\d{2}\/\d{2}\/\d{4}.*\d{2}:\d{2}:\d{2}/;

/** A seeded server to mutate, and the reset that leaves it clean. */
async function pickServer(request: APIRequest, index: number) {
  const res = await request.get("/api/v1/servers?page_size=5");
  const { items } = (await res.json()) as {
    items: { id: string; name: string }[];
  };
  const server = items[index];
  if (!server) throw new Error("No seeded server available.");
  await request.delete(`/api/v1/servers/${server.id}/maintenance`);
  return server;
}
type APIRequest = APIRequestContext;

test.describe("Events page", () => {
  test("columns, server names, dates and the type filter", async ({
    page,
    request,
  }) => {
    const server = await pickServer(request, 1);
    await request.put(`/api/v1/servers/${server.id}/maintenance`, {
      data: { reason: "e2e events page" },
    });
    await request.delete(`/api/v1/servers/${server.id}/maintenance`);

    await page.goto("/events");
    const headers = page.locator("thead th");
    await expect(headers).toHaveText([
      "Server",
      "What happened",
      "When",
      "User",
    ]);

    const rows = page.locator("tbody tr");
    await expect(rows.first()).toBeVisible();
    const serverCell = rows.first().locator("td").first();
    await expect(serverCell).not.toContainText("srv_");
    await expect(serverCell.getByRole("link")).toBeVisible();
    await expect(rows.first().locator("td").nth(2)).toHaveText(STAMP);

    const link = rows.first().getByRole("link");
    const before = await link.boundingBox();
    await rows
      .first()
      .getByRole("button", { name: /details/i })
      .click();
    await expect(page.locator("tbody pre").first()).toBeVisible();
    expect(await link.boundingBox()).toEqual(before);

    await page.getByLabel("Event type").selectOption("MAINTENANCE_DISABLED");
    await expect(rows.first().locator("td").nth(1)).toContainText(
      "MAINTENANCE_DISABLED",
    );
    await expect(rows.first().locator("td").nth(1)).not.toContainText(
      "Maintenance ended",
    );
    await rows
      .first()
      .getByRole("button", { name: /details/i })
      .click();
    await expect(
      page.locator("tbody").getByText("Maintenance ended").first(),
    ).toBeVisible();
    for (const text of await rows.locator("td:nth-child(2)").allInnerTexts()) {
      expect(text).toContain("MAINTENANCE_DISABLED");
    }
  });

  test("the server filter is a name-contains search", async ({ page }) => {
    await page.goto("/events");
    await page.getByLabel("Server", { exact: true }).fill("ocp-dell");
    await expect(page).toHaveURL(/server_name=ocp-dell/);
    const names = page.locator("tbody tr td:first-child");
    await expect(names.first()).toContainText("ocp-dell");
    await expect
      .poll(async () =>
        (await names.allInnerTexts()).every((t) =>
          t.toLowerCase().includes("ocp-dell"),
        ),
      )
      .toBe(true);
  });

  test("the user filter offers readable labels", async ({ page }) => {
    await page.goto("/events");
    const user = page.getByRole("combobox", { name: "User" });
    await user.click();
    const options = page
      .getByRole("listbox", { name: "Users" })
      .getByRole("option");
    await expect(options.filter({ hasText: "System (ingestion)" })).toHaveCount(
      1,
    );
    await user.fill("ingest");
    await expect(options).toHaveCount(1);
    await options.filter({ hasText: "System (ingestion)" }).click();
    await expect(page).toHaveURL(/actor_id=ingestion/);
    await expect(page.locator("tbody tr").first()).toBeVisible();
    for (const text of await page
      .locator("tbody tr td:last-child")
      .allInnerTexts()) {
      expect(text).toContain("System (ingestion)");
    }
  });

  test("time range: Last hour keeps fresh events, a past custom range drops them", async ({
    page,
    request,
  }) => {
    const server = await pickServer(request, 2);
    await request.put(`/api/v1/servers/${server.id}/maintenance`, {
      data: { reason: "e2e range" },
    });

    await page.goto(
      "/events?event_type=MAINTENANCE_ENABLED&server_name=" + server.name,
    );
    await expect(page.locator("tbody tr").first()).toBeVisible();
    await page.getByLabel("Time range").selectOption("1h");
    await expect(page.locator("tbody tr").first()).toContainText(
      "MAINTENANCE_ENABLED",
    );

    await page.getByLabel("Time range").selectOption("custom");
    await page.getByLabel(/^From/).fill("01/01/2020 00:00");
    await page.getByLabel(/^To/).fill("02/01/2020 00:00");
    await expect(page.getByText("No events recorded.")).toBeVisible();

    await request.delete(`/api/v1/servers/${server.id}/maintenance`);
  });

  test("HEALTH_CHANGED lists only health transitions", async ({ page }) => {
    await page.goto("/events");
    await page.getByLabel("Event type").selectOption("HEALTH_CHANGED");
    const rows = page.locator("tbody tr");
    await expect(rows.first()).toBeVisible();
    const cells = await rows.locator("td:nth-child(2)").allInnerTexts();
    expect(cells.length).toBeGreaterThan(0);
    for (const text of cells) {
      expect(text).toContain("HEALTH_CHANGED");
      expect(text).toMatch(
        /(HEALTHY|WARNING|MAJOR|CRITICAL|UNKNOWN)\s*→\s*(HEALTHY|WARNING|MAJOR|CRITICAL|UNKNOWN)/,
      );
      expect(text).not.toMatch(
        /Health (HEALTHY|WARNING|MAJOR|CRITICAL|UNKNOWN)/,
      );
    }
  });
});

test.describe("Server history and donors", () => {
  test("the History tab narrates a donor marking and its end", async ({
    page,
    request,
  }) => {
    const server = await pickServer(request, 3);
    await request.put(`/api/v1/servers/${server.id}/maintenance`, {
      data: { reason: "e2e DONOR for history" },
    });
    await request.delete(`/api/v1/servers/${server.id}/maintenance`);

    await page.goto(`/servers/${server.id}`);
    await page.getByRole("button", { name: "History", exact: true }).click();
    const donorRow = page.locator("tbody tr", {
      hasText: "MAINTENANCE_ENABLED",
    });
    await expect(donorRow.first()).not.toContainText("e2e DONOR for history");
    await expect(donorRow.first().locator("td:last-child")).toContainText(
      /\buser\b/,
    );
    await donorRow
      .first()
      .getByRole("button", { name: /details/i })
      .click();
    await expect(
      page
        .locator("tbody")
        .getByText(/Marked as parts donor by .*e2e DONOR for history/)
        .first(),
    ).toBeVisible();
    await expect(
      page.locator("tbody tr", { hasText: "MAINTENANCE_DISABLED" }).first(),
    ).toBeVisible();
  });

  test("a donor shows the Donor badge, not Maint, and the filter narrows to it", async ({
    page,
    request,
  }) => {
    const server = await pickServer(request, 4);
    await request.put(`/api/v1/servers/${server.id}/maintenance`, {
      data: { reason: "e2e DONOR badge" },
    });

    await page.goto("/servers");
    const row = page.locator("tbody tr", {
      has: page.getByRole("link", { name: server.name, exact: true }),
    });
    await expect(row.getByText(/Donor/)).toBeVisible();
    await expect(row.getByText(/Maint/)).toHaveCount(0);
    await expect(row.getByText(/Donor/)).toHaveAttribute("title", /by \S+/);

    await page.getByRole("checkbox", { name: "Donor" }).click();
    await expect(page).toHaveURL(/donor=true/);
    await expect(
      page.getByRole("link", { name: server.name, exact: true }),
    ).toBeVisible();
    await expect(page.locator("tbody tr")).not.toHaveCount(0);
    await expect(page.locator("tbody").getByText(/Maint/)).toHaveCount(0);

    await request.delete(`/api/v1/servers/${server.id}/maintenance`);
  });
});
