// 04-clarify (core): choose a payee, review, confirm on the device; the choice locks once a
// pending card or result exists; an earlier reply must be made current first (RW-1).
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

async function choose(page, reply, index) {
  await reply.locator(T(`clarify-choice-${index}`)).click();
  await expect(reply.locator(T('clarify-review'))).toBeEnabled();
  await h.clickAndWait(page, reply.locator(T('clarify-review')), '/recipient');
}

test.describe('04-clarify', () => {
  test('CLARIFY_PLAIN: choose, review, card, confirm; the candidates lock afterwards @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    const rkey = await h.send(page, SAY.CLARIFY_PLAIN);
    const reply = h.responseOf(page, rkey);
    await expect(reply.locator(T('clarify-choice-0'))).toBeVisible();
    await expect(reply.locator(T('clarify-choice-1'))).toBeVisible();
    await expect(reply.locator(T('card'))).toHaveCount(0);
    expect(await h.isLocked(reply.locator(T('clarify-review')))).toBe(true);
    await h.screenshot(page, '04-clarify', 'candidates');

    await choose(page, reply, 0);
    await expect(reply.locator(T('card')).first()).toBeVisible();
    const step = await h.deviceStep(page);
    expect(['confirm', 'gate_blocked']).toContain(step);
    if (step === 'gate_blocked') await h.answerGate(page, { verified: 'yes', guided: 'no' });
    await h.expectStep(page, 'confirm');
    await h.screenshot(page, '04-clarify', 'card');
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    if ((await h.deviceStep(page)) === 'mfa_request') await h.completeRed(page);
    await expect(reply.locator(T('result-banner'))).toBeVisible();

    await expect(reply.locator(T('clarify-locked'))).toBeVisible();
    expect(await h.isLocked(reply.locator(T('clarify-choice-1')))).toBe(true);
    expect((await h.responseState(bodies, rkey)).clarify.locked).toBe(true);
  });

  test('an earlier clarification cannot be reviewed until it is made current @scripted', async ({
    page,
  }) => {
    await h.start(page);
    const first = await h.send(page, SAY.CLARIFY_PLAIN);
    await h.send(page, SAY.balance);
    const earlier = h.responseOf(page, first);
    await expect(earlier).toHaveAttribute('data-current', 'false');
    const review = earlier.locator(T('clarify-review'));
    if ((await review.count()) > 0) expect(await h.isLocked(review)).toBe(true);

    const back = earlier.locator(`${T('response-back')}, ${T('gate-back')}`).first();
    await expect(back).toBeVisible();
    await h.clickAndWait(page, back, '/current');
    await expect(earlier).toHaveAttribute('data-current', 'true');
    await choose(page, earlier, 1);
    await expect(earlier.locator(T('card')).first()).toBeVisible();
  });
});
