// Playwright configuration for the web front end's journeys. Run through run_e2e.py,
// which sets NODE_PATH, the mode, the harness URL and the output directory.
const path = require('path');
const { defineConfig } = require('playwright/test');

const OUT = process.env.CORRIDOR_E2E_OUT || path.join(__dirname, '..', '..', '.tmp', 'web-e2e');
const REPORT = process.env.CORRIDOR_E2E_REPORT || path.join(OUT, 'report-local.json');
const RESULTS = process.env.CORRIDOR_E2E_RESULTS || path.join(OUT, 'test-results');
const BASE_URL = process.env.CORRIDOR_WEB_URL || 'http://127.0.0.1:9';
// The public lane: the public name maps to the harness's TLS port, with a throwaway certificate.
const HOST_RULES = process.env.CORRIDOR_E2E_HOST_RULES;
const PUBLIC_LANE = HOST_RULES
  ? { ignoreHTTPSErrors: true, launchOptions: { args: [`--host-resolver-rules=${HOST_RULES}`] } }
  : {};

module.exports = defineConfig({
  testDir: path.join(__dirname, 'specs'),
  testMatch: /.*\.spec\.cjs$/,
  outputDir: RESULTS,
  workers: 1,
  retries: 0,
  fullyParallel: false,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  reporter: [['line'], ['json', { outputFile: REPORT }]],
  use: {
    baseURL: BASE_URL,
    channel: 'msedge',
    headless: true,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    serviceWorkers: 'block',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: 'off',
    screenshot: 'only-on-failure',
    ...PUBLIC_LANE,
  },
  projects: [
    // Specs tagged @mobile run only on the mobile project (15-responsive, best effort).
    { name: 'desktop', use: { viewport: { width: 1920, height: 1080 } }, grepInvert: /@mobile/ },
    {
      name: 'mobile',
      use: { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: false },
      grep: /@mobile/,
    },
  ],
});
