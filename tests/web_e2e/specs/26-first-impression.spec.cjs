// C26: every first-screen recommendation is reachable in both interface languages.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');
const { T } = h;

for (const locale of ['zh-CN', 'en']) {
  for (const key of ['transfer', 'billing', 'wealth', 'cards', 'subscriptions', 'birthday']) {
    test('26-first-impression @scripted ' + `${locale} ${key}`, async ({ page }) => {
      const errors = [];
      page.on('pageerror', (error) => errors.push(error.message));
      await h.start(page);
      if (locale === 'en') await h.setLocale(page, locale);
      await expect(page.locator(T('try-these'))).toHaveText(locale === 'en' ? 'Try these' : '推荐试试');
      for (const name of ['transfer', 'billing', 'wealth', 'cards', 'subscriptions', 'birthday']) {
        await expect(page.locator(T(`shortcut-${name}`))).toBeVisible();
      }
      if (key === 'wealth') {
        await page.locator(T('shortcut-wealth')).click();
        await expect(page.locator(T('wealth-scope-note'))).toBeVisible();
        await expect(page.locator(T('tab-wealth'))).toHaveAttribute('aria-selected', 'true');
      } else {
        const response = await h.clickAndWait(page, page.locator(T(`shortcut-${key}`)), '/api/chat/shortcut');
        const body = await response.json();
        expect(body.ok).toBe(true);
        await expect(h.responseOf(page, body.rkey)).toBeVisible();
        expect(body.state.chat.responses[body.rkey].status).not.toBe('clarify');
      }
      expect(errors).toEqual([]);
    });
  }
}
