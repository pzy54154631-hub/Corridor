// 25-contact-hint (RW-11): a transfer no contact matched says what the demo address book
// holds, with no card; the composer names the demo contacts, on a phone as well. The same
// page locally and in public mode.
const { test, expect } = require('playwright/test');
const fs = require('node:fs');
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
  for (const locale of ['zh-CN', 'en']) {
    test(`recommendations after a message stay single-line ${locale} @scripted @public`, async ({ page }) => {
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await h.start(page, { locale: locale === 'en' ? locale : undefined });
      await h.send(page, UNKNOWN);
      for (const [width, height] of [[1600, 1000], [1366, 768], [390, 844], [360, 740]]) {
        await test.step(`${width}x${height}`, async () => {
          await page.setViewportSize({ width, height });
          await page.evaluate(() => document.fonts.ready);
          const shortcuts = page.locator('.scr-shortcuts-compact');
          const title = shortcuts.getByTestId('try-these');
          await expect(title).toBeVisible();
          const heading = await title.evaluate(el => ({
            height: el.getBoundingClientRect().height,
            lineHeight: parseFloat(getComputedStyle(el).lineHeight),
          }));
          expect(heading.height).toBeGreaterThan(0);
          expect(heading.height).toBeLessThanOrEqual(heading.lineHeight + 0.5);
          const buttons = shortcuts.locator('.btn');
          expect(await buttons.count()).toBeGreaterThan(2);
          for (const button of await buttons.all()) {
            const box = await button.evaluate(el => ({
              height: el.getBoundingClientRect().height,
              fits: el.scrollWidth <= el.clientWidth,
              singleLine: getComputedStyle(el).whiteSpace === 'nowrap',
            }));
            expect(box.height).toBeGreaterThan(0);
            expect(box.height).toBeLessThanOrEqual(44);
            expect(box.fits && box.singleLine).toBe(true);
          }
          const row = shortcuts.locator('.scr-shortcut-row');
          await row.evaluate(el => { el.scrollLeft = el.scrollWidth; });
          expect(await row.evaluate(el => {
            const last = [...el.querySelectorAll('.btn')].at(-1).getBoundingClientRect();
            const bounds = el.getBoundingClientRect();
            return last.left >= bounds.left && last.right <= bounds.right - 32;
          })).toBe(true);
          await row.evaluate(el => { el.scrollLeft = 0; });
          await noSidewaysScroll(page);
          await page.getByTestId('composer-send').scrollIntoViewIfNeeded();
          await expect(title).toBeInViewport();
          await expect(buttons.first()).toBeInViewport();
          if (h.MODE === 'scripted' && (width === 1600 || width === 390)) {
            fs.mkdirSync('.tmp/c54', { recursive: true });
            await page.screenshot({ path: `.tmp/c54/dialogue-${width}x${height}-${locale}.png` });
          }
        });
      }
    });
  }

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
