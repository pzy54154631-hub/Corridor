// 16-lockout (core, P0-15): three wrong codes lock the account; yellow and red steps show
// account_locked with no gesture, green queries still work, and "start over" recovers.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

function gestures(page) {
  return page.locator(
    `${T('device-pane')} ${T('device-confirm')}, ${T('device-pane')} ${T('device-mfa-request')}, ${T('device-pane')} ${T('device-code-input')}`,
  );
}

test.describe('16-lockout', () => {
  test('three wrong codes lock; banner and way out; green still works; start over recovers @scripted', async ({
    page,
    request,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page, { view: 'judge' });
    await h.send(page, SAY.red);
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await h.requestCode(page);
    const code = await h.readSmsCode(page);
    const wrong = code.slice(0, 5) + String((Number(code[5]) + 1) % 10);
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      const step = await h.deviceStep(page);
      if (step !== 'mfa_code') break;
      await h.enterCode(page, wrong);
    }
    await expect(page.locator(T('safety-banner'))).toHaveAttribute('data-locked', 'true');
    await h.expectStep(page, 'account_locked');
    await expect(gestures(page)).toHaveCount(0);
    await expect(page.locator(`${T('device-pane')} ${T('device-cancel')}`)).toBeVisible();
    const state = await h.latestState(bodies);
    expect(state.shell.safety.locked).toBe(true);
    expect(state.shell.safety.hint).toBeTruthy();
    expect(state.shell.safety.state).toBe('Locked');
    expect(state.device.tasks.every((task) => task.gesture === null)).toBe(true);
    await h.screenshot(page, '16-lockout', 'locked-judge');

    const yellow = await h.send(page, SAY.yellow);
    await expect(h.responseOf(page, yellow)).toBeVisible();
    await expect(gestures(page)).toHaveCount(0);
    const green = await h.send(page, SAY.balance);
    await expect(h.responseOf(page, green).locator(T('result-banner'))).toBeVisible();

    await h.resetSession(page);
    await h.login(page);
    await expect(page.locator(`${T('safety-banner')}[data-locked="true"]`)).toHaveCount(0);
    const fresh = await h.send(page, SAY.yellow);
    await h.expectStep(page, 'confirm');
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await expect(h.responseOf(page, fresh).locator(T('result-banner'))).toBeVisible();
    const counts = await h.probe(request);
    if (counts) expect(counts.device_complete_mfa).toBe(0);
  });
});
