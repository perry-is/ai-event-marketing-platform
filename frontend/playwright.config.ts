import { defineConfig, devices } from "@playwright/test";

const backendPort = process.env.EMP_E2E_BACKEND_PORT ?? "8011";
const frontendPort = process.env.EMP_E2E_FRONTEND_PORT ?? "5174";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: `http://localhost:${frontendPort}`,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1365, height: 900 },
      },
    },
    {
      name: "mobile",
      use: { ...devices["iPhone 13"], defaultBrowserType: "chromium" },
    },
  ],
  webServer: [
    {
      command: "node scripts/start-test-backend.mjs",
      url: `http://127.0.0.1:${backendPort}/health/live`,
      reuseExistingServer: false,
      env: {
        EMP_E2E_BACKEND_PORT: backendPort,
        EMP_E2E_FRONTEND_PORT: frontendPort,
        EMP_E2E_APP_ORIGIN: `http://localhost:${frontendPort}`,
      },
    },
    {
      command: `npm run dev -- --port ${frontendPort}`,
      url: `http://localhost:${frontendPort}`,
      env: { EMP_DEV_API_TARGET: `http://127.0.0.1:${backendPort}` },
      reuseExistingServer: false,
    },
  ],
});
