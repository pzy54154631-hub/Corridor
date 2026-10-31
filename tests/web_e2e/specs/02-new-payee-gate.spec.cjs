// 02-new-payee-gate (core): S6 self-check questions and R7 (P0-3). The page renders the
// server's gate decision; it never decides on its own.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

function deviceButtons(page) {
  return page.locator(
    `${T('device-pane')} ${T('device-confirm')}, ${T('device-pane')} ${T('device-mfa-request')}, ${T('device-pane')} ${T('device-code-input')}`,
  );
}

async function expectBlocked(page) {
  await h.expectStep(page, 'gate_blocked');
  await expect(deviceButtons(page)).toHaveCount(0);
  await expect(page.locator(`${T('device-pane')} ${T('device-cancel')}`)).toBeVisible();
}

// Answer safely and finish the transfer from whatever tier the card has.
async function finish(page, reply) {
  await h.answerGate(page, { verified: 'yes', guided: 'no' });
  await expect(reply.locator(T('gate'))).toHaveAttribute('data-decision', 'proceed');
  await h.expectStep(page, 'confirm');
  expect((await h.deviceConfirm(page)).ok).toBe(true);
  if ((await h.deviceStep(page)) === 'mfa_request') await h.completeRed(page);
  await expect(reply.locator(T('result-banner'))).toBeVisible();
}

test.describe('02-new-payee-gate', () => {
  test('NEW (level 1): blocked until answered; "someone is guiding me" pauses and latches; retry, answer safely, finish @scripted', async ({
    page,
  }) => {
    await h.start(page);
    const rkey = await h.send(page, SAY.NEW);
    const reply = h.responseOf(page, rkey);
    const gate = reply.locator(T('gate'));
    await expect(gate).toHaveAttribute('data-decision', 'incomplete');
    await expect(gate).toHaveAttribute('data-interactive', 'true');
    await expect(reply.locator(`${T('card')} ${T('gate')}`)).toHaveCount(0);
    await expectBlocked(page);
    await h.screenshot(page, '02-new-payee-gate', 'new-blocked');

    await h.answerGate(page, { verified: 'yes', guided: 'yes' });
    await expect(gate).toHaveAttribute('data-decision', 'pause');
    await expect(reply.locator(T('gate-pause'))).toBeVisible();
    await expect(reply.locator(T('gate-self-report'))).toBeVisible();
    expect(await h.isLocked(reply.locator(T('gate-q-guided-no')))).toBe(true);
    expect(await h.isLocked(reply.locator(T('gate-q-verified-yes')))).toBe(true);
    await expectBlocked(page);
    await h.screenshot(page, '02-new-payee-gate', 'new-paused');

    await h.retryGate(page);
    await expect(gate).not.toHaveAttribute('data-decision', 'pause');
    await finish(page, reply);
  });

  test('FRAUD (level 2): the reply opens with the anti-fraud sentence; "not verified independently" pauses @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    const rkey = await h.send(page, SAY.FRAUD);
    const reply = h.responseOf(page, rkey);
    const vm = (await h.responseState(bodies, rkey));
    const opening = h.templateRegex(h.catalog('polish.scam.opening', 'zh-CN'));
    expect(vm.copy_lines[0]).toMatch(opening);
    await expect(page.locator(T('chat-log'))).toContainText(vm.copy_lines[0].slice(0, 12));
    await expectBlocked(page);

    await h.answerGate(page, { verified: 'no', guided: 'no' });
    await expect(reply.locator(T('gate'))).toHaveAttribute('data-decision', 'pause');
    expect(await h.isLocked(reply.locator(T('gate-q-verified-yes')))).toBe(true);
    await expectBlocked(page);
    await h.screenshot(page, '02-new-payee-gate', 'fraud-paused');

    await h.retryGate(page);
    await finish(page, reply);
  });

  test('DIRECTED_NEW: R7 presets question 2 and pauses at once; only retry clears it @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    const rkey = await h.send(page, SAY.DIRECTED_NEW);
    const reply = h.responseOf(page, rkey);
    const gate = reply.locator(T('gate'));
    await expect(gate).toHaveAttribute('data-preset', 'true');
    await expect(gate).toHaveAttribute('data-decision', 'pause');
    await expect(reply.locator(T('gate-pause'))).toBeVisible();
    const lead = h.catalog('polish.scam.pause_lead_preset', 'zh-CN');
    expect((await h.responseState(bodies, rkey)).gate.pause.copy.startsWith(lead.trim())).toBe(true);
    await expectBlocked(page);
    await h.screenshot(page, '02-new-payee-gate', 'directed-preset');

    await h.retryGate(page);
    await expect(gate).toHaveAttribute('data-preset', 'false');
    await expect(gate).toHaveAttribute('data-decision', 'incomplete');
    await finish(page, reply);
  });

  test('OPENING: a plain authority mention still opens with the anti-fraud sentence @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    const rkey = await h.send(page, SAY.OPENING);
    const vm = (await h.responseState(bodies, rkey));
    expect(vm.copy_lines[0]).toMatch(h.templateRegex(h.catalog('polish.scam.opening', 'zh-CN')));
    await expect(h.responseOf(page, rkey).locator(T('gate'))).toBeVisible();
  });

  test('switching to English keeps the answers and the device gesture (no re-evaluation) @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    const rkey = await h.send(page, SAY.NEW);
    const reply = h.responseOf(page, rkey);
    await h.answerGate(page, { verified: 'yes', guided: 'no' });
    await h.expectStep(page, 'confirm');
    const zh = (await h.responseState(bodies, rkey)).gate;

    await h.setLocale(page, 'en');
    const en = (await h.responseState(bodies, rkey)).gate;
    expect(en.version).toBe(zh.version);
    expect(en.decision).toBe('proceed');
    expect(en.questions.map((q) => q.answer)).toEqual(zh.questions.map((q) => q.answer));
    await expect(reply.locator(T('gate'))).toHaveAttribute('data-decision', 'proceed');
    await h.expectStep(page, 'confirm');
    await h.screenshot(page, '02-new-payee-gate', 'answered-en');
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    if ((await h.deviceStep(page)) === 'mfa_request') await h.completeRed(page);
    await expect(reply.locator(T('result-banner'))).toBeVisible();
    await h.setLocale(page, 'zh-CN');
  });
});
