// 24-raw-keys: no rendered page shows a catalog key in place of its text (RW-9 item 3). The
// first screen did: the login card's six strings were missing and t() fell back to the key.
// The same journeys run in the scripted lane and, over TLS on the public name, the public lane.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;
const VIEWS = [
  ['customer', 'zh-CN'],
  ['customer', 'en'],
  ['judge', 'zh-CN'],
  ['judge', 'en'],
];

// Behind the proxy each visitor has an address of its own; the terminator is a plain pipe, so
// the browser sends what Caddy would. One address would soon drain the page bucket.
let visitors = 0;

test.beforeEach(async ({ context }) => {
  if (process.env.CORRIDOR_E2E_MODE !== 'public') return;
  visitors += 1;
  await context.setExtraHTTPHeaders({ 'X-Forwarded-For': `203.0.113.${visitors}` });
});

async function firstScreen(page, view, locale) {
  await h.openApp(page, { view, locale: locale === 'en' ? locale : undefined });
  const facts = page.locator(`${T('login-facts')} li`);
  await expect(facts).toHaveCount(3);
  for (const text of await facts.allInnerTexts()) expect(text.trim().length).toBeGreaterThan(8);
  await h.expectNoRawKeys(page);
}

async function phonePage(page, bodies) {
  const { session } = await h.latestState(bodies);
  const phone = await page.context().newPage();
  await phone.goto(session.phone_url);
  await expect(phone.locator(T('sms-corner'))).toBeVisible();
  await h.expectNoRawKeys(phone);
  return phone;
}

async function throughTheTabs(page) {
  for (const name of ['accounts', 'wealth', 'plans', 'assistant']) {
    await page.locator(`#tab-${name}`).click();
    await h.expectNoRawKeys(page);
  }
}

test.describe('24-raw-keys', () => {
  for (const [view, locale] of VIEWS) {
    test.describe(`${view} ${locale}`, () => {
      test('the first screen shows no key @scripted @public', async ({ page }) => {
        await firstScreen(page, view, locale);
      });
    });
  }

  test('the first screen shows no key on a phone-sized page @scripted @public @mobile', async ({
    page,
  }) => {
    await firstScreen(page, 'customer', 'zh-CN');
    await h.screenshot(page, '24-raw-keys', 'first-screen');
  });

  test('the phone page shows no key in either language @scripted @public', async ({ page }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    const phone = await phonePage(page, bodies);
    await phone.close();
    await h.setLocale(page, 'en');
    await (await phonePage(page, bodies)).close();
  });

  test('the phone page shows no key at phone size @scripted @public @mobile', async ({ page }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    await (await phonePage(page, bodies)).close();
  });

  test('a red transfer and the tabs show no key @scripted @public', async ({ page }) => {
    await h.start(page, { view: 'judge' });
    await h.expectNoRawKeys(page);
    await h.send(page, SAY.yellow);
    await h.expectStep(page, 'confirm');
    await h.deviceConfirm(page);
    await h.expectNoRawKeys(page);
    await h.send(page, SAY.red);
    await h.expectStep(page, 'confirm');
    await h.expectNoRawKeys(page);
    await h.deviceConfirm(page);
    await h.completeRed(page);
    await h.expectNoRawKeys(page);
    await throughTheTabs(page);
    await h.setLocale(page, 'en');
    await throughTheTabs(page);
  });

  test('the notice is on the first screen and the phone page @public', async ({ page }) => {
    const bodies = h.collectApiBodies(page);
    await h.openApp(page);
    const notice = page.locator(T('public-banner')).first();
    await expect(notice).toBeVisible();
    await expect(notice).toContainText('演示环境：合成数据、模拟认证，请勿输入真实信息');
    const box = await notice.boundingBox();
    expect(box.y).toBeLessThan(5);
    const details = page.locator(T('public-details')).first();
    await details.locator('summary').click();
    await expect(details).toContainText('整张合成通讯录');
    await details.locator('summary').click();
    await h.screenshot(page, '24-raw-keys', 'public-first-screen');
    await h.login(page);
    const phone = await phonePage(page, bodies);
    await expect(phone.locator(T('public-banner'))).toBeVisible();
  });

  test('the notice leaves the login button in reach on a phone @public @mobile', async ({
    page,
  }) => {
    await h.openApp(page);
    await expect(page.locator(T('public-banner')).first()).toBeVisible();
    await page.locator(T('login-enter')).scrollIntoViewIfNeeded();
    await expect(page.locator(T('login-enter'))).toBeVisible();
    await h.screenshot(page, '24-raw-keys', 'public-first-screen', { fullPage: true });
  });
});
