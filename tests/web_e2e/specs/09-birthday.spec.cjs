// 09-birthday (ext 1): node selection is registered by the server; a non-prefix selection
// shows the rule and no confirm key; a true prefix lists only the chosen cards and signs
// nothing in that step; a full selection confirms and updates the task graph (INV-2.9).
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T } = h;
const BIRTHDAY = '帮我准备老婆的生日';

async function capabilityOrSkip(page, bodies) {
  const caps = (await h.latestState(bodies)).capabilities;
  test.skip(!caps.plan_select, 'plan_select is not in FEATURES yet (ext 1)');
}

// Opens the birthday plan and returns the reply that carries the node selection.
async function openPlan(page, bodies) {
  const rkey = await h.send(page, BIRTHDAY);
  const reply = h.responseOf(page, rkey);
  const choice = reply.locator(T('birthday-choice-0'));
  if (await choice.isVisible().catch(() => false)) {
    await h.clickAndWait(page, choice, '/birthday');
  }
  await expect(reply.locator(T('plan-node-0'))).toBeVisible();
  return { rkey, reply };
}

async function toggle(page, reply, index) {
  await h.clickAndWait(page, reply.locator(T(`plan-node-${index}`)), '/nodes');
}

function deviceCards(page) {
  return page.locator(`${T('device-pane')} ${T('card')}[data-owner="device"]`);
}

test.describe('09-birthday', () => {
  test('non-prefix: rule shown, no confirm; prefix: only the chosen cards, nothing signed; full: confirm @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    await capabilityOrSkip(page, bodies);
    const { rkey, reply } = await openPlan(page, bodies);
    const total = (await h.responseState(bodies, rkey)).plan_select.total;
    expect(total).toBeGreaterThan(1);
    await h.expectStep(page, 'select_nodes');
    await h.screenshot(page, '09-birthday', 'plan-empty');

    await toggle(page, reply, 1);
    await expect(reply.locator(T('plan-node-1'))).toHaveAttribute('data-checked', 'true');
    await expect(reply.locator(T('plan-error'))).toBeVisible();
    await h.expectStep(page, 'select_nodes');
    await expect(page.locator(`${T('device-pane')} ${T('device-confirm')}`)).toHaveCount(0);
    await expect(page.locator(`${T('device-pane')} ${T('device-subset')}`)).toHaveCount(0);

    await toggle(page, reply, 0);
    await h.expectStep(page, 'confirm_subset');
    await expect(page.locator(T('device-selection'))).toBeVisible();
    await expect(deviceCards(page)).toHaveCount(2);
    await expect(page.locator(`${T('device-pane')} ${T('device-subset')}`)).toBeVisible();
    await h.screenshot(page, '09-birthday', 'prefix-subset');

    for (let index = 2; index < total; index += 1) await toggle(page, reply, index);
    await h.expectStep(page, 'confirm_plan');
    await expect(deviceCards(page)).toHaveCount(total);
    const settled = await h.deviceConfirm(page);
    expect(settled.ok).toBe(true);
    await h.expectStep(page, 'done');
    await expect(page.locator(T('task-panel'))).toBeVisible();
    const plans = page.locator(T('tab-plans'));
    if (await plans.isVisible()) {
      await plans.click();
      await expect(page.locator(T('plans-dag')).first()).toBeVisible();
    }
    await h.screenshot(page, '09-birthday', 'plan-confirmed');
  });

  test('a true prefix prepares a new plan card for the chosen nodes only @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    await capabilityOrSkip(page, bodies);
    const { reply } = await openPlan(page, bodies);
    await toggle(page, reply, 0);
    await h.expectStep(page, 'confirm_subset');
    const before = (await h.latestState(bodies)).device.tasks[0];
    await h.clickAndWait(page, page.locator(`${T('device-pane')} ${T('device-subset')}`), '/confirm');
    const after = (await h.latestState(bodies)).device.tasks[0];
    expect(after.cards.length).toBeLessThan(before.selection.total);
    expect(['select_nodes', 'confirm_plan']).toContain(after.step);
  });
});
