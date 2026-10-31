// 08-subscriptions (ext 3): query (yellow card, device confirmation) then cancel one
// subscription; the chat path when the shortcut is open, the accounts tab otherwise.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T } = h;
const QUERY = '查看我的订阅';

async function confirmOnDevice(page) {
  const step = await h.deviceStep(page);
  expect(['confirm', 'confirm_intent', 'mfa_request']).toContain(step);
  if (step === 'mfa_request') await h.completeRed(page);
  else expect((await h.deviceConfirm(page)).ok).toBe(true);
}

test.describe('08-subscriptions', () => {
  test('query and cancel a subscription with device confirmations @scripted', async ({ page, request }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page);
    const caps = (await h.latestState(bodies)).capabilities;
    const chat = caps.shortcuts.includes('subscriptions');
    test.skip(!chat && !caps.tabs.includes('accounts'), 'ext 3 is not in FEATURES yet');

    let scope;
    if (chat) {
      const rkey = await h.send(page, QUERY);
      scope = h.responseOf(page, rkey);
      await expect(scope.locator(T('card')).first()).toHaveAttribute('data-tier', 'yellow');
      await confirmOnDevice(page);
      await expect(scope.locator(T('result-banner'))).toBeVisible();
      const result = (await h.responseState(bodies, rkey)).result;
      expect(result.subscriptions.rows.length).toBeGreaterThan(0);
    } else {
      await page.locator(T('tab-accounts')).click();
      scope = page.locator(T('phone-pane'));
      await h.clickAndWait(page, scope.locator(T('subs-query')), '/api/subscriptions/query');
      await confirmOnDevice(page);
    }
    await h.screenshot(page, '08-subscriptions', 'listed');

    const choice = page.locator(T('subs-cancel-choice-0'));
    if (await choice.isVisible().catch(() => false)) {
      await choice.click();
      await h.clickAndWait(page, page.locator(T('subs-cancel')), '/subscriptions/cancel');
    } else {
      const rkey = await h.send(page, '取消订阅');
      const reply = h.responseOf(page, rkey);
      const vm = await h.responseState(bodies, rkey);
      expect(vm.subscription_select.locked).toBe(false);
      const selected = reply.locator(T('subscription-choice-0'));
      await expect(selected).toHaveText(vm.subscription_select.choices[0].label);
      const before = await h.probe(request);
      const response = await h.clickAndWait(page, selected, `/api/chat/responses/${rkey}/subscription`);
      const prepared = await response.json();
      expect(prepared.ok).toBe(true);
      expect(prepared.state.chat.responses[prepared.rkey].result).toBeNull();
      await expect(selected).toBeDisabled();
      const after = await h.probe(request);
      if (before && after) expect(after.device_approve).toBe(before.device_approve);
    }
    await confirmOnDevice(page);
    await expect(page.locator(T('result-banner')).last()).toBeVisible();
    await h.screenshot(page, '08-subscriptions', 'cancelled');
  });
});
