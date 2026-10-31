// A scheduled-authorization choice prepares a card; the later device gesture revokes it.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');
const { T } = h;

test('create a scheduled transfer, choose its cancellation, and confirm the revocation @scripted', async ({ page }) => {
  const bodies = h.collectApiBodies(page);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await h.start(page);

  const created = await h.send(page, '每月5号给房东转500元');
  await expect(h.responseOf(page, created).locator(T('card')).first()).toBeVisible();
  expect(['confirm', 'confirm_intent']).toContain(await h.deviceStep(page));
  expect((await h.deviceConfirm(page)).ok).toBe(true);
  if (await h.deviceStep(page) === 'mfa_request') await h.completeRed(page);
  await expect(h.responseOf(page, created).locator(T('result-banner'))).toBeVisible();

  const listed = await h.send(page, '取消每月5号给房东的转账');
  const reply = h.responseOf(page, listed);
  const select = reply.locator(T('schedule-cancel-select'));
  await expect(select).toBeVisible();
  const initial = await h.responseState(bodies, listed);
  expect(initial.schedule_cancel_select.locked).toBe(false);
  const choice = initial.schedule_cancel_select.choices[0];
  const button = reply.locator(T('schedule-cancel-choice-0'));
  await expect(button).toHaveText(choice.label);
  await button.scrollIntoViewIfNeeded();
  await h.screenshot(page, '21-schedule-cancel-choice', 'listed-before-choice');

  const response = await h.clickAndWait(
    page,
    button,
    `/api/chat/responses/${listed}/schedule-cancel`,
  );
  const body = await response.json();
  expect(body.ok).toBe(true);
  expect(body.rkey).not.toBe(listed);
  const current = h.responseOf(page, body.rkey);
  await expect(current.locator(T('card')).first()).toBeVisible();
  await h.expectStep(page, 'confirm_intent');
  expect(body.state.chat.responses[body.rkey].result).toBeNull();
  await expect(button).toBeDisabled();

  expect((await h.deviceConfirm(page)).ok).toBe(true);
  await expect(current.locator(T('result-banner'))).toBeVisible();
  await expect(current.locator('.scr-status')).toHaveText('已撤销定时授权');
  await expect(current.locator(T('result-banner'))).toContainText('已撤销这项定时转账授权的剩余执行');
  await h.screenshot(page, '21-schedule-cancel-choice', 'revoked-after-device-confirm');
  expect(errors).toEqual([]);
});
