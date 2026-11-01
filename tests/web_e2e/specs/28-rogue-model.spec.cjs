const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');
const fs = require('node:fs');

for (const width of [360, 390, 1920]) {
  for (const locale of ['zh-CN', 'en']) {
    test(`28-rogue-model @scripted @public @mobile ${width} ${locale}`, async ({ page }) => {
      await page.setViewportSize({ width, height: width === 1920 ? 1080 : 844 });
      if (width === 360) await page.emulateMedia({ reducedMotion: 'reduce' });
      await page.goto('/');
      await expect(page.getByTestId('login-enter')).toBeVisible();
      if (locale === 'en') await h.setLocale(page, locale);
      const entry = page.getByTestId('rogue-entry');
      let requests = 0;
      page.on('request', r => { if (r.url().endsWith('/api/rogue')) requests++; });
      const response = page.waitForResponse((r) => r.url().endsWith('/api/rogue'));
      await entry.click();
      const report = (await (await response).json()).rogue;
      const started = Date.now();
      if (width !== 360) {
        await expect(page.getByTestId('rogue-stat-blocked')).toHaveText(locale === 'en' ? 'Checking' : '检查中');
        await expect(page.getByTestId('rogue-row').first()).toContainText(locale === 'en' ? 'Model launching attack' : '模型发起攻击');
      }
      await expect(page.getByTestId('rogue-row')).toHaveCount(8, { timeout: 15000 });
      await expect(page.getByTestId('rogue-summary')).toContainText(`${report.summary.blocked}/8`);
      expect(Date.now() - started).toBeLessThan(7000);
      await expect(page.getByTestId('rogue-stat-blocked')).toHaveText(`${report.summary.blocked}/${report.summary.attacks}`);
      await expect(page.getByTestId('rogue-stat-dispatches')).toHaveText(String(report.summary.attack_dispatches));
      await expect(page.getByTestId('rogue-stat-controls')).toHaveText(`${report.summary.benign_completed}/${report.summary.attacks}`);
      expect(await page.locator('.rogue-rows details[open]').count()).toBe(0);
      await page.locator('.rogue-rows summary').first().click();
      await expect(page.locator('.rogue-rows details').first()).toContainText(report.rows[0].expected_rule);
      await page.locator('.rogue-rows summary').first().click();
      await expect(page.getByTestId('rogue-limit')).toContainText('corridor adversary');
      await expect(page.getByTestId('rogue-row').last()).toContainText(locale === 'en' ? 'Normal operation: completed' : '正常操作：已完成');
      expect(await page.getByTestId('rogue-panel').evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
      const box = await page.getByTestId('rogue-panel').boundingBox();
      expect(box).toEqual({ x: 0, y: 0, width, height: width === 1920 ? 1080 : 844 });
      await page.getByTestId('rogue-close').focus();
      await page.keyboard.press('Shift+Tab');
      await expect(page.getByTestId('rogue-replay')).toBeFocused();
      await page.keyboard.press('Tab');
      await expect(page.getByTestId('rogue-close')).toBeFocused();
      await page.getByTestId('rogue-panel').evaluate(el => { el.scrollTop = 0; });
      if (width === 1920 && locale === 'zh-CN') {
        expect(await page.getByTestId('rogue-replay').evaluate(el => el.getBoundingClientRect().bottom <= innerHeight)).toBe(true);
      }
      fs.mkdirSync('.tmp/c39', { recursive: true });
      await page.screenshot({ path: `.tmp/c39/rogue-${width}-${locale}.png` });
      if (width === 1920 && locale === 'zh-CN' && process.env.CORRIDOR_E2E_MODE === 'scripted') {
        await page.screenshot({ path: 'docs/submission-zh/screenshots/13-rogue-model.png' });
      }
      await page.getByTestId('rogue-replay').click();
      await expect(page.getByTestId('rogue-summary')).toContainText(`${report.summary.blocked}/8`);
      expect(requests).toBe(1);
      await page.keyboard.press('Escape');
      await expect(page.getByTestId('rogue-panel')).toHaveCount(0);
      await expect(entry).toBeFocused();
      await entry.click();
      await page.getByTestId('rogue-close').click();
      await expect(page.getByTestId('rogue-panel')).toHaveCount(0);
      await expect(entry).toBeFocused();
      await page.getByTestId('login-enter').click();
      await expect(page.getByTestId('rogue-entry')).toBeVisible();
      await page.getByTestId('rogue-entry').click();
      await expect(page.getByTestId('rogue-panel')).toBeVisible();
    });
  }
}

test('28-rogue-model failure is visible @scripted @public', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  // Browser fetch uses the public harness's TLS host mapping; APIRequestContext does not.
  const body = await page.evaluate(async () => (await fetch('/api/rogue')).json());
  Object.assign(body.rogue.rows[0], { passed: false, attack_dispatches: 1, benign_completed: false,
    oracle_violation: true, prohibited_effects: 1 });
  Object.assign(body.rogue.summary, { blocked: 7, attack_dispatches: 1, benign_completed: 7,
    violations: 1, prohibited_effects: 1 });
  await page.route('**/api/rogue', async route => {
    await route.fulfill({ json: body });
  });
  await page.getByTestId('rogue-entry').click();
  await expect(page.getByTestId('rogue-stat-blocked')).toHaveText('7/8');
  await expect(page.getByTestId('rogue-stat-dispatches')).toHaveText('1');
  await expect(page.getByTestId('rogue-stat-controls')).toHaveText('7/8');
  await expect(page.getByTestId('rogue-row').first()).toHaveClass(/rogue-failed/);
  await expect(page.getByTestId('rogue-row').first()).toContainText('检查未通过');
  await expect(page.getByTestId('rogue-row').first()).toContainText('正常操作：未完成');
});
