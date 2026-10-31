// 00-smoke (core): first screen, corner notice, login, nothing executed.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');
const { serve } = require('../lib/fixtures.cjs');

const { T } = h;

test.describe('00-smoke', () => {
  test('first screen is the customer view, login works, nothing is executed @scripted', async ({
    page,
    request,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.openApp(page);
    await expect(page.locator(T('evidence-pane'))).toHaveCount(0);
    await expect(page.locator(T('corner-notice'))).toBeVisible();
    const first = (await h.latestState(bodies));
    expect(first.view).toBe('customer');
    expect(first.evidence).toBeUndefined();
    expect(first.contract).toBe('web-contract/1.1');
    await h.screenshot(page, '00-smoke', 'login');

    await h.login(page);
    await expect(page.locator(T('chat-log'))).toBeAttached();
    await expect(page.locator(T('device-pane'))).toBeVisible();
    await expect(page.locator(`${T('device-pane')} ${T('device-task')}`)).toHaveCount(0);
    await h.screenshot(page, '00-smoke', 'home');

    await h.setView(page, 'judge');
    await expect(page.locator(T('judge-strip'))).toBeVisible();
    await expect(page.locator(T('evidence-dispatch'))).toHaveAttribute('data-count', '0');
    const counts = await h.probe(request);
    if (counts) {
      expect(counts.device_approve).toBe(0);
      expect(counts.device_complete_mfa).toBe(0);
    }
    await h.screenshot(page, '00-smoke', 'judge-home');
  });

  test('judge link opens the judge view on a new session @scripted', async ({ page }) => {
    await h.openApp(page, { view: 'judge' });
    await expect(page.locator(T('evidence-pane'))).toHaveCount(1);
    await h.assertCornerOnTop(page);
  });

  test('fixtures mode serves the page and the contract examples @fixtures', async ({ page }) => {
    const api = await serve(page, { state: 'customer-red-code' });
    await page.goto('/');
    await expect(page.locator(T('stage-header'))).toBeVisible();
    await expect(page.locator(T('corner-notice'))).toBeVisible();
    await h.assertCornerOnTop(page);
    await expect(page.locator(T('evidence-pane'))).toHaveCount(0);
    const session = api.requests.find((item) => item.path === '/api/session');
    expect(session, 'the page opens a session first').toBeTruthy();
    expect(session.headers['x-corridor-web']).toBe('1');
    await h.screenshot(page, '00-smoke', 'fixtures-red-code');
  });
});
