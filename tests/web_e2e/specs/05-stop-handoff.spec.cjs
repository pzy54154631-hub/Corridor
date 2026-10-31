// 05-stop-handoff (core): cancel is a global withdrawal written into the chat, the pending
// SMS expires, "stop" in chat works, hand-off shows the redacted operator snapshot, and
// "stop all" answers in receipt terms (P0-7).
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

function gestures(page) {
  return page.locator(
    `${T('device-pane')} ${T('device-confirm')}, ${T('device-pane')} ${T('device-mfa-request')}, ${T('device-pane')} ${T('device-code-input')}`,
  );
}

function anyPost(page) {
  return page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      /\/api\/(chat\/stop|stop|device\/tasks\/[^/]+\/cancel)$/.test(new URL(response.url()).pathname),
  );
}

async function handoff(page, bodies) {
  const direct = page.locator(T('handoff'));
  if ((await direct.count()) > 0) {
    await h.clickAndWait(page, direct.first(), '/api/chat/handoff');
    return;
  }
  const panel = (await h.latestState(bodies)).chat.task_panel;
  if (panel) {
    const button = page.locator(T('task-panel')).getByRole('button', { name: panel.controls.handoff });
    await h.clickAndWait(page, button, '/api/chat/handoff');
    return;
  }
  await h.send(page, SAY.handoff);
}

test.describe('05-stop-handoff', () => {
  test('cancel on the card: stop receipt in the chat, the SMS expires, no gesture left @scripted', async ({
    page,
  }) => {
    await h.start(page);
    const rkey = await h.send(page, SAY.red);
    const reply = h.responseOf(page, rkey);
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await h.requestCode(page);
    await h.readSmsCode(page);
    const turns = page.locator('[data-testid^="turn-"]');
    const before = await turns.count();

    // The marker turn ("tapped Cancel") and the stop receipt are appended to the chat.
    await Promise.all([anyPost(page), reply.locator(T('cancel-chat')).click()]);
    await expect(turns).toHaveCount(before + 2);
    await expect(page.locator(T('turn-user')).last()).toBeVisible();
    await expect(turns.last()).toHaveAttribute('data-testid', /turn-(assistant|system)/);
    await expect(gestures(page)).toHaveCount(0);
    const frame = page.frameLocator(T('sms-frame'));
    await expect(frame.locator(`${T('sms-message')}[data-status="active"]`)).toHaveCount(0, {
      timeout: 15_000,
    });
    await expect(frame.locator(`${T('sms-message')}[data-status="expired"]`).first()).toBeVisible();
    await h.screenshot(page, '05-stop-handoff', 'cancelled');
  });

  test('saying "stop" withdraws the pending card @scripted', async ({ page }) => {
    await h.start(page);
    await h.send(page, SAY.yellow);
    await h.expectStep(page, 'confirm');
    await h.send(page, SAY.stop);
    await expect(gestures(page)).toHaveCount(0);
  });

  test('hand-off pauses automation and shows the redacted operator snapshot @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page, { view: 'judge' });
    await h.send(page, SAY.yellow);
    await handoff(page, bodies);
    await expect(gestures(page)).toHaveCount(0);
    const snapshot = page.locator(T('operator-snapshot'));
    if (!(await snapshot.isVisible().catch(() => false))) {
      const open = page.locator(`${T('operator-open')}, ${T('route-hint')}[data-target="operator"]`);
      await open.first().click();
    }
    await expect(snapshot).toBeVisible();
    await h.latestState(bodies);
    const tab = bodies.find((entry) => entry.path === '/api/tabs/operator');
    if (tab) expect(JSON.parse(tab.body).tab.vm.snapshot).toBeTruthy();
    await h.screenshot(page, '05-stop-handoff', 'operator-judge');
  });

  test('"stop all" stops pending steps and answers in receipt terms @scripted', async ({
    page,
  }) => {
    await h.start(page);
    await h.send(page, SAY.yellow);
    await h.expectStep(page, 'confirm');
    await h.stopAll(page);
    await expect(gestures(page)).toHaveCount(0);
    await expect(page.locator(T('toast')).first()).toBeVisible();
    await expect(page.locator(T('turn-system')).last()).toBeVisible();
    await h.screenshot(page, '05-stop-handoff', 'stop-all');
  });
});
