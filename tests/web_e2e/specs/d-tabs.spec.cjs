// D-line screen specs (fixtures mode) for the ext tabs: accounts, wealth and
// plans. Tabs load their payload with a GET and send only the documented
// bodies; suitability and refusals stay with the server.
const { test, expect } = require('playwright/test');
const { serve } = require('../lib/fixtures.cjs');
const S = require('./d-states.cjs');

const T = (id) => `[data-testid="${id}"]`;

async function open(page, state, tabs) {
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  const api = await serve(page, { state: S.ok(state), tabs });
  await page.goto('/');
  return { api, errors };
}

function posted(page, fragment) {
  return page.waitForRequest(
    (request) => request.method() === 'POST' && new URL(request.url()).pathname.endsWith(fragment),
  );
}

async function click(page, locator, fragment) {
  const [request] = await Promise.all([posted(page, fragment), locator.click()]);
  return JSON.parse(request.postData() || 'null');
}

async function openTab(page, name) {
  const [response] = await Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === `/api/tabs/${name}`),
    page.locator(T(`tab-${name}`)).click(),
  ]);
  expect(response.request().method()).toBe('GET');
}

const caps = (tabs) => S.base({ capabilities: { tabs, shortcuts: ['transfer', 'cards', 'scam_drill', 'subscriptions'] } });

test('@d-accounts balances, bill bars and the subscription fallback', async ({ page }) => {
  const { errors } = await open(page, caps(['accounts']), { accounts: S.TABS.accounts, operator: S.TABS.operator });
  await openTab(page, 'accounts');
  await expect(page.locator(T('accounts-balances'))).toContainText('¥12,000.00');
  await expect(page.getByText('餐饮')).toBeVisible();
  const query = await click(page, page.locator(T('subs-query')), '/api/subscriptions/query');
  expect(query).toEqual({});
  const prepare = page.locator(T('subs-cancel'));
  await expect(prepare).toBeDisabled();
  await page.locator(T('subs-cancel-choice-1')).check();
  const cancel = await click(page, prepare, '/api/subscriptions/cancel');
  expect(cancel).toEqual({ choice: 'c1' });
  expect(errors).toEqual([]);
});

test('@d-wealth compare, pick, purchase and redeem send only form choices', async ({ page }) => {
  await open(page, caps(['wealth']), { wealth: S.TABS.wealth });
  await openTab(page, 'wealth');
  await expect(page.locator(T('wealth-scope-note'))).toBeVisible();
  await page.locator(T('wealth-objective-total_fee')).click();
  expect(await click(page, page.locator(T('wealth-compare')), '/api/wealth/compare')).toEqual({
    objective: 'total_fee',
  });
  expect(await click(page, page.locator(T('wealth-purchase')), '/api/wealth/purchase')).toEqual({ choice: null });
  await page.locator(T('wealth-product-1')).check();
  expect(await click(page, page.locator(T('wealth-purchase')), '/api/wealth/purchase')).toEqual({ choice: 'c1' });
  await expect(page.locator(T('wealth-redeem-1'))).toBeDisabled();
  expect(await click(page, page.locator(T('wealth-redeem-0')), '/api/wealth/redeem')).toEqual({ choice: 'c0' });
  expect(await click(page, page.locator(T('wealth-r4')), '/api/presets/suitability-r4')).toEqual({});
});

test('@d-wealth a refusal card has no device prompt', async ({ page }) => {
  const wealth = S.clone(S.TABS.wealth);
  const refused = S.card('W1-0', 'black', { amount: '¥50,000.00' });
  refused.notes = [{ kind: 'suitability', title: '适当性', lines: ['风险等级不匹配'], footer: null }];
  wealth.r4 = {
    title: '适当性拒绝演示',
    lead: '内核拒绝了这张申购卡',
    card: refused,
    reasons: ['产品风险等级 R4 高于测评结果'],
    dispatches_text: '派发 0 次',
    isolation: '隔离演示，不影响您的会话',
  };
  await open(page, caps(['wealth']), { wealth });
  await openTab(page, 'wealth');
  const refusal = page.locator(T('refusal-card'));
  await expect(refusal).toBeVisible();
  await expect(refusal.locator(T('note-suitability'))).toHaveCount(1);
  await expect(refusal.locator(T('card')).locator(T('card-note'))).toHaveCount(0);
  await expect(page.locator(T('device-open'))).toHaveCount(0);
});

test('@d-plans task graph and operator snapshot', async ({ page }) => {
  await open(page, caps(['plans']), { plans: S.TABS.plans, operator: S.TABS.operator });
  await openTab(page, 'plans');
  await expect(page.locator(T('plans-dag'))).toHaveCount(1);
  await expect(page.locator(T('operator-snapshot'))).toContainText('待办');
  await expect(page.locator(T('operator-compensate'))).toHaveCount(0);
});

test('@d-plans the operator compensation posts once, then shows the device hint', async ({ page }) => {
  const operator = S.clone(S.TABS.operator);
  const version = '0123456789abcdef0123456789abcdef';
  operator.compensation = { label: '准备退回补偿', compensate_version: version };
  const state = S.base({ capabilities: { tabs: ['plans'], compensate: true, plan_select: true } });
  await open(page, state, { plans: S.TABS.plans, operator });
  await openTab(page, 'plans');
  expect(await click(page, page.locator(T('operator-compensate')), '/api/chat/compensate')).toEqual({ plan_version: version });
});

test('@d-tabs leaving a tab drops its controls from the document', async ({ page }) => {
  await open(page, caps(['accounts', 'plans']), { accounts: S.TABS.accounts, plans: S.TABS.plans, operator: S.TABS.operator });
  await openTab(page, 'plans');
  await expect(page.locator(T('operator-snapshot'))).toHaveCount(1);
  await page.locator(T('tab-assistant')).click();
  await expect(page.locator(T('operator-snapshot'))).toHaveCount(0);
  await openTab(page, 'accounts');
  await expect(page.locator(T('accounts-balances'))).toHaveCount(1);
  await openTab(page, 'plans');
  await expect(page.locator(T('accounts-balances'))).toHaveCount(0);
});
