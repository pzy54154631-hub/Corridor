// 06-wealth (ext 4): compare, purchase card with the five suitability lines, device
// confirmation (red adds the SMS), redemption, the isolated R4 refusal with no gesture, and a
// purchase that still confirms after "stop all" (O-S12).
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T } = h;

// Walk the device steps of one card to the end: intent, confirm, and the SMS step if red.
async function confirmOnDevice(page) {
  for (let round = 0; round < 4; round += 1) {
    const step = await h.deviceStep(page);
    if (step === 'mfa_request') {
      await h.completeRed(page);
      return;
    }
    if (!['confirm', 'confirm_intent', 'final_confirm'].includes(step)) {
      expect(round, `device step ${step}`).toBeGreaterThan(0);
      return;
    }
    expect((await h.deviceConfirm(page)).ok).toBe(true);
  }
}

async function purchase(page) {
  await page.locator(T('wealth-objective-net_return')).click();
  await h.clickAndWait(page, page.locator(T('wealth-compare')), '/api/wealth/compare');
  await expect(page.locator(T('wealth-product-0'))).toBeVisible();
  await h.clickAndWait(page, page.locator(T('wealth-purchase')), '/api/wealth/purchase');
}

test.describe('06-wealth', () => {
  test('compare, purchase, redeem, R4 refusal, purchase after stop all @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    const caps = (await h.latestState(bodies)).capabilities;
    test.skip(!caps.tabs.includes('wealth'), 'ext 4 is not in FEATURES yet');

    await page.locator(T('tab-wealth')).click();
    await expect(page.locator(T('wealth-scope-note'))).toBeVisible();
    await purchase(page);
    const card = page.locator(`${T('device-pane')} ${T('card')}`).first();
    await expect(card).toBeVisible();
    const note = page.locator(T('note-suitability')).first();
    await expect(note).toBeVisible();
    await h.screenshot(page, '06-wealth', 'purchase-card');
    await confirmOnDevice(page);
    await expect(page.locator(T('result-banner')).last()).toBeVisible();

    const redeem = page.locator(T('wealth-redeem-0'));
    if (await redeem.isVisible().catch(() => false)) {
      await h.clickAndWait(page, redeem, '/api/wealth/redeem');
      await confirmOnDevice(page);
    }

    await h.clickAndWait(page, page.locator(T('wealth-r4')), '/api/presets/suitability-r4');
    await expect(page.locator(T('refusal-card'))).toBeVisible();
    await expect(page.locator(T('refusal-card')).locator('button')).toHaveCount(0);
    const tasks = (await h.latestState(bodies)).device.tasks;
    expect(tasks.every((task) => task.gesture === null || task.source !== 'wealth')).toBe(true);
    await h.screenshot(page, '06-wealth', 'r4-refusal');

    await h.stopAll(page);
    await purchase(page);
    await confirmOnDevice(page);
    await expect(page.locator(T('result-banner')).last()).toBeVisible();
  });
});
