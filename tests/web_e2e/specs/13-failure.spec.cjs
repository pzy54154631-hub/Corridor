// 13-failure (core): a failure after the controller call closes the task (fail-once:
// expected failure; fail-unknown: outcome unknown, X4); only Cancel remains, and cancelling
// restores the transaction boundary. An unknown outcome also distrusts the session (R2-E3):
// a new request needs a reset first.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

async function failThenRecover(page, request, kind, status) {
  const before = await h.probe(request);
  await h.start(page);
  const rkey = await h.send(page, SAY.yellow);
  const reply = h.responseOf(page, rkey);
  await h.expectStep(page, 'confirm');
  const [response] = await Promise.all([
    h.waitApi(page, '/confirm'),
    page.locator(`${T('device-pane')} ${T('device-confirm')}`).click(),
  ]);
  expect(response.status()).toBe(status);
  const body = await response.json();
  expect(body.ok).toBe(false);
  expect(body.state).toBeTruthy();
  expect(JSON.stringify(body)).not.toMatch(/Traceback|injected failure|harness/);
  const after = await h.probe(request);
  if (before && after) {
    expect(after.workflow_confirm_transfer).toBe(before.workflow_confirm_transfer + 1);
  }

  const block = page.locator(`${T('device-pane')} ${T('failure-block')}`);
  await expect(block).toHaveAttribute('data-kind', kind);
  await expect(reply.locator(T('failure-block'))).toHaveAttribute('data-kind', kind);
  await expect(page.locator(`${T('device-pane')} ${T('device-confirm')}`)).toHaveCount(0);
  await expect(page.locator(`${T('device-pane')} ${T('device-cancel')}`)).toBeVisible();
  await h.expectStep(page, kind === 'failed' ? 'closed_failed' : 'closed_unknown');
  await h.screenshot(page, '13-failure', `closed-${kind}`, { testOnly: true });

  await h.clickAndWait(page, page.locator(`${T('device-pane')} ${T('device-cancel')}`), '/cancel');
  await expect(page.locator(`${T('device-pane')} ${T('device-confirm')}`)).toHaveCount(0);
  if (kind === 'unknown') {
    // R2-E3: an unknown result distrusts the whole session. Cancel restores the boundary,
    // a new request is refused, and only a reset (a new synthetic session) starts afresh.
    await page.locator(T('composer-input')).fill(SAY.yellow);
    const refused = await h.clickAndWait(page, page.locator(T('composer-send')), '/api/chat/message');
    expect(refused.status()).toBe(409);
    expect((await refused.json()).error.code).toBe('not_applicable');
    await h.resetSession(page);
    await h.login(page);
    await expect(page.locator(T('composer-input'))).toBeVisible();
    return;
  }
  const next = await h.send(page, SAY.yellow);
  await h.expectStep(page, 'confirm');
  expect((await h.deviceConfirm(page)).ok).toBe(true);
  await expect(h.responseOf(page, next).locator(T('result-banner'))).toBeVisible();
}

test.describe('13-failure', () => {
  test('expected failure closes the task with a failure block @fail-once', async ({
    page,
    request,
  }) => {
    await failThenRecover(page, request, 'failed', 422);
  });

  test('unknown outcome closes the task; the text says the result cannot be confirmed @fail-unknown', async ({
    page,
    request,
  }) => {
    await failThenRecover(page, request, 'unknown', 500);
  });
});
