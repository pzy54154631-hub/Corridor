// 15-responsive: 1024 px two columns with the evidence drawer; 1920x1080 at 200 % zoom
// (a 960x540 CSS viewport) stays usable; 390x844 is best effort (design 4.1).
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

async function noHorizontalScroll(page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
}

// The sheet opens by itself for a new task (it never acts); otherwise "open device".
async function openDevice(page) {
  const confirm = page.locator(`${T('device-pane')} ${T('device-confirm')}`);
  const shown = await expect(confirm)
    .toBeVisible({ timeout: 3_000 })
    .then(() => true)
    .catch(() => false);
  if (!shown) await page.locator(T('device-open')).first().click();
  await confirm.scrollIntoViewIfNeeded();
}

test.describe('15-responsive', () => {
  test('1024 px: phone and device side by side; evidence is a drawer @scripted', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await h.start(page, { view: 'judge' });
    const phone = await page.locator(T('phone-pane')).boundingBox();
    const device = await page.locator(T('device-pane')).boundingBox();
    expect(device.x).toBeGreaterThanOrEqual(phone.x + phone.width - 1);
    const evidence = page.locator(T('evidence-pane'));
    const toggle = page.locator(T('evidence-toggle'));
    await expect(toggle).toBeVisible();
    const shown = await evidence.isVisible();
    await toggle.click();
    await expect(evidence).toBeVisible({ visible: !shown });
    await toggle.click();
    await expect(evidence).toBeVisible({ visible: shown });
    await noHorizontalScroll(page);
    await h.screenshot(page, '15-responsive', 'w1024-judge');
  });

  test('1920x1080 at 200 % zoom: single column, device sheet opens, no sideways scroll @scripted', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 960, height: 540 });
    await h.start(page);
    await noHorizontalScroll(page);
    await h.send(page, SAY.yellow);
    const confirm = page.locator(`${T('device-pane')} ${T('device-confirm')}`);
    await openDevice(page);
    await expect(confirm).toBeVisible();
    await h.assertCornerOnTop(page);
    await h.screenshot(page, '15-responsive', 'zoom200-device');
    expect((await h.deviceConfirm(page)).ok).toBe(true);
  });

  test('390x844 (best effort): single column, device sheet opens and Esc closes it @scripted @mobile', async ({
    page,
  }) => {
    await h.start(page);
    await noHorizontalScroll(page);
    await h.send(page, SAY.yellow);
    const pane = page.locator(T('device-pane'));
    await openDevice(page);
    await expect(page.locator(`${T('device-pane')} ${T('device-confirm')}`)).toBeVisible();
    await h.assertCornerOnTop(page);
    await h.screenshot(page, '15-responsive', 'w390-device', { testOnly: true });
    await page.keyboard.press('Escape');
    await expect(page.locator(`${T('device-pane')} ${T('device-confirm')}`)).toBeHidden();
    await expect(pane).toHaveCount(1);
  });
});
