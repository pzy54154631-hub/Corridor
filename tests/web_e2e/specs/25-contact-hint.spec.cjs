// 25-contact-hint (RW-11): a transfer no contact matched says what the demo address book
// holds, with no card; the composer names the demo contacts, on a phone as well. The same
// page locally and in public mode.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T } = h;

const UNKNOWN = '给父亲转500元';

async function noSidewaysScroll(page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
}

async function hintShown(page) {
  await expect(page.locator(T('composer-contacts'))).toContainText('演示联系人');
  const rkey = await h.send(page, UNKNOWN);
  const reply = h.responseOf(page, rkey);
  const hint = reply.locator(T('contact-hint'));
  await expect(hint).toContainText('演示通讯录');
  await expect(hint.locator('li')).toHaveCount(6);
  await expect(hint.locator('li').first()).toHaveText('妈妈');
  await expect(hint).not.toContainText('父亲');
  await expect(hint.locator('button, input')).toHaveCount(0);
  await expect(reply.locator(T('card'))).toHaveCount(0);
  return reply;
}

test.describe('25-contact-hint', () => {
  test('an unknown recipient: the hint, no card, then a listed name gets its card @scripted @public', async ({
    page,
  }) => {
    await h.start(page);
    await hintShown(page);
    await h.screenshot(page, '25-contact-hint', 'hint');
    const rkey = await h.send(page, '给妈妈转500元');
    const reply = h.responseOf(page, rkey);
    await expect(reply.locator(T('card')).first()).toBeVisible();
    await expect(reply.locator(T('contact-hint'))).toHaveCount(0);
  });

  test('a listed contact in a sentence the parser skips: the given form gets its card @scripted @public', async ({
    page,
  }) => {
    await h.start(page);
    const first = h.responseOf(page, await h.send(page, '给妈妈和房东转500元'));
    const hint = first.locator(T('contact-hint'));
    await expect(hint).toContainText('这句话没有对上演示通讯录里的收款人');
    await expect(hint).toContainText('给妈妈转500元');
    await expect(hint).not.toContainText('通讯录里没有');
    await expect(first.locator(T('card'))).toHaveCount(0);
    const second = h.responseOf(page, await h.send(page, '给妈妈转500元'));
    await expect(second.locator(T('card')).first()).toBeVisible();
    await expect(second.locator(T('contact-hint'))).toHaveCount(0);
  });

  test('both stay inside a phone-sized page @scripted @public @mobile', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 740 });
    await h.start(page);
    await noSidewaysScroll(page);
    await hintShown(page);
    await noSidewaysScroll(page);
    const width = page.viewportSize().width;
    for (const id of ['contact-hint', 'composer-contacts']) {
      const box = await page.locator(T(id)).last().boundingBox();
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
    }
    const send = page.locator(T('composer-send'));
    await send.scrollIntoViewIfNeeded();
    await expect(send).toBeInViewport();
    await expect(page.locator(T('composer-contacts'))).toBeInViewport();
    await h.screenshot(page, '25-contact-hint', 'phone');
  });
});
