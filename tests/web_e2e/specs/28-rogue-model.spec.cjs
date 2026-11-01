const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');
const fs = require('node:fs');

async function expectFirstScreen(page, locator, { exactHit = false } = {}) {
  await expect(locator).toBeVisible();
  await expect.poll(() => locator.evaluate((el, exact) => {
    const r = el.getBoundingClientRect();
    const points = [[r.x + r.width / 2, r.y + r.height / 2],
      [r.left + 6, r.top + 6], [r.right - 6, r.bottom - 6]];
    return r.width > 0 && r.height > 0 && r.left >= 0 && r.top >= 0 &&
      r.right <= innerWidth && r.bottom <= innerHeight && points.every(([x, y]) => {
        const hit = document.elementFromPoint(x, y);
        return exact ? hit === el : hit === el || el.contains(hit);
      });
  }, exactHit), { message: `${await locator.getAttribute('data-testid') || await locator.getAttribute('class')}: fully inside the first screen and unobscured` }).toBe(true);
}

for (const locale of ['zh-CN', 'en']) {
  test(`28-rogue-model first screen ${locale} @scripted @public`, async ({ page }) => {
    test.setTimeout(180_000);
    if (h.MODE === 'public') {
      // Pace this dense matrix below the real two-API-requests/second public limit.
      // Requests and responses still use the unmodified public server.
      let nextRequest = 0;
      await page.route('**/api/**', async route => {
        const at = Math.max(Date.now(), nextRequest);
        nextRequest = at + 550;
        const delay = at - Date.now();
        if (delay > 0) await new Promise(resolve => setTimeout(resolve, delay));
        await route.continue();
      });
    }
    // Reuse a browser session across viewport checks: public mode caps sessions per address.
    for (const [width, height] of [[1366, 768], [1440, 900], [1600, 1000], [1024, 768],
      [768, 1024], [390, 844], [360, 740]]) await test.step(`${width}x${height}`, async () => {
      await page.setViewportSize({ width, height });
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await h.openApp(page);
      if (await page.locator('html').getAttribute('lang') !== locale) await h.setLocale(page, locale);
      await page.evaluate(() => document.fonts.ready);
      for (const loggedIn of [false, true]) {
        if (loggedIn) {
          await expect(page.getByTestId('login-enter')).toBeVisible();
          await expect(page.getByTestId('login-enter')).toBeEnabled();
          const response = await h.clickAndWait(page, page.getByTestId('login-enter'), '/api/session/login');
          expect((await response.json()).ok).toBe(true);
          await expect(page.getByTestId('composer-input')).toBeVisible();
          // Login may be below the fold. Reopen the logged-in site to test its first screen.
          await h.openApp(page);
          await expect(page.getByTestId('composer-input')).toBeVisible();
        }
        for (const view of ['customer', 'judge']) {
          if (view === 'judge') await h.setView(page, view);
          await expect(page.getByTestId(`view-${view}`)).toHaveAttribute('aria-pressed', 'true');
          expect(await page.evaluate(() => scrollY), `${width}x${height} ${locale} ${view} loggedIn=${loggedIn}`).toBe(0);
          expect(await page.locator('.screen:visible').evaluateAll(els => els.length > 0 && els.every(el => el.scrollTop === 0))).toBe(true);
          const entry = page.getByTestId('rogue-header-entry');
          await expect(entry).toHaveText(locale === 'en' ? 'Compromised-Model Drill' : '模型失陷演练');
          await expect(entry).toHaveClass(/btn-primary/);
          await expectFirstScreen(page, entry, { exactHit: true });
          if (width <= 767) {
            expect(await entry.evaluate(el => {
              const range = document.createRange();
              range.selectNodeContents(el);
              return range.getClientRects().length === 1 &&
                el.scrollWidth <= el.clientWidth && el.getBoundingClientRect().height === 40;
            })).toBe(true);
            const groups = page.locator('.header-controls .segmented');
            expect(await groups.evaluateAll(els => new Set(els.map(el => el.getBoundingClientRect().top)).size)).toBe(1);
          }
          if (!loggedIn && width === 1600) {
            expect(await page.locator('.scr-login-hero').evaluate(el =>
              el.getBoundingClientRect().top - document.querySelector('.app-header').getBoundingClientRect().bottom)).toBeLessThanOrEqual(20);
          }
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
          for (const control of await page.locator('.stage-header button, .stage-header .planner-badge, .stage-header .brand').all()) {
            if (await control.isVisible()) await expectFirstScreen(page, control);
          }
          if (!loggedIn && ((width === 1600 && height === 1000) || (width === 390 && height === 844))) {
            await expectFirstScreen(page, page.getByTestId('rogue-entry'));
            await expectFirstScreen(page, page.getByTestId('login-enter'));
            expect(await page.getByTestId('rogue-entry').evaluate(el =>
              el.getBoundingClientRect().bottom <= document.querySelector('[data-testid="login-facts"]').getBoundingClientRect().top)).toBe(true);
            fs.mkdirSync('.tmp/c42', { recursive: true });
            await page.screenshot({ path: `.tmp/c42/first-screen-${h.MODE}-${width}-${locale}-${view}.png` });
          }
          if (width <= 767) {
            await expect(page.getByTestId('corner-notice')).toBeHidden();
            await expect(page.locator('.status-label')).toContainText(locale === 'en' ? 'synthetic' : '合成');
            if (h.MODE === 'public') await expect(page.getByTestId('public-banner')).toContainText(locale === 'en' ? 'synthetic' : '合成');
          } else {
            await expect(page.getByTestId('corner-notice')).toBeVisible();
            await expect(page.getByTestId('device-idle-title')).toHaveText(locale === 'en' ? 'Simulated device' : '模拟设备');
            await expect(page.locator('.device-idle-steps li')).toHaveCount(3);
          }
          if (!loggedIn && locale === 'zh-CN' && view === 'customer' && [360, 390, 1600].includes(width)) {
            fs.mkdirSync('.tmp/c43', { recursive: true });
            await page.screenshot({ path: `.tmp/c43/${h.MODE}-first-${width}x${height}.png` });
          }
          if (locale === 'zh-CN' && view === 'customer' &&
              ((!loggedIn && [1600, 1366, 390].includes(width)) || (loggedIn && width === 1600))) {
            fs.mkdirSync('.tmp/c42', { recursive: true });
            if (loggedIn) await expect(page.frameLocator('[data-testid="sms-frame"]').locator('body')).toContainText(/合成|synthetic/);
            await page.screenshot({ path: `.tmp/c42/${h.MODE}-${loggedIn ? 'bank' : 'first'}-${width}x${height}.png` });
            if (!loggedIn && width === 1600) {
              // Element screenshots scroll tall devices into view, invalidating the
              // next first-screen check. A page clip captures without scrolling.
              await page.screenshot({ path: `.tmp/c42/${h.MODE}-device-idle.png`,
                clip: await page.locator('.device-idle').boundingBox() });
            }
          }
          await entry.click();
          await expect(page.getByTestId('rogue-panel')).toBeVisible();
          await expect(page.getByTestId('rogue-summary')).toContainText(/\d\/8/);
          await page.getByTestId('rogue-close').click();
          await expect(page.getByTestId('rogue-panel')).toHaveCount(0);
          await expect(entry).toBeFocused();
        }
        await h.setView(page, 'customer');
        await expect(page.getByTestId('view-customer')).toHaveAttribute('aria-pressed', 'true');
      }
      await h.resetSession(page);
      await expect(page.getByTestId('login-enter')).toBeVisible();
    });
  });
}

