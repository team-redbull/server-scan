import { expect, test } from "@playwright/test";

test.describe("Sites overview", () => {
  test("is the landing page and drills into a pre-filtered server list", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Sites", level: 1 }),
    ).toBeVisible();

    // Every fixed site always renders, even one with no servers — a
    // site with nothing in it and a site that does not exist are
    // different facts, and the UI must be able to show the difference.
    for (const name of ["New York City", "Tel Aviv", "Bat Yam", "Site Five"]) {
      await expect(
        page.getByRole("heading", { name, exact: true }),
      ).toBeVisible();
    }

    // The seeded fleet deliberately includes servers whose names carry no
    // site token, so this bucket is reachable rather than theoretical.
    await expect(
      page.getByRole("heading", { name: "Unassigned", exact: true }),
    ).toBeVisible();

    // The fleet-wide card sums every site card beside it, and names every
    // vendor rather than lumping the ones it has no label for into "Other".
    const acrossSites = page
      .getByRole("link")
      .filter({
        has: page.getByRole("heading", {
          name: "Across all sites",
          exact: true,
        }),
      });
    await expect(acrossSites).toBeVisible();
    await expect(acrossSites).toContainText("Standalone");
    await expect(acrossSites).not.toContainText("Other");

    // Each card links into the server list already filtered to that site.
    await page.getByRole("link", { name: /Site Five/ }).click();
    await expect(page).toHaveURL(/\/servers\?site_id=five/);
    await expect(page.getByRole("heading", { name: "Servers" })).toBeVisible();

    await expect(page.locator("tbody tr").first()).toBeVisible();

    // Every row on a site-filtered list must belong to that site. The site
    // is not a column any more — it is inside the hostname — so this is
    // also what proves the name-derived site actually agrees with the
    // stored value the filter queries on. The exception is the seeded
    // siteless family: on a Cisco server the site comes from its service
    // profile's org instead, and its hostname carries no token at all.
    const names = await page.locator("tbody tr td:nth-child(2)").allInnerTexts();
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) {
      if (name.toLowerCase().startsWith("random-server-")) continue;
      expect(name.toLowerCase()).toContain("five");
    }
  });
});
