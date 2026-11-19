import { expect, test } from "@playwright/test";

test.describe("Remove a server", () => {
  test("removes it from its detail page, after typing its name, and it is gone from the inventory", async ({
    page,
    request,
  }) => {
    // The last server of the list, so no other spec's "first row" is the one removed.
    const { items } = (await (await request.get("/api/v1/servers/rows")).json()) as {
      items: { id: string; name: string }[];
    };
    const target = items[items.length - 1];
    if (!target) throw new Error("No seeded server to remove.");

    await page.goto(`/servers/${target.id}`);
    await page.getByRole("button", { name: "Remove this server" }).click();
    const dialog = page.getByRole("dialog");
    const confirm = dialog.getByRole("button", { name: "Remove server" });
    await expect(confirm).toBeDisabled();

    await dialog.getByLabel(/Type the server/).fill(target.name);
    await confirm.click();
    // A server in use is refused first; the dialog says why and offers the override.
    const anyway = dialog.getByRole("checkbox", { name: "Remove anyway" });
    if (await anyway.isVisible({ timeout: 1000 }).catch(() => false)) {
      await anyway.check();
      await dialog.getByRole("button", { name: "Remove anyway" }).click();
    }

    await expect(page).toHaveURL(/\/servers$/);
    expect((await request.get(`/api/v1/servers/${target.id}`)).status()).toBe(404);
    const events = (await (
      await request.get(`/api/v1/events?event_type=SERVER_DELETED&server_name=${encodeURIComponent(target.name)}`)
    ).json()) as { items: { server_id: string }[] };
    expect(events.items.some((e) => e.server_id === target.id)).toBe(true);
  });
});
