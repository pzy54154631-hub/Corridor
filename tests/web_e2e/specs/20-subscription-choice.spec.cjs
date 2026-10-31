// The response choice prepares an approval card; only a later device click executes it.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');
const { T } = h;

test.describe('20-subscription-choice', () => {
  test('query, choose a real subscription, then explicitly confirm cancellation @scripted', async ({ page, request }) => {
    const bodies = h.collectApiBodies(page);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await h.start(page);
    const query = await h.send(page, '查看我的订阅');
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await expect(h.responseOf(page, query).locator(T('result-banner'))).toBeVisible();
    const rkey = await h.send(page, '取消订阅');
    const reply = h.responseOf(page, rkey);
    const select = reply.locator(T('subscription-select'));
    await expect(select).toBeVisible();
    const initial = await h.responseState(bodies, rkey);
    expect(initial.subscription_select.locked).toBe(false);
    const chosen = initial.subscription_select.choices[0];
    const button = reply.locator(T('subscription-choice-0'));
    await expect(button).toHaveText(chosen.label);
    const before = await h.probe(request);
    await h.screenshot(page, '20-subscription-choice', 'explicit-choice');
    const response = await h.clickAndWait(page, button, `/api/chat/responses/${rkey}/subscription`);
    const body = await response.json();
    expect(body.ok).toBe(true);
    expect(body.rkey).not.toBe(rkey);
    const current = h.responseOf(page, body.rkey);
    await expect(current.locator(T('card')).first()).toBeVisible();
    await h.expectStep(page, 'confirm_intent');
    await expect(reply.locator(T('subscription-choice-0'))).toBeDisabled();
    const after = await h.probe(request);
    if (before && after) expect(after.device_approve).toBe(before.device_approve);
    expect(body.state.chat.responses[body.rkey].result).toBeNull();
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await expect(current.locator(T('result-banner'))).toBeVisible();
    await h.screenshot(page, '20-subscription-choice', 'cancelled-after-confirm');
    expect(errors).toEqual([]);
  });
});
