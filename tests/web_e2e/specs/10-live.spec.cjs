// 10-live (S7, live-fake only): unlock with the access code (challenge on click, field
// cleared at once), switch on, the badge names a test fake model, the controls lock during a
// transaction and a deployment change applies at the next boundary. Screenshots are
// test-only; this spec is never evidence that "the model changed and the rules held".
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;
const FAKE = '（测试假模型）';

async function openPanel(page) {
  if (!(await page.locator(T('live-sheet')).isVisible().catch(() => false))) {
    await Promise.all([
      page.waitForResponse((response) => new URL(response.url()).pathname === '/api/live'),
      page.locator(T('planner-badge')).click(),
    ]);
  }
  await expect(page.locator(T('live-sheet'))).toBeVisible();
}

async function closePanel(page) {
  await page.keyboard.press('Escape');
  await expect(page.locator(T('live-sheet'))).toBeHidden();
}

test.describe('10-live', () => {
  test('unlock, switch on, fake-model badge, locked mid-transaction, switch deployment at the boundary @live-fake', async ({
    page,
    request,
  }) => {
    const accessCode = h.ready().access_code;
    expect(accessCode, 'the live-fake harness publishes its sentinel access code').toBeTruthy();
    const bodies = h.collectApiBodies(page);
    await h.start(page, { view: 'judge' });
    expect((await h.latestState(bodies)).capabilities.live).toBe(true);
    await openPanel(page);

    const field = page.locator(`${T('live-sheet')} ${T('live-code')}`);
    await expect(field).toHaveAttribute('type', 'password');
    await field.fill(accessCode);
    const challenge = h.waitApi(page, '/api/live/challenge');
    const unlock = h.waitApi(page, '/api/live/unlock');
    await page.locator(T('live-unlock')).click();
    await challenge;
    await expect(field).toHaveValue('');
    const unlocked = await (await unlock).json();
    expect(unlocked.result).toBe('accepted');
    await expect(page.locator(T('live-result'))).toBeVisible();

    await h.clickAndWait(page, page.locator(T('live-toggle')), '/api/live/toggle');
    const badge = page.locator(T('planner-badge'));
    await expect(badge).toHaveAttribute('data-kind', 'model');
    await expect(badge).toContainText(FAKE);
    await h.screenshot(page, '10-live', 'on', { testOnly: true });
    await closePanel(page);

    const rkey = await h.send(page, SAY.yellow);
    await h.expectStep(page, 'confirm');
    await openPanel(page);
    expect(await h.isLocked(page.locator(T('live-toggle')))).toBe(true);
    expect(await h.isLocked(page.locator(T('live-deployment')))).toBe(true);
    await expect(page.locator(T('live-reason'))).toBeVisible();
    await h.screenshot(page, '10-live', 'locked-mid-transaction', { testOnly: true });
    await closePanel(page);
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await expect(h.responseOf(page, rkey).locator(T('result-banner'))).toBeVisible();

    await openPanel(page);
    const deployment = page.locator(T('live-deployment'));
    // Back at the boundary the switch is free again; the deployment changes only while the
    // live client is off (409 live_on otherwise).
    expect(await h.isLocked(page.locator(T('live-toggle')))).toBe(false);
    await h.latestState(bodies);
    const live = JSON.parse(bodies.filter((entry) => entry.path === '/api/live').pop().body).live;
    expect(live.deployments.every((item) => item.label.endsWith(FAKE))).toBe(true);
    const other = live.deployments.find((item) => item.id !== live.selected);
    if (other) {
      await h.clickAndWait(page, page.locator(T('live-toggle')), '/api/live/toggle');
      expect(await h.isLocked(deployment)).toBe(false);
      const select = (await deployment.evaluate((element) => element.tagName)) === 'SELECT'
        ? deployment
        : deployment.locator('select');
      await Promise.all([h.waitApi(page, '/api/live/select'), select.selectOption(other.id)]);
      await h.clickAndWait(page, page.locator(T('live-toggle')), '/api/live/toggle');
      await expect(badge).toContainText(FAKE);
    }
    await closePanel(page);
    const second = await h.send(page, SAY.balance);
    await expect(h.responseOf(page, second).locator(T('result-banner'))).toBeVisible();
    const evidence = (await h.latestState(bodies)).evidence;
    expect(evidence.planner_log.length).toBeGreaterThan(0);
    const counts = await h.probe(request);
    if (counts) expect(counts.device_complete_mfa).toBe(0);
  });

  test('the customer view shows no credential state, price or counters @live-fake', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    await openPanel(page);
    await h.latestState(bodies);
    const live = JSON.parse(bodies.filter((entry) => entry.path === '/api/live').pop().body).live;
    expect(live.price).toBeUndefined();
    expect(live.counters).toBeUndefined();
    for (const item of live.deployments) expect(item.credential_text).toBeUndefined();
    const text = JSON.stringify(live);
    for (const name of ['LIVE_', 'LLM_', 'CORRIDOR_', 'sk-test-only']) expect(text).not.toContain(name);
  });
});
