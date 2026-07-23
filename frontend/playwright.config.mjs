import { defineConfig } from "@playwright/test";

const externalBaseUrl = process.env.PLAYWRIGHT_BASE_URL;
const baseURL = externalBaseUrl || "http://127.0.0.1:3210";
const localChromeChannel = process.platform === "darwin" ? "chrome" : undefined;
const requestedChannel = process.env.PLAYWRIGHT_CHANNEL;
const browserChannel = requestedChannel === "bundled"
  ? undefined
  : requestedChannel || localChromeChannel;
const singleProcessBrowser = process.env.PLAYWRIGHT_SINGLE_PROCESS === "1";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  workers: process.env.CI ? 2 : 3,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "line",
  outputDir: "test-results/playwright",
  use: {
    baseURL,
    channel: browserChannel,
    launchOptions: singleProcessBrowser
      ? { args: ["--single-process", "--no-zygote"] }
      : undefined,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: externalBaseUrl
    ? undefined
    : {
        command: "next dev --webpack -p 3210",
        url: baseURL,
        timeout: 120_000,
        reuseExistingServer: false,
        env: {
          API_INTERNAL_BASE: "http://127.0.0.1:9",
          NEXT_DIST_DIR: ".next/e2e",
          NEXT_PUBLIC_API_BASE: "",
        },
      },
});
