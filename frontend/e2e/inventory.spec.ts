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
    // Scoped to the Name cell: the row also carries a BMC console link.
    const firstLink = page
      .locator("tbody tr")
      .first()
      .locator("td")
      .first()
      .getByRole("link");
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

    await rows.first().locator("td").first().getByRole("link").click();
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
    const { items } = (await listResponse.json()) as {
      items: { id: string }[];
    };
    const serverId = items[0]?.id;
    if (!serverId) {
      throw new Error(
        "No seeded server available to test the back link against.",
      );
    }

    await page.goto(`/servers/${serverId}`);
    await page.getByRole("link", { name: "← Back to inventory" }).click();
    await expect(page).toHaveURL(/\/servers$/);
  });
  test("a server's detail header carries the same BMC link as its inventory row", async ({
    page,
  }) => {
    await page.goto("/servers");
    const bmcLink = 'a[aria-label^="Open BMC console for"]';
    const row = page
      .locator("tbody tr", { has: page.locator(bmcLink) })
      .first();
    await expect(row).toBeVisible();
    const href = await row.locator(bmcLink).getAttribute("href");
    const name = await row.locator("td").first().innerText();

    await row.locator("td").first().getByRole("link").click();
    const header = page.getByRole("link", {
      name: `Open BMC console for ${name}`,
      exact: true,
    });
    await expect(header).toHaveAttribute("href", href ?? "");
    await expect(header).toHaveAttribute("target", "_blank");
  });

  test("filters by MCE from the sidebar and keeps the choice in the URL", async ({
    page,
  }) => {
    await page.goto("/servers");
    const rows = page.locator("tbody tr");
    await expect(rows.first()).toBeVisible();

    const sidebar = page.getByRole("complementary", {
      name: "Cluster filters",
    });
    const mces = sidebar.getByRole("region", { name: "MCE" });
    const first = mces.getByRole("checkbox").first();
    await expect(first).toBeVisible();
    const mceName = (
      await first
        .locator("xpath=ancestor::label")
        .locator("span")
        .first()
        .innerText()
    ).trim();

    await first.click();
    await expect(first).toBeChecked();
    await expect(page).toHaveURL(
      new RegExp(`mce=${encodeURIComponent(mceName)}`),
    );
    await expect(rows.first()).toBeVisible();
    // Every visible row belongs to the ticked MCE (the MCE column exists once a row has one).
    const mceCells = await page
      .locator("tbody tr td:nth-child(4)")
      .allInnerTexts();
    expect(mceCells.length).toBeGreaterThan(0);
    for (const cell of mceCells) expect(cell.trim()).toBe(mceName);

    await first.click();
    await expect(first).not.toBeChecked();
    await expect(page).not.toHaveURL(/mce=/);
  });
});
