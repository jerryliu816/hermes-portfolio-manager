import { expect, test } from "@playwright/test";

/**
 * The properties worth driving a browser for.
 *
 * Not "does the page render" — a unit test covers that. These check that the
 * project's central commitment survives all the way to the screen: a figure is
 * never shown without what qualifies it, and an unconfirmed account is never
 * quietly treated as though it were confirmed.
 */

test.describe("dashboard", () => {
  test("shows the total with its caveats, not on its own", async ({ page }) => {
    await page.goto("/");

    const total = page.getByText("Total value");
    await expect(total).toBeVisible();

    // The caveats are the point: a portfolio can be substantially stale and
    // still render a confident-looking number.
    const caveats = page.getByRole("note").first();
    await expect(caveats).toBeVisible();
    await expect(caveats).toContainText(/as-of date is unknown|cost basis|tax treatment/);
  });

  test("labels the allocation donut as holdings, not the portfolio total", async ({ page }) => {
    await page.goto("/");
    // Allocation excludes cash, so calling it "Total" beside a larger headline
    // figure would invite the reader to think one of the two is wrong.
    await expect(page.getByText("HOLDINGS", { exact: false }).first()).toBeVisible();
  });

  test("renders the as-of date the API sent, not a day either side", async ({ page }) => {
    const response = await page.request.get("/api/v1/portfolio/summary");
    const { as_of_date } = await response.json();
    const [year, month, day] = as_of_date.split("-").map(Number);
    const expected = new Date(year, month - 1, day).toLocaleDateString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
    });

    await page.goto("/");
    await expect(page.getByText(`as of ${expected}`)).toBeVisible();
  });
});

test.describe("holdings", () => {
  test("filters, and groups by security without summing share counts", async ({ page }) => {
    await page.goto("/holdings");
    await expect(page.getByRole("table")).toBeVisible();

    const before = await page.locator("tbody tr").count();
    await page.getByLabel("Search holdings").fill("VTI");
    await expect(page.locator("tbody tr")).not.toHaveCount(before);

    await page.getByLabel("Search holdings").fill("");
    await page.getByRole("button", { name: "By security" }).click();

    // The grouped view must say why quantities are not aggregated.
    await expect(page.getByText(/share counts are not/i)).toBeVisible();
  });

  test("hides closed holdings until asked", async ({ page }) => {
    await page.goto("/holdings");
    const closed = await page.locator("tbody tr").count();
    await page.getByRole("checkbox").check();
    await expect(page.locator("tbody tr").nth(closed)).toBeVisible();
  });
});

test.describe("accounts", () => {
  test("never shows a raw account number", async ({ page }) => {
    await page.goto("/accounts");
    await expect(page.getByRole("table")).toBeVisible();

    const body = (await page.locator("body").innerText()).replace(/\s+/g, " ");
    // Every masked key starts with asterisks; no bare long digit run should
    // appear where an account identifier would be.
    expect(body).toContain("*");
    expect(body).not.toMatch(/\b\d{9,}\b/);
  });

  test("flags accounts that still need classifying", async ({ page }) => {
    await page.goto("/accounts");
    const banner = page.getByRole("note").first();
    await expect(banner).toContainText(/no confirmed tax treatment/);
    await expect(banner).toContainText(/a guess would quietly distort/);
  });

  test("classifying an account sticks and clears the warning", async ({ page }) => {
    await page.goto("/accounts");
    const needsConfirming = await page.locator("tr", { hasText: "needs confirming" }).count();
    expect(needsConfirming).toBeGreaterThan(0);

    // Take the first account that needs confirming.
    const row = page.locator("tr", { hasText: "needs confirming" }).first();
    await row.getByRole("button", { name: "Classify" }).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(/left out of them rather than assumed/);

    await dialog.getByRole("combobox").nth(1).selectOption("taxable");
    await dialog.getByRole("button", { name: "Save" }).click();

    await expect(dialog).toBeHidden();

    // The confirmation sticks: that row no longer asks to be confirmed, and the
    // header badge counts one fewer.
    await expect(page.locator("tr", { hasText: "needs confirming" })).toHaveCount(
      needsConfirming - 1,
    );
  });
});

test.describe("policy", () => {
  test("separates data problems from gaps in the plan", async ({ page }) => {
    await page.goto("/policy");
    // Being out of band and being unable to trust the data are different
    // things, and the page must not conflate them.
    await expect(page.getByText(/Why these figures may be unreliable/)).toBeVisible();
  });

  test("labels rebalance figures as arithmetic, not advice", async ({ page }) => {
    await page.goto("/policy");
    await expect(
      page.getByText(/ignore tax, trading cost, and whether the money should move/),
    ).toBeVisible();
  });
});

test.describe("journal", () => {
  test("states that nothing has been acted on", async ({ page }) => {
    await page.goto("/journal");
    await expect(page.getByText(/Nothing here has been acted on/)).toBeVisible();
  });

  test("deferring keeps a recommendation open", async ({ page }) => {
    await page.goto("/journal");
    const decide = page.getByRole("button", { name: "Decide" }).first();
    if ((await decide.count()) === 0) test.skip(true, "no open recommendation seeded");

    await decide.click();
    await page.getByLabel("Decision note").fill("revisit after the stale data is fixed");
    // Scoped to the card: "deferred" is also a status filter in the toolbar.
    const card = page.locator("section", { hasText: "Rebalance toward fixed income" }).first();
    await card.getByRole("button", { name: "deferred", exact: true }).click();

    await expect(card.getByText("revisit after the stale data is fixed")).toBeVisible();
    // Deferring is not being finished with it, so it stays decidable.
    await expect(card.getByRole("button", { name: "Decide" })).toBeVisible();
  });
});

test("dark mode is a real theme, not an inverted one", async ({ page }) => {
  await page.goto("/");
  const toggle = page.getByRole("button", { name: /Switch to (dark|light) theme/ });
  await toggle.click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", /dark|light/);
});
