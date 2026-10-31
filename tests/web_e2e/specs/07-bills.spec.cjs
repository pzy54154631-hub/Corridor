// 07-bills (ext 2): "September bill" shows totals and category bars; "unusual charges"
// shows the anomaly cards; the judge view adds the source fields.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T } = h;
const BILL = '看看9月账单';
const ANOMALY = '有没有异常扣款';

test.describe('07-bills', () => {
  test('bill totals, category bars and unusual charges @scripted', async ({ page }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page, { view: 'judge' });
    const caps = (await h.latestState(bodies)).capabilities;
    test.skip(!caps.shortcuts.includes('billing'), 'ext 2 is not in FEATURES yet');

    const rkey = await h.send(page, BILL);
    const reply = h.responseOf(page, rkey);
    await expect(reply.locator(T('result-banner'))).toBeVisible();
    const bill = (await h.responseState(bodies, rkey)).result.bill;
    expect(bill.totals.length).toBeGreaterThan(0);
    expect(bill.totals.every((total) => typeof total.source === 'string')).toBe(true);
    for (const total of bill.totals) await expect(reply).toContainText(total.value);
    for (const category of bill.categories) {
      expect(Number(category.ratio)).toBeGreaterThanOrEqual(0);
      expect(Number(category.ratio)).toBeLessThanOrEqual(1);
      await expect(reply).toContainText(category.label);
    }
    await h.screenshot(page, '07-bills', 'september');

    const second = await h.send(page, ANOMALY);
    const anomalies = (await h.responseState(bodies, second)).result.bill.anomalies;
    expect(anomalies.length).toBeGreaterThan(0);
    for (const item of anomalies) await expect(h.responseOf(page, second)).toContainText(item.title);
    await h.screenshot(page, '07-bills', 'anomalies');

    await h.setView(page, 'customer');
    const customer = (await h.latestState(bodies)).chat.responses[rkey].result.bill;
    expect(customer.totals.every((total) => total.source === undefined)).toBe(true);
  });
});
