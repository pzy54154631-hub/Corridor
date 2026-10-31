// 23-suggestion: model suggestions under a plain clarification. Scripted, none is shown.
// With the test-only fake model (suggest-fake), the reply lists the system-written question
// and the fixed note, and nothing is on the device; clicking it sends exactly that sentence
// as a new message, the transfer card follows and only the device gesture settles it.
// Screenshots are test-only; this spec is not evidence of any real model's suggestions.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T } = h;
const UNCLEAR = '帮我给妈妈弄500块过去';
const SENTENCE = '给妈妈转500元';

async function goLive(page) {
  const accessCode = h.ready().access_code;
  expect(accessCode, 'the suggest-fake harness publishes its sentinel access code').toBeTruthy();
  await Promise.all([
    page.waitForResponse((response) => new URL(response.url()).pathname === '/api/live'),
    page.locator(T('planner-badge')).click(),
  ]);
  await expect(page.locator(T('live-sheet'))).toBeVisible();
  await page.locator(`${T('live-sheet')} ${T('live-code')}`).fill(accessCode);
  const unlock = h.waitApi(page, '/api/live/unlock');
  await page.locator(T('live-unlock')).click();
  expect((await (await unlock).json()).result).toBe('accepted');
  await h.clickAndWait(page, page.locator(T('live-toggle')), '/api/live/toggle');
  await expect(page.locator(T('planner-badge'))).toHaveAttribute('data-kind', 'model');
  await page.keyboard.press('Escape');
  await expect(page.locator(T('live-sheet'))).toBeHidden();
}

test.describe('23-suggestion', () => {
  test('a scripted clarification shows no model suggestion @scripted', async ({ page }) => {
    await h.start(page, { view: 'judge' });
    const rkey = await h.send(page, UNCLEAR);
    const reply = h.responseOf(page, rkey);
    await expect(reply).toHaveAttribute('data-status', 'clarify');
    await expect(reply.locator(T('suggestion-select'))).toHaveCount(0);
    expect(await h.deviceStep(page)).toBeNull();
  });

  test('a fake-model suggestion is sent as its sentence and settles only on the device @suggest-fake', async ({
    page,
    request,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page, { view: 'judge' });
    await goLive(page);

    const rkey = await h.send(page, UNCLEAR);
    const reply = h.responseOf(page, rkey);
    await expect(reply).toHaveAttribute('data-status', 'clarify');
    const block = reply.locator(T('suggestion-select'));
    await expect(block).toBeVisible();
    const question = h.catalog('suggest.question').replace('{sentence}', SENTENCE);
    const choice = block.locator(T('suggestion-choice-0'));
    await expect(choice).toHaveText(question);
    await expect(block.locator(T('suggestion-note'))).toHaveText(h.catalog('suggest.note'));
    await expect(block.locator(T('suggestion-evidence'))).toBeVisible();
    await expect(reply.locator(T('card'))).toHaveCount(0);
    expect(await h.deviceStep(page)).toBeNull();
    await h.screenshot(page, '23-suggestion', 'offered', { testOnly: true });

    const response = await h.clickAndWait(page, choice, '/suggestion');
    const body = await response.json();
    expect(body.ok, JSON.stringify(body.error || {})).toBe(true);
    const chosen = h.responseOf(page, body.rkey);
    await expect(chosen).toBeVisible();
    await expect(page.locator(T('turn-user')).last()).toHaveText(SENTENCE);
    await expect(chosen.locator(T('card')).first()).toBeVisible();
    const state = await h.latestState(bodies);
    expect(state.chat.responses[rkey].suggestion_select.choices[0].state).toBe('adopted');
    await expect(reply.locator(T('suggestion-state-0'))).toBeVisible();
    expect(await h.isLocked(choice)).toBe(true);

    await h.expectStep(page, 'confirm');
    await h.screenshot(page, '23-suggestion', 'card', { testOnly: true });
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await expect(chosen.locator(T('result-banner'))).toBeVisible();
    const counts = await h.probe(request);
    if (counts) expect(counts.device_complete_mfa).toBe(0);
  });
});
