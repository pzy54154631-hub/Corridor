// 12-i18n (core): one switch re-renders every text; the card hash, gate record and gesture
// stay the same (P0-14, INV-1.3).
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

test.describe('12-i18n', () => {
  test('English and back: lang attribute, same display-card hash, the gesture still works @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page, { view: 'judge' });
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
    const rkey = await h.send(page, SAY.yellow);
    await h.expectStep(page, 'confirm');
    const zh = (await h.latestState(bodies));
    const zhTask = zh.device.tasks[0];
    const zhDisplay = zh.evidence.consistency && zh.evidence.consistency.display_card.full;
    const zhCards = zh.chat.responses[rkey].cards;

    await h.setLocale(page, 'en');
    const en = (await h.latestState(bodies));
    expect(en.locale).toBe('en');
    const enTask = en.device.tasks[0];
    expect(enTask.hash.full).toBe(zhTask.hash.full);
    if (zhDisplay) expect(en.evidence.consistency.display_card.full).toBe(zhDisplay);
    expect(enTask.gesture && enTask.gesture.op).toBe('confirm');
    const factKeys = (cards) => cards.map((card) => card.facts.map((fact) => fact.key));
    expect(factKeys(en.chat.responses[rkey].cards)).toEqual(factKeys(zhCards));
    expect(en.chat.responses[rkey].status_text).not.toBe(zh.chat.responses[rkey].status_text);
    if (zhTask.hash.status !== 'not_yet') {
      await expect(page.locator(`${T('device-pane')} ${T('device-hash')}`)).toContainText(
        zhTask.hash.short,
      );
    }
    await h.screenshot(page, '12-i18n', 'judge-en');

    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await expect(h.responseOf(page, rkey).locator(T('result-banner'))).toBeVisible();
    await h.setLocale(page, 'zh-CN');
    await expect(h.responseOf(page, rkey).locator(T('result-banner'))).toBeVisible();
  });

  test('the customer view switches language too, and the corner stays on top @scripted', async ({
    page,
  }) => {
    await h.start(page);
    await h.setLocale(page, 'en');
    await h.assertCornerOnTop(page);
    await h.screenshot(page, '12-i18n', 'customer-en-home');
    await h.setLocale(page, 'zh-CN');
  });
});
