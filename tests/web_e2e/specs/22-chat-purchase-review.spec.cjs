// The native chat preference review precedes the canonical card, MFA and settlement.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');
const { T } = h;

test('natural-language purchase reviews its terms before the red card and MFA @scripted', async ({ page }) => {
  const bodies = h.collectApiBodies(page);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await h.start(page);
  const rkey = await h.send(page, '帮我申购2000元稳健30天持有');
  await h.expectStep(page, 'review_purchase');
  const pane = page.locator(T('device-pane'));
  const review = pane.locator(T('purchase-review'));
  await expect(review).toBeVisible();
  await expect(review.locator('[data-field="selected_product_ref"]')).toContainText('P_A');
  await expect(review.locator('[data-field="source_account_ref"]')).toContainText('A_CURRENT');
  await expect(review.locator('[data-field="amount"]')).toContainText('2,000.00');
  await expect(review.locator('[data-field]')).toHaveCount(11);
  await expect(pane.locator(T('card'))).toHaveCount(0);
  const initial = (await h.latestState(bodies)).device.tasks.find(task => task.rkey === rkey);
  expect(initial.cards).toEqual([]);
  expect(initial.hash).toBeUndefined();
  await review.scrollIntoViewIfNeeded();
  await h.screenshot(page, '22-chat-purchase-review', 'unsigned-terms');

  const prepared = await h.deviceConfirm(page);
  expect(prepared.ok).toBe(true);
  await h.expectStep(page, 'mfa_request');
  await expect(review).toHaveCount(0);
  await expect(pane.locator(T('card')).first()).toBeVisible();
  await h.screenshot(page, '22-chat-purchase-review', 'prepared-before-mfa');
  await h.completeRed(page);
  await h.expectStep(page, 'done');
  const response = await h.responseState(bodies, rkey);
  expect(response.status).toBe('settled');
  await expect(h.responseOf(page, rkey).locator(T('result-banner'))).toBeVisible();
  await h.screenshot(page, '22-chat-purchase-review', 'settled');
  expect(errors).toEqual([]);
});
