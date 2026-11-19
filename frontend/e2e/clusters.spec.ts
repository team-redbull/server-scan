import { expect, test } from "@playwright/test";

test.describe("Clusters", () => {
  test("is in the nav and lists the membership jobs (or says none has reported)", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("link", { name: "Clusters", exact: true }).click();

    await expect(page).toHaveURL(/\/clusters$/);
    await expect(page.getByRole("heading", { name: "Clusters" })).toBeVisible();
    await expect(page.locator("tbody tr").first().or(page.getByText("No membership job has reported yet."))).toBeVisible();
  });
});
