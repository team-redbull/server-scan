import { expect, test } from "@playwright/test";

test.describe("Maintenance", () => {
  test("the server detail page shows maintenance but cannot change it", async ({
    page,
    request,
  }) => {
    const listResponse = await request.get("/api/v1/servers?page_size=1");
    const { items } = (await listResponse.json()) as { items: { id: string }[] };
    const serverId = items[0]?.id;
    if (!serverId) {
      throw new Error("No seeded server available to test maintenance against.");
    }
    await request.put(`/api/v1/servers/${serverId}/maintenance`, {
      data: { reason: "e2e detail read-only" },
    });

    await page.goto(`/servers/${serverId}`);
    await expect(page.getByText("e2e detail read-only")).toBeVisible();
    await expect(page.getByRole("button", { name: /maintenance/i })).toHaveCount(0);
    await expect(page.getByPlaceholder("Reason (optional)")).toHaveCount(0);

    await request.delete(`/api/v1/servers/${serverId}/maintenance`);
  });

  test("toggles maintenance from the inventory list, and the filter keeps up", async ({
    page,
    request,
  }) => {
    const listResponse = await request.get("/api/v1/servers?page_size=1");
    const { items } = (await listResponse.json()) as { items: { id: string; name: string }[] };
    const server = items[0];
    if (!server) {
      throw new Error("No seeded server available to test maintenance against.");
    }
    await request.delete(`/api/v1/servers/${server.id}/maintenance`);

    await page.goto("/servers");

    // Starting maintenance asks why; ending it does not.
    await page.getByRole("button", { name: `Put ${server.name} into maintenance` }).click();
    const card = page.getByRole("dialog");
    await card.getByLabel(/why is it going into maintenance/i).fill("e2e list toggle");
    await card.getByRole("button", { name: "Start maintenance" }).click();
    await expect(card).toBeHidden();
    await expect(
      page.getByRole("button", { name: `End maintenance on ${server.name}` }),
    ).toBeVisible();

    // The reason reached the document, not just the button state.
    const detail = await (await request.get(`/api/v1/servers/${server.id}`)).json();
    expect(detail.maintenance.reason).toBe("e2e list toggle");

    // Under the Maintenance filter the row must LEAVE the list the moment
    // it leaves maintenance: the mutation patches the cached fleet, and the
    // server clears its rows/list cache so the next poll agrees (ADR-0028).
    const maintenanceFilter = page.getByRole("checkbox", { name: "Maintenance" });
    await maintenanceFilter.click();
    await expect(maintenanceFilter).toBeChecked();
    await expect(page.getByRole("link", { name: server.name, exact: true })).toBeVisible();

    await page.getByRole("button", { name: `End maintenance on ${server.name}` }).click();
    await expect(page.getByRole("link", { name: server.name, exact: true })).toHaveCount(0);

    await request.delete(`/api/v1/servers/${server.id}/maintenance`);
  });
});