test('28-rogue-model header refresh restores focus @scripted @public', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await h.openApp(page);
  const entry = page.getByTestId('rogue-header-entry');
  await entry.click();
  await expect(page.getByTestId('rogue-summary')).toContainText(/\d\/8/);
  // A background state refresh replaces the header while the modal owns focus.
  expect(await page.evaluate(async () => {
    const origin = document.querySelector('[data-testid="rogue-header-entry"]');
    const { store } = await import('/static/js/store.js');
    store.set({});
    await Promise.resolve();
    return !origin.isConnected;
  })).toBe(true);
  await page.getByTestId('rogue-close').click();
  await expect(page.getByTestId('rogue-panel')).toHaveCount(0);
  await expect(entry).toBeFocused();
});

test('28-rogue-model idle device opens existing panel @scripted @public', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await h.openApp(page);
  const entry = page.getByTestId('rogue-device-entry');
  await expect(entry).toHaveClass(/btn-secondary/);
  await entry.click();
  await expect(page.getByTestId('rogue-summary')).toContainText('8/8');
  await page.getByTestId('rogue-close').click();
  await expect(entry).toBeFocused();
});

for (const width of [360, 390, 1600, 1920]) {
  for (const locale of ['zh-CN', 'en']) {
    test(`28-rogue-model @scripted @public @mobile ${width} ${locale}`, async ({ page }) => {
      await page.setViewportSize({ width, height: width === 1920 ? 1080 : width === 1600 ? 1000 : 844 });
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
        await expect(page.getByTestId('rogue-stat-blocked')).toHaveText(locale === 'en' ? 'Calculating' : '计算中');
        await expect(page.getByTestId('rogue-row').first()).toBeVisible();
        await expect(page.locator('.rogue-table caption')).toContainText(locale === 'en' ? 'Table 1.' : '表 1');
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
      await expect(page.getByTestId('rogue-limit')).toHaveText(locale === 'en'
        ? 'The listed attacks are handwritten synthetic examples, with no model calls. The results describe the authorization kernel’s behavior in these cases only and do not establish protection against all attacks; a manipulated payment confirmed by the user may still execute. Run corridor adversary to reproduce all 40 cases.'
        : '所列攻击均为手写合成样例，未调用模型。结果仅说明授权内核在上述情形下的行为，不代表可阻止全部攻击；用户本人确认的诱导付款仍可能执行。完整 40 例可运行 corridor adversary 复现。');
      await expect(page.getByTestId('rogue-row').last()).toContainText(locale === 'en' ? 'Completed' : '已完成');
      expect(await page.getByTestId('rogue-panel').evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
      const box = await page.getByTestId('rogue-panel').boundingBox();
      expect(box).toEqual({ x: 0, y: 0, width, height: width === 1920 ? 1080 : width === 1600 ? 1000 : 844 });
      await page.getByTestId('rogue-close').focus();
      await page.keyboard.press('Shift+Tab');
      await expect(page.getByTestId('rogue-replay')).toBeFocused();
      await page.keyboard.press('Tab');
      await expect(page.getByTestId('rogue-close')).toBeFocused();
      await page.getByTestId('rogue-panel').evaluate(el => { el.scrollTop = 0; });
      if (width === 1920 && locale === 'zh-CN') {
        expect(await page.getByTestId('rogue-replay').evaluate(el => el.getBoundingClientRect().bottom <= innerHeight)).toBe(true);
      }
      fs.mkdirSync('.tmp/c42', { recursive: true });
      await page.screenshot({ path: `.tmp/c42/rogue-${width}-${locale}.png` });
      if (width === 1920 && locale === 'zh-CN' && process.env.CORRIDOR_E2E_MODE === 'scripted' && process.env.CORRIDOR_CAPTURE_ROGUE === '1') {
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
  await expect(page.getByTestId('rogue-row').first()).toContainText('未完成');
});
