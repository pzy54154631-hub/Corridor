// Startup must recover visibly even before the message catalog is available.
const fs = require('fs');
const path = require('path');
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

for (const locale of ['zh-CN', 'en']) {
  test.describe(`29-session-retry ${locale}`, () => {
    test.use({ locale });

    for (const manual of [false, true]) {
      test(`${manual ? 'manual' : 'automatic'} recovery @scripted @public`, async ({ page }, info) => {
        let attempts = 0;
        await page.route('**/api/session', async (route) => {
          expect(route.request().method()).toBe('POST');
          attempts += 1;
          if (attempts === 1) {
            await route.fulfill({
              status: 503,
              headers: { 'Retry-After': manual ? '30' : '1' },
              contentType: 'application/json',
              body: JSON.stringify({ ok: false, error: { code: 'busy' } }),
            });
          } else await route.continue();
        });
        await page.goto('/');
        const notice = page.getByTestId('boot-retry-message');
        await expect(notice).toHaveText(h.catalog('web.ui.boot.retry', locale)
          .replace('{seconds}', manual ? '30' : '1'));
        await expect(page.locator('.spinner')).toHaveCount(0);
        await h.expectNoRawKeys(page);
        const retry = page.getByRole('button', { name: locale === 'en' ? 'Retry now' : '立即重试', exact: true });
        await expect(retry).toBeVisible();
        if (manual) {
          const file = path.join(process.env.CORRIDOR_E2E_OUT, 'screens', 'test-only',
            '29-session-retry', `${locale}-${h.MODE}-${info.project.name}.png`);
          fs.mkdirSync(path.dirname(file), { recursive: true });
          await page.screenshot({ path: file });
          await retry.click();
        }
        await expect(page.getByTestId('login-enter')).toBeVisible();
        await expect(notice).toHaveCount(0);
        expect(attempts).toBe(2);
        // A manual retry must cancel its old timer, even after the page has mounted.
        await page.clock.install();
        await page.clock.fastForward(61_000);
        expect(attempts).toBe(2);
      });
    }

    test('failed requests use a fallback delay and bounded backoff @scripted @public', async ({ page }) => {
      let attempts = 0;
      await page.route('**/api/session', async (route) => {
        attempts += 1;
        if (attempts === 1) await route.abort();
        else await route.fulfill({ status: 500, body: 'Unavailable' });
      });
      await page.clock.install();
      await page.goto('/');
      const notice = page.getByTestId('boot-retry-message');
      await expect(notice).toContainText(' 5 ');
      await h.expectNoRawKeys(page);
      await page.getByTestId('boot-retry-now').click();
      await expect(notice).toContainText(' 10 ');
      await expect(page.getByTestId('boot-retry-now')).toBeDisabled();
      for (const [wait, next] of [[10, 20], [20, 40], [40, 60], [60, 60]]) {
        const before = attempts;
        await page.clock.fastForward((wait - 1) * 1000);
        expect(attempts).toBe(before);
        await page.clock.fastForward(1000);
        await expect(notice).toContainText(` ${next} `);
        expect(attempts).toBe(before + 1);
      }
      await h.expectNoRawKeys(page);
    });
  });
}
