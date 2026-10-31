// Explicit interactions against the real scripted controller. No automatic SMS completion.
// The lab is a judge-view tool (R2): it opens only in the judge view, and its simulated
// SMS is the experiment's own phone page in a frame, never an inbox read by this page.
// The submission build closes the lab (RW-4''): its journeys run in the lab lanes
// (@lab-scripted, @lab-unknown), on a server that opens it; their screenshots are
// test-only. The @scripted journey checks that the submission build draws no entry.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');
const { T } = h;

async function clickLab(page, id, endpoint) {
  const response = await h.clickAndWait(page, page.locator(T(id)), endpoint);
  const body = await response.json();
  expect(body.ok, JSON.stringify(body.error || {})).toBe(true);
  return body.lab;
}

async function openLab(page) {
  await h.start(page, { view: 'judge' });
  await Promise.all([h.waitApi(page, '/api/lab', 'GET'), page.locator(T('lab-toggle')).click()]);
  await expect(page.locator(T('lab-workspace'))).toBeVisible();
  await expect(page.locator(T('phone-pane'))).toBeHidden();
}

async function prepare(page, scenario) {
  const initial = await clickLab(page, 'lab-start-' + scenario, '/api/lab/start');
  expect(initial.can_change).toBe(true);
  expect(initial.proof.dispatches).toBe(0);
  const changed = await clickLab(page, 'lab-change', '/api/lab/change');
  expect(changed.previous.task.gesture).toBeNull();
  expect(changed.proof.card_changed).toBe(true);
  await expect(page.locator(T('lab-original-card'))).toBeVisible();
  await expect(page.locator(T('lab-current-card'))).toBeVisible();
  const refused = await clickLab(page, 'lab-try-previous', '/api/lab/try-previous');
  expect(refused.last_attempt.status).toBe('refused');
  expect(refused.last_attempt.code).toBe('not_found');
  expect(refused.last_attempt.operation_status).toBe(404);
  // Verify-only: the old request on its withdrawn task, then the old token on the new card.
  expect(refused.last_attempt.checks.map(item => [item.target, item.code, item.operation_status])).toEqual([
    ['withdrawn_request', 'not_found', 404],
    ['current_card', 'gesture_mismatch', 409],
  ]);
  await expect(page.locator(T('lab-check'))).toHaveCount(2);
  expect(refused.proof.dispatches).toBe(0);
  expect(refused.proof.bank_receipts).toHaveLength(0);
  await expect(page.locator(T('lab-attempt-code'))).toHaveText('not_found');
  return refused;
}

async function noOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
}

async function capture(page, name, options = {}) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await h.screenshot(page, '19-authorization-lab', name, { fullPage: true, ...options });
}

