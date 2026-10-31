// 03-phrase-gate (core): the phrase gate and the scam drill create no card and dispatch
// nothing (P0-4); the judge view explains why.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

async function dispatchCount(page) {
  return Number(await page.locator(T('evidence-dispatch')).getAttribute('data-count'));
}

async function expectPhraseGate(page, bodies, rkey) {
  const reply = h.responseOf(page, rkey);
  await expect(reply.locator(T('card'))).toHaveCount(0);
  await expect(page.locator(`${T('device-pane')} ${T('device-task')}`)).toHaveCount(0);
  const vm = (await h.responseState(bodies, rkey));
  expect(vm.cards).toEqual([]);
  expect(vm.device_task).toBeNull();
  const fixed = h.templateRegex(h.catalog('polish.scam.phrase_gate', 'zh-CN'));
  expect(vm.copy_lines.some((line) => fixed.test(line))).toBe(true);
  await expect(page.locator(T('chat-log'))).toContainText('96110');
}

test.describe('03-phrase-gate', () => {
  test('HELD: fixed anti-fraud copy, no card, idle device, no dispatch @scripted', async ({
    page,
    request,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page, { view: 'judge' });
    const before = await dispatchCount(page);
    const counts = await h.probe(request);
    const rkey = await h.send(page, SAY.HELD);
    await expectPhraseGate(page, bodies, rkey);
    await expect(h.responseOf(page, rkey).locator(T('phrase-gate-note'))).toBeVisible();
    expect(await dispatchCount(page)).toBe(before);
    const after = await h.probe(request);
    if (counts && after) expect(after.device_approve).toBe(counts.device_approve);
    await h.screenshot(page, '03-phrase-gate', 'held-judge');

    await h.setView(page, 'customer');
    await expect(h.responseOf(page, rkey).locator(T('phrase-gate-note'))).toHaveCount(0);
    await h.screenshot(page, '03-phrase-gate', 'held-customer');
  });

  test('scam drill shortcut: same fixed copy and no card; the judge caption is shown @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page, { view: 'judge' });
    await expect(page.locator(T('drill-caption'))).toBeVisible();
    const before = await dispatchCount(page);
    const rkey = await h.shortcut(page, 'scam_drill');
    await expectPhraseGate(page, bodies, rkey);
    expect(await dispatchCount(page)).toBe(before);
    const caption = (await h.latestState(bodies)).chat.drill_caption;
    if (caption) {
      const judge = h.catalog('polish.scam.drill_caption_judge', 'zh-CN');
      const head = judge.split('{')[0].trim().slice(0, 8);
      if (head) expect(caption).toContain(head);
    }
    await h.screenshot(page, '03-phrase-gate', 'drill-judge');
  });
});
