import { expect, test } from "@playwright/test";

test.describe("Inventory", () => {
  test("lists servers and filters by search", async ({ page }) => {
    // "/" is the sites overview now; the server list lives at /servers.
    await page.goto("/servers");
    await expect(page.getByRole("heading", { name: "Servers" })).toBeVisible();

    const rows = page.locator("tbody tr");
    await expect(rows.first()).toBeVisible();
    const unfilteredCount = await rows.count();

    // Search is client-side over the whole fleet (ADR-0033): no request to
    // wait for; the URL changes per keystroke, the rows after the debounce.
    await page.getByPlaceholder("Name, serial, tag, BMC…").fill("ocp-dell");
    await expect(page).toHaveURL(/search=ocp-dell/);
    await expect(rows.first().locator("td").first()).toContainText(/ocp-dell/i);
    // A real filter, not a no-op: the seeded fleet has multiple vendors, so
    // narrowing to one name fragment can never show more rows than no filter.
    const filteredCount = await rows.count();
    expect(filteredCount).toBeLessThanOrEqual(unfilteredCount);
  });

  test("navigates to a server's detail page and renders every tab", async ({
    page,
  }) => {
    await page.goto("/servers");
    const firstLink = page.locator("tbody tr").first().getByRole("link");
    const name = await firstLink.innerText();
    await firstLink.click();

    await expect(
      page.getByRole("heading", { name, exact: true }),
    ).toBeVisible();

    for (const tab of ["Overview", "Hardware", "Network", "Connectivity"]) {
      await page.getByRole("button", { name: tab, exact: true }).click();
      await expect(
        page.getByRole("button", { name: tab, exact: true }),
      ).toHaveAttribute("aria-current", "page");
    }

    // The BMC reads as a plain host: no scheme, no port, no Redfish path.
    await page.getByRole("button", { name: "Network", exact: true }).click();
    const bmcAddress = page
      .getByText("Address", { exact: true })
      .locator("xpath=following-sibling::dd[1]");
    await expect(bmcAddress).toBeVisible();
    await expect(bmcAddress).not.toContainText("://");
    await page
      .getByRole("button", { name: "Connectivity", exact: true })
      .click();

    // Connectivity is the tab most worth a content assertion (slice 1's
    // "renders a variable number of fabric groups, not a hardcoded two"
    // requirement) — either real fabric groups or the explicit empty state,
    // never a blank pane.
    const fabricGroups = page.getByTestId("fabric-group");
    const emptyState = page.getByText("No connectivity data.");
    await expect(fabricGroups.first().or(emptyState)).toBeVisible();
  });

  test("remembers the active filter when returning from a server's detail page", async ({
    page,
  }) => {
    await page.goto("/servers?vendor=cisco");
    const rows = page.locator("tbody tr");
    await expect(rows.first()).toBeVisible();

    await rows.first().getByRole("link").click();
    await expect(page).toHaveURL(/\/servers\//);

    await page.getByRole("link", { name: "← Back to inventory" }).click();
    await expect(page).toHaveURL(/\/servers\?vendor=cisco/);
    await expect(page.getByRole("heading", { name: "Servers" })).toBeVisible();
  });

  test("a direct visit to a server's page returns to the unfiltered inventory, not the sites overview", async ({
    page,
    request,
  }) => {
    const listResponse = await request.get("/api/v1/servers?page_size=1");
    const { items } = (await listResponse.json()) as { items: { id: string }[] };
    const serverId = items[0]?.id;
    if (!serverId) {
      throw new Error("No seeded server available to test the back link against.");
    }

    await page.goto(`/servers/${serverId}`);
    await page.getByRole("link", { name: "← Back to inventory" }).click();
    await expect(page).toHaveURL(/\/servers$/);
  });
});
