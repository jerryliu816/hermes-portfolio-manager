import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests.
 *
 * These drive a real browser against a real API and a real database, because
 * the things worth checking here — that a caveat actually reaches the screen,
 * that a classification sticks — are exactly the things a unit test cannot
 * see. The backend is expected to be running and seeded; `make e2e` does both.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: "npm run dev -- --port 5173",
        url: "http://localhost:5173",
        reuseExistingServer: true,
        timeout: 60_000,
      },
});