test.describe('19-authorization-lab', () => {
  let errors;
  test.beforeEach(async ({ page }) => {
    errors = [];
    page.on('pageerror', error => errors.push(error.message));
  });
  test.afterEach(() => expect(errors).toEqual([]));

  test('the submission build draws no lab entry and sends no lab request @scripted', async ({ page }) => {
    const bodies = h.collectApiBodies(page);
    let labRequests = 0;
    page.on('request', request => {
      if (new URL(request.url()).pathname.startsWith('/api/lab')) labRequests += 1;
    });
    await h.start(page, { view: 'judge' });
    expect((await h.latestState(bodies)).capabilities.lab).toBe(false);
    await expect(page.locator(T('evidence-pane'))).toHaveCount(1);
    await expect(page.locator(T('lab-toggle'))).toHaveCount(0);
    await expect(page.locator(T('lab-workspace'))).toBeHidden();
    await h.setLocale(page, 'en');
    await expect(page.locator(T('lab-toggle'))).toHaveCount(0);
    await h.setView(page, 'customer');
    await expect(page.locator(T('lab-toggle'))).toHaveCount(0);
    expect(labRequests).toBe(0);
  });

  test('yellow: withdrawn request refuses, current card dispatches once @lab-scripted', async ({ page }) => {
    await openLab(page);
    await capture(page, 'entry');
    await prepare(page, 'yellow');
    await capture(page, 'yellow-old-refused');
    const done = await clickLab(page, 'lab-confirm', '/api/lab/gesture');
    expect(done.proof.dispatches).toBe(1);
    expect(done.proof.bank_receipts).toHaveLength(1);
    expect(done.proof.audit_valid).toBe(true);
    await expect(page.locator(T('lab-dispatches'))).toHaveText('1');
    await expect(page.locator(T('lab-receipts-count'))).toHaveText('1');
    await expect(page.locator(T('lab-confirm'))).toHaveCount(0);
    await expect(page.locator('.lab-current h2')).toHaveText(h.catalog('web.ui.lab.current.retained.title'));
    await noOverflow(page);
    await capture(page, 'yellow-receipt');
  });

  test('red: only explicit SMS open, typing, submit and final confirmation @lab-scripted', async ({ page }) => {
    let phoneGets = 0;
    let labPhoneGets = 0;
    let gesturePosts = 0;
    page.on('request', request => {
      const pathname = new URL(request.url()).pathname;
      if (pathname.startsWith('/api/phone/')) phoneGets += 1;
      if (pathname === '/api/lab/phone') labPhoneGets += 1;
      if (pathname === '/api/lab/gesture') gesturePosts += 1;
    });
    await openLab(page);
    await prepare(page, 'red');
    await clickLab(page, 'lab-confirm', '/api/lab/gesture');
    const requested = await clickLab(page, 'lab-mfa-request', '/api/lab/gesture');
    await expect(page.locator(T('lab-code-input'))).toBeVisible();
    await expect(page.locator(T('lab-sms-frame'))).toHaveCount(0);
    const prior = gesturePosts;
    const childPhones = phoneGets;
    await page.locator(T('lab-phone-open')).click();
    const frame = page.frameLocator(T('lab-sms-frame'));
    const message = frame.locator('[data-testid="sms-message"][data-status="active"]').last();
    await expect(message).toBeVisible();
    expect(phoneGets).toBeGreaterThan(childPhones);
    expect(labPhoneGets).toBe(0);
    const match = (await message.innerText()).match(/(?<![0-9])([0-9]{6})(?![0-9])/);
    expect(match).not.toBeNull();
    // No lab session API returned the code: the child's own phone page alone shows it.
    expect(JSON.stringify(requested)).not.toContain(match[1]);
    const field = page.locator(T('lab-code-input'));
    await expect(field).toHaveValue('');
    await field.pressSequentially(match[1], { delay: 30 });
    await page.waitForTimeout(400);
    expect(gesturePosts).toBe(prior);
    await expect(page.locator(T('lab-dispatches'))).toHaveText('0');
    await capture(page, 'red-explicit-code', { testOnly: true });
    await clickLab(page, 'lab-mfa-verify', '/api/lab/gesture');
    await expect(page.locator(T('lab-code-input'))).toHaveCount(0);
    await expect(page.locator(T('lab-dispatches'))).toHaveText('0');
    const done = await clickLab(page, 'lab-confirm', '/api/lab/gesture');
    expect(done.proof.dispatches).toBe(1);
    expect(done.proof.bank_receipts).toHaveLength(1);
    await expect(page.locator('.lab-current h2')).toHaveText(h.catalog('web.ui.lab.current.retained.title'));
    await capture(page, 'red-receipt');
  });

  test('rapid double click cannot dispatch twice @lab-scripted', async ({ page }) => {
    await openLab(page);
    await prepare(page, 'yellow');
    const response = h.waitApi(page, '/api/lab/gesture');
    await page.locator(T('lab-confirm')).dblclick({ delay: 10 });
    const body = await (await response).json();
    expect(body.lab.proof.dispatches).toBe(1);
    await expect(page.locator(T('lab-confirm'))).toHaveCount(0);
    await expect(page.locator(T('lab-receipts-count'))).toHaveText('1');
  });

  test('locale, view, return to bank, restart and reset preserve boundaries @lab-scripted', async ({ page }) => {
    await openLab(page);
    await prepare(page, 'yellow');
    const en = h.waitApi(page, '/api/lab', 'GET');
    await h.setLocale(page, 'en');
    await en;
    await expect(page.locator('#lab-title')).toHaveText(h.catalog('web.ui.lab.title', 'en'));
    await expect(page.locator(T('lab-current-card'))).toBeVisible();
    // The customer view never shows the lab: no entry, no workspace, no lab request.
    await h.clickAndWait(page, page.locator(T('view-customer')), '/api/session/view');
    await expect(page.locator(T('lab-workspace'))).toBeHidden();
    await expect(page.locator(T('lab-toggle'))).toHaveCount(0);
    await expect(page.locator(T('phone-pane'))).toBeVisible();
    await h.clickAndWait(page, page.locator(T('view-judge')), '/api/session/view');
    await Promise.all([h.waitApi(page, '/api/lab', 'GET'), page.locator(T('lab-toggle')).click()]);
    await expect(page.locator(T('lab-current-card'))).toBeVisible();
    await noOverflow(page);
    await capture(page, 'english-judge');
    await page.locator(T('lab-toggle')).click();
    await expect(page.locator(T('phone-pane'))).toBeVisible();
    await expect(page.locator(T('composer-input'))).toBeVisible();
    await expect(page.locator(T('lab-workspace'))).toBeHidden();
    await Promise.all([h.waitApi(page, '/api/lab', 'GET'), page.locator(T('lab-toggle')).click()]);
    await expect(page.locator(T('lab-current-card'))).toBeVisible();
    const restarted = await clickLab(page, 'lab-start-red', '/api/lab/start');
    expect(restarted.previous).toBeNull();
    await expect(page.locator(T('lab-current-card'))).toHaveCount(0);
    await expect(page.locator(T('lab-dispatches'))).toHaveText('0');
    await h.resetSession(page);
    await expect(page.locator(T('lab-workspace'))).toBeHidden();
    await Promise.all([h.waitApi(page, '/api/lab', 'GET'), page.locator(T('lab-toggle')).click()]);
    await expect(page.locator(T('lab-original-card'))).toHaveCount(0);
    await expect(page.locator(T('lab-proof'))).toHaveCount(0);
  });

  test('narrow layout keeps cards and explicit actions usable @lab-scripted @mobile', async ({ page }) => {
    await openLab(page);
    await prepare(page, 'yellow');
    await noOverflow(page);
    await page.locator(T('lab-confirm')).scrollIntoViewIfNeeded();
    await capture(page, 'mobile-before-confirm');
    const done = await clickLab(page, 'lab-confirm', '/api/lab/gesture');
    expect(done.proof.dispatches).toBe(1);
    await noOverflow(page);
  });

  test('unknown backend outcome clears cached gestures @lab-unknown', async ({ page }) => {
    await openLab(page);
    await prepare(page, 'yellow');
    const refreshed = h.waitApi(page, '/api/lab', 'GET');
    const response = await h.clickAndWait(page, page.locator(T('lab-confirm')), '/api/lab/gesture');
    expect(response.status()).toBe(500);
    await refreshed;
    await expect(page.locator(T('lab-confirm'))).toHaveCount(0);
    await expect(page.locator(T('lab-attempt-status'))).toHaveText(h.catalog('web.ui.lab.status.unknown'));
    await capture(page, 'unknown-closed', { testOnly: true });
  });
});
