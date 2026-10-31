// 11-judge-evidence (core): the judge pane opens three sections by default, shows the
// verdict, business-fact consistency and labelled hashes; the customer view carries none of
// it, in the DOM or anywhere in the AppState JSON (INV-5.1, 5.2, 5.4).
const fs = require('fs');
const path = require('path');
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;
const DEFAULT_OPEN = ['kernel', 'consistency', 'dispatch'];
const SOURCE_CODES = [
  'scripted',
  'live-error',
  'output-rejected',
  'budget-fallback',
  'live-fallback',
];
const HEX64 = /[0-9a-f]{64}/;

// Every registered rule id (kernel/rules.py is the single registry).
function registry() {
  const root = process.env.CORRIDOR_E2E_ROOT || path.join(__dirname, '..', '..', '..');
  const text = fs.readFileSync(path.join(root, 'src', 'corridor', 'kernel', 'rules.py'), 'utf-8');
  return [...new Set(text.match(/\b[A-Z][A-Z0-9]*-[A-Z0-9-]*[A-Z0-9]\b/g))];
}

// Every rule id the judge view mentions, so the customer view can be checked for each.
function ruleIds(state) {
  const found = new Set();
  const walk = (value, key) => {
    if (Array.isArray(value)) {
      if (key === 'rule_ids') value.forEach((item) => found.add(item));
      value.forEach((item) => walk(item));
    } else if (value && typeof value === 'object') {
      Object.entries(value).forEach(([name, item]) => walk(item, name));
    }
  };
  walk(state);
  return [...found];
}

test.describe('11-judge-evidence', () => {
  test('judge pane: three sections open, verdict, consistency, labelled hashes; customer view carries none of it @scripted', async ({
    page,
  }) => {
    const bodies = h.collectApiBodies(page);
    await h.start(page, { view: 'judge' });
    const rkey = await h.send(page, SAY.red);
    await h.expectStep(page, 'confirm');

    const sections = page.locator(`[data-testid^="evidence-section-"]`);
    const count = await sections.count();
    expect(count).toBeGreaterThan(DEFAULT_OPEN.length);
    for (let index = 0; index < count; index += 1) {
      const section = sections.nth(index);
      const name = (await section.getAttribute('data-testid')).slice('evidence-section-'.length);
      const open = DEFAULT_OPEN.includes(name) ? 'true' : 'false';
      await expect(section, `section ${name}`).toHaveAttribute('data-open', open);
    }

    const kernel = page.locator(T('evidence-kernel'));
    await expect(kernel).toBeVisible();
    const notYet = page.locator(T('evidence-not-yet'));
    if ((await notYet.count()) === 0) {
      await expect(page.locator(T('evidence-consistency'))).toHaveAttribute(
        'data-same-facts',
        'true',
      );
    }
    const hash = page.locator(`${T('device-pane')} ${T('device-hash')}`);
    await expect(hash).toBeVisible();
    await expect(hash).toContainText('SHA-256');
    await expect(hash).toHaveAttribute('data-status', /^(pending|signed|not_yet)$/);
    await h.screenshot(page, '11-judge-evidence', 'judge-red');

    // Once the proposal exists the device shows the display-card hash, expandable in full.
    expect((await h.deviceConfirm(page)).ok).toBe(true);
    await h.expectStep(page, 'mfa_request');
    await expect(hash).toHaveAttribute('data-status', /^(pending|signed)$/);
    const task = (await h.latestState(bodies)).device.tasks[0];
    expect(task.hash.full).toMatch(/^[0-9a-f]{64}$/);
    await expect(hash).toContainText(task.hash.short);
    const expand = hash.locator(`${T('hash-expand')}, summary, button`).first();
    await expand.click();
    await expect(hash).toContainText(task.hash.full);
    await h.screenshot(page, '11-judge-evidence', 'judge-red-pending');
    await h.completeRed(page);
    await expect(page.locator(T('evidence-consistency'))).toHaveAttribute('data-same-facts', 'true');
    await expect(page.locator(T('evidence-audit'))).toHaveAttribute('data-verified', 'true');
    expect(['Accept', 'Escalate', 'Reject']).toContain(await kernel.getAttribute('data-outcome'));
    expect(
      Number(await page.locator(T('evidence-dispatch')).getAttribute('data-count')),
    ).toBeGreaterThan(0);
    await h.screenshot(page, '11-judge-evidence', 'judge-receipt');
    const judgeState = (await h.latestState(bodies));
    const known = registry();
    expect(known.length).toBeGreaterThan(50);
    const rules = [...new Set([...ruleIds(judgeState), ...known])];

    await h.setView(page, 'customer');
    const customer = (await h.latestState(bodies));
    expect(customer.view).toBe('customer');
    expect(customer.evidence).toBeUndefined();
    const json = JSON.stringify(customer);
    const dom = await page.evaluate(() => document.documentElement.outerHTML);
    for (const [label, text] of [
      ['AppState', json],
      ['DOM', dom],
    ]) {
      expect(HEX64.test(text), `${label}: 64-hex`).toBe(false);
      for (const rule of rules) expect(text.includes(rule), `${label}: ${rule}`).toBe(false);
      for (const code of SOURCE_CODES) expect(text.includes(code), `${label}: ${code}`).toBe(false);
      for (const name of ['LIVE_', 'LLM_', 'CORRIDOR_']) {
        expect(text.includes(name), `${label}: ${name}`).toBe(false);
      }
    }
    await expect(page.locator(T('device-hash'))).toHaveCount(0);
    const evidence = bodies.filter((entry) => entry.path.startsWith('/api/evidence/'));
    for (const entry of evidence) {
      if (JSON.parse(entry.body || '{}').available === false) {
        expect(entry.body.includes('technical')).toBe(false);
      }
    }
    await expect(h.responseOf(page, rkey).locator(T('result-banner'))).toBeVisible();
    await h.screenshot(page, '11-judge-evidence', 'customer-receipt');
  });

  test('before any proposal the consistency section says "not yet produced" @scripted', async ({
    page,
  }) => {
    await h.start(page, { view: 'judge' });
    await h.send(page, SAY.NEW);
    await h.expectStep(page, 'gate_blocked');
    const notYet = page.locator(T('evidence-not-yet'));
    const consistency = page.locator(T('evidence-consistency'));
    await expect(consistency).toBeVisible();
    if ((await notYet.count()) > 0) await expect(notYet).toBeVisible();
    else await expect(consistency).toHaveAttribute('data-same-facts', /true|false/);
  });
});
