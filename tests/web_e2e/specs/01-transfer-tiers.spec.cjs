// 01-transfer-tiers (core): green, yellow and red transfers through the device pane. The
// code is read only from the simulated phone (sms-frame) and never reaches the main page.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;
const notDigit = (code) => new RegExp(`(?<![0-9])${code}(?![0-9])`);

test.describe('01-transfer-tiers', () => {
  test('green query answers directly with a receipt @scripted', async ({ page }) => {
    await h.start(page);
    const rkey = await h.send(page, SAY.balance);
    const reply = h.responseOf(page, rkey);
    await expect(reply.locator(T('result-banner'))).toBeVisible();
    await expect(page.locator(`${T('device-pane')} ${T('device-confirm')}`)).toHaveCount(0);
    await expect(page.locator(`${T('device-pane')} ${T('device-mfa-request')}`)).toHaveCount(0);
    await h.screenshot(page, '01-transfer-tiers', 'green-receipt');
  });

  test('yellow transfer: one device confirmation, receipt and receipt line @scripted', async ({
    page,
    request,
  }) => {
    await h.start(page);
    const before = await h.probe(request);
    const rkey = await h.send(page, SAY.yellow);
    const reply = h.responseOf(page, rkey);
    await expect(reply.locator(T('card')).first()).toHaveAttribute('data-tier', 'yellow');
    await h.expectStep(page, 'confirm');
    await h.screenshot(page, '01-transfer-tiers', 'yellow-card');
    const done = await h.deviceConfirm(page);
    expect(done.ok).toBe(true);
    await expect(reply.locator(T('result-banner'))).toBeVisible();
    await expect(reply.locator(T('receipt-line'))).toBeVisible();
    await expect(page.locator(`${T('device-pane')} ${T('device-confirm')}`)).toHaveCount(0);
    const after = await h.probe(request);
    if (before && after) {
      expect(after.device_approve).toBeGreaterThan(before.device_approve);
      expect(after.device_complete_mfa).toBe(0);
    }
    await h.screenshot(page, '01-transfer-tiers', 'yellow-receipt');
  });

  test('red transfer: confirm, code from the SMS frame only, confirm again; the next red transfer needs a new code @scripted', async ({
    page,
    request,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    const rkey = await h.send(page, SAY.red);
    const reply = h.responseOf(page, rkey);
    await expect(reply.locator(T('card')).first()).toHaveAttribute('data-tier', 'red');
    await h.expectStep(page, 'confirm');
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await h.expectStep(page, 'mfa_request');
    await h.requestCode(page);
    await h.expectStep(page, 'mfa_code');
    const code = await h.readSmsCode(page);
    await expect(page.locator(T('sms-frame'))).toBeVisible();
    await h.screenshot(page, '01-transfer-tiers', 'red-code');

    const html = await page.evaluate(() => document.documentElement.outerHTML);
    expect(notDigit(code).test(html), 'code absent from the main document').toBe(false);

    const verify = await h.enterCode(page, code);
    expect(verify.ok).toBe(true);
    await h.expectStep(page, 'final_confirm');
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await expect(reply.locator(T('result-banner'))).toBeVisible();
    await expect(reply.locator(T('receipt-line'))).toBeVisible();
    await h.screenshot(page, '01-transfer-tiers', 'red-receipt');

    // INV-2.3: the page caches no verification; a second red card asks for a new code.
    const second = await h.send(page, SAY.red);
    await expect(h.responseOf(page, second).locator(T('card')).first()).toHaveAttribute(
      'data-tier',
      'red',
    );
    await h.expectStep(page, 'confirm');
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await h.expectStep(page, 'mfa_request');
    const again = await h.completeRed(page);
    expect(again).not.toBe(code);
    await expect(h.responseOf(page, second).locator(T('result-banner'))).toBeVisible();

    await h.latestState(bodies);
    for (const entry of bodies) {
      if (entry.path.startsWith('/api/phone/')) continue;
      for (const value of [code, again]) {
        expect(notDigit(value).test(entry.body || ''), `code in ${entry.path}`).toBe(false);
      }
    }
    const counts = await h.probe(request);
    if (counts) expect(counts.device_complete_mfa).toBe(0);
  });

  // R1 (S-2, V-1): after a yellow then a red transfer the code field was hidden under the
  // sticky action bar. Hit-test its centre as drawn, without the auto-scroll a click does.
  for (const size of [
    { width: 1920, height: 1080 },
    { width: 1440, height: 900 },
  ]) {
    test(`the code field is on top and in view at ${size.width}x${size.height} @scripted`, async ({ page }) => {
      await page.setViewportSize(size);
      await h.start(page);
      await h.send(page, SAY.yellow);
      expect((await h.deviceConfirm(page)).ok).toBe(true);
      await h.send(page, SAY.red);
      await h.expectStep(page, 'confirm');
      expect((await h.deviceConfirm(page)).ok).toBe(true);
      await h.requestCode(page);
      await h.expectStep(page, 'mfa_code');
      await page.waitForTimeout(400);
      const seen = await page.evaluate(() => {
        const pane = document.querySelector('[data-testid="device-pane"]');
        const holder = pane.querySelector('[data-testid="device-code-input"]');
        const input = holder.tagName === 'INPUT' ? holder : holder.querySelector('input');
        const screen = pane.querySelector('.device-screen') || pane;
        const box = input.getBoundingClientRect();
        const frame = screen.getBoundingClientRect();
        const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
        return {
          onTop: hit === input || input.contains(hit),
          inScreen: box.top >= frame.top - 1 && box.bottom <= frame.bottom + 1,
          inWindow: box.top >= 0 && box.bottom <= window.innerHeight,
          hit: hit ? hit.className || hit.tagName : null,
        };
      });
      expect(seen.onTop, `element at the field's centre: ${seen.hit}`).toBe(true);
      expect(seen.inScreen && seen.inWindow).toBe(true);
    });
  }
});
