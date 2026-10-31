// The mobile sheet must leave the signed facts and gestures readable together.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');
const { T, SAY } = h;

async function inView(page, selectors) {
  return page.evaluate((selectors) => selectors.map((selector) => {
    const el = document.querySelector(selector);
    if (!el) return { selector, visible: false };
    const box = el.getBoundingClientRect();
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return { selector, visible: box.width > 0 && box.height > 0 && box.top >= 0 &&
      box.bottom <= innerHeight && box.left >= 0 && box.right <= innerWidth &&
      (hit === el || el.contains(hit)) };
  }), selectors);
}

async function noNestedScroll(page) {
  expect(await page.evaluate(() => {
    const pane = document.querySelector('[data-testid="device-pane"]');
    return [...pane.querySelectorAll('*')].filter((el) => {
      // The simulated inbox is its own document; the approval card has one scroll owner.
      return /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 1;
    }).map((el) => el.className);
  })).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
}

for (const size of [{ width: 390, height: 844 }, { width: 360, height: 740 }]) {
  for (const locale of ['zh-CN', 'en']) {
    test('mobile facts, SMS disclosure and MFA @scripted @mobile ' + `${size.width}x${size.height} ${locale}`, async ({ page }) => {
      await page.setViewportSize(size);
      await h.start(page, locale === 'en' ? { locale } : {});
      await h.send(page, SAY.yellow);
      const pane = page.locator(T('device-pane'));
      const sms = page.locator(T('sms-card'));
      await expect(sms).not.toHaveAttribute('open', '');
      await expect(sms.locator('summary')).toContainText(locale === 'en' ? 'Expand' : '展开');
      const facts = [
        `${T('device-pane')} ${T('card-amount')}`,
        `${T('device-pane')} ${T('card-fact')}[data-key="objects.contact_ref"]`,
        `${T('device-pane')} .card-tier`,
        `${T('device-pane')} ${T('device-confirm')}`,
        `${T('device-pane')} ${T('device-cancel')}`,
      ];
      await expect.poll(async () => (await inView(page, facts)).every((x) => x.visible)).toBe(true);
      await noNestedScroll(page);
      await h.screenshot(page, '27-mobile-device', `yellow-${size.width}-${locale}`, { testOnly: true });
      await sms.locator('summary').click();
      await expect(sms).toHaveAttribute('open', '');
      await expect(sms.locator('summary')).toContainText(locale === 'en' ? 'Collapse' : '收起');
      await sms.locator('summary').click();
      expect((await h.deviceConfirm(page)).ok).toBe(true);
      await page.keyboard.press('Escape');
      await h.send(page, SAY.red);
      expect((await h.deviceConfirm(page)).ok).toBe(true);
      await h.requestCode(page);
      await h.expectStep(page, 'mfa_code');
      await expect(sms).toHaveAttribute('open', '');
      const code = await h.readSmsCode(page);
      await noNestedScroll(page);
      // Only the outer sheet scrolls. Each control must be reachable and unobscured.
      for (const id of ['device-code-input', 'device-code-submit']) {
        await pane.locator(T(id)).scrollIntoViewIfNeeded();
        await expect.poll(async () => (await inView(page, [`${T('device-pane')} ${T(id)}`]))[0].visible).toBe(true);
      }
      await sms.locator('summary').click();
      await expect(sms).not.toHaveAttribute('open', '');
      // A rejected code refreshes the task but must preserve this manual collapse.
      await h.enterCode(page, code === '000000' ? '111111' : '000000');
      await h.expectStep(page, 'mfa_code');
      await expect(sms).not.toHaveAttribute('open', '');
      expect((await h.enterCode(page, code)).ok).toBe(true);
      await h.expectStep(page, 'final_confirm');
      await pane.locator(T('device-confirm')).scrollIntoViewIfNeeded();
      await expect.poll(async () => (await inView(page, [`${T('device-pane')} ${T('device-confirm')}`]))[0].visible).toBe(true);
      expect((await h.deviceConfirm(page)).ok).toBe(true);
    });
  }
}
