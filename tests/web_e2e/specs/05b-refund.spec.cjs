// 05b-refund (ext 1): after a confirmed birthday plan, "stop" then the task panel's refund
// prepares a compensation card that is confirmed on the device like any other card.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;
const BIRTHDAY = '帮我准备老婆的生日';

test.describe('05b-refund', () => {
  test('confirm the whole plan, stop, refund, confirm the compensation card @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    const caps = (await h.latestState(bodies)).capabilities;
    test.skip(!caps.plan_select || !caps.compensate, 'ext 1 is not in FEATURES yet');

    const rkey = await h.send(page, BIRTHDAY);
    const reply = h.responseOf(page, rkey);
    const choice = reply.locator(T('birthday-choice-0'));
    if (await choice.isVisible().catch(() => false)) {
      await h.clickAndWait(page, choice, '/birthday');
    }
    const total = (await h.responseState(bodies, rkey)).plan_select.total;
    for (let index = 0; index < total; index += 1) {
      await h.clickAndWait(page, reply.locator(T(`plan-node-${index}`)), '/nodes');
    }
    await h.expectStep(page, 'confirm_plan');
    expect((await h.deviceConfirm(page)).ok).toBe(true);

    await h.send(page, SAY.stop);
    // "Refund" is the task panel's compensation control (the chat word alone only asks back).
    const panel = (await h.latestState(bodies)).chat.task_panel;
    expect(panel && panel.controls.compensate).toBeTruthy();
    const refund = page.locator(T('task-panel')).getByRole('button', {
      name: panel.controls.compensate,
    });
    await h.clickAndWait(page, refund.first(), '/api/chat/compensate');
    const task = page.locator(`${T('device-pane')} ${T('device-task')}`).first();
    await expect(task).toHaveAttribute('data-source', /compensation|chat/);
    const step = await h.deviceStep(page);
    expect(['confirm', 'confirm_intent', 'mfa_request']).toContain(step);
    await h.screenshot(page, '05b-refund', 'compensation-card');
    if (step === 'mfa_request') await h.completeRed(page);
    else expect((await h.deviceConfirm(page)).ok).toBe(true);
    await expect(page.locator(`${T('device-pane')} ${T('device-task')}`).first()).toHaveAttribute(
      'data-step',
      'done',
    );
  });
});
