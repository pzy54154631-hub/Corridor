// 18-ordering (core): state-changing requests leave one at a time in click order, so a
// changed answer always lands before a gesture (INV-9.5); a duplicated tab gets its own
// session.
const { test, expect } = require('playwright/test');
const h = require('../lib/helpers.cjs');

const { T, SAY } = h;

function recordPosts(page) {
  const log = [];
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname.startsWith('/api/')) {
      log.push({ path: new URL(request.url()).pathname, event: 'sent' });
    }
  });
  page.on('requestfinished', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname.startsWith('/api/')) {
      log.push({ path: new URL(request.url()).pathname, event: 'done' });
    }
  });
  return log;
}

test.describe('18-ordering', () => {
  test('a quick risky answer then confirm: the answer goes first and the confirm is refused @scripted', async ({
    page,
    request,
  }) => {
    await h.start(page);
    const rkey = await h.send(page, SAY.NEW);
    const reply = h.responseOf(page, rkey);
    await h.answerGate(page, { verified: 'yes', guided: 'no' });
    await h.expectStep(page, 'confirm');
    const before = await h.probe(request);
    const log = recordPosts(page);

    const confirmResponse = h.waitApi(page, '/confirm').catch(() => null);
    await reply.locator(T('gate-q-guided-yes')).click({ noWaitAfter: true });
    await page
      .locator(`${T('device-pane')} ${T('device-confirm')}`)
      .click({ timeout: 2_000, noWaitAfter: true })
      .catch(() => null);
    const answered = h.waitApi(page, '/gate/answer');
    const confirm = await Promise.race([
      confirmResponse,
      answered.then(() => page.waitForTimeout(1_500)).then(() => null),
    ]);

    const sent = log.filter((item) => item.event === 'sent').map((item) => item.path);
    const answerIndex = sent.findIndex((path) => path.endsWith('/gate/answer'));
    expect(answerIndex).toBeGreaterThanOrEqual(0);
    const confirmIndex = sent.findIndex((path) => path.endsWith('/confirm'));
    if (confirmIndex >= 0) {
      expect(confirmIndex).toBeGreaterThan(answerIndex);
      const doneAnswer = log.findIndex(
        (item) => item.event === 'done' && item.path.endsWith('/gate/answer'),
      );
      const sentConfirm = log.findIndex(
        (item) => item.event === 'sent' && item.path.endsWith('/confirm'),
      );
      expect(sentConfirm, 'confirm waits for the answer to finish').toBeGreaterThan(doneAnswer);
      expect(confirm && confirm.status()).toBe(409);
    }
    await expect(reply.locator(T('gate'))).toHaveAttribute('data-decision', 'pause');
    await h.expectStep(page, 'gate_blocked');
    const after = await h.probe(request);
    if (before && after) expect(after.device_approve).toBe(before.device_approve);
  });

  test('a duplicated tab (copied sessionStorage) is moved to a new session with a notice @scripted', async ({
    context,
    page,
  }) => {
    await h.start(page);
    const stored = await page.evaluate(() => {
      const out = {};
      for (let index = 0; index < sessionStorage.length; index += 1) {
        const key = sessionStorage.key(index);
        out[key] = sessionStorage.getItem(key);
      }
      return out;
    });
    const tabKey = Object.keys(stored).find((key) => /^[A-Za-z0-9_-]{32}$/.test(stored[key]));
    expect(tabKey, 'the page keeps its tab in sessionStorage').toBeTruthy();

    const copy = await context.newPage();
    await copy.addInitScript(
      ([key, value]) => {
        if (!sessionStorage.getItem(key)) sessionStorage.setItem(key, value);
      },
      [tabKey, stored[tabKey]],
    );
    const sessions = [];
    copy.on('response', async (response) => {
      if (new URL(response.url()).pathname === '/api/session') {
        sessions.push(await response.json().catch(() => ({})));
      }
    });
    await copy.goto('/');
    await expect(copy.locator(T('stage-header'))).toBeVisible();
    await expect.poll(() => sessions.filter((body) => body.tab).length).toBeGreaterThan(0);
    await expect
      .poll(() => sessions.filter((body) => body.tab).pop().tab, { timeout: 10_000 })
      .not.toBe(stored[tabKey]);
    await expect(copy.locator(T('toast')).first()).toBeVisible();
    await copy.close();
  });

  // R1 (E-9): two requests left on one generation; their stale_generation answers name two
  // newer generations and arrive in reverse order. The late, older one is reported, never
  // adopted: the page does not roll back from the newer generation.
  test('a late stale_generation answer never rolls the page back @scripted', async ({ page }) => {
    await h.start(page);
    const seen = await page.evaluate(async () => {
      const api = await import('/static/js/api.js');
      const { store } = await import('/static/js/store.js');
      const base = store.state;
      const real = window.fetch;
      const held = new Map();
      window.fetch = (path) =>
        new Promise((resolve) => {
          held.set(String(path), resolve);
        });
      const answer = (generation) =>
        new Response(
          JSON.stringify({
            ok: false,
            error: { code: 'stale_generation', message: null, retry: false },
            generation,
            revision: 0,
            state: { ...base, generation, revision: 0 },
          }),
          { status: 409, headers: { 'Content-Type': 'application/json' } },
        );
      const before = api.currentGeneration();
      try {
        const late = api.get('/api/state?order=late');
        const early = api.get('/api/state?order=early');
        while (!(held.has('/api/state?order=late') && held.has('/api/state?order=early'))) {
          await new Promise((resolve) => setTimeout(resolve, 10));
        }
        held.get('/api/state?order=early')(answer('genNewer0002'));
        await early;
        const afterEarly = api.currentGeneration();
        held.get('/api/state?order=late')(answer('genOlder0001'));
        await late;
        return { before, afterEarly, afterLate: api.currentGeneration() };
      } finally {
        window.fetch = real;
      }
    });
    expect(seen.afterEarly).toBe('genNewer0002');
    expect(seen.afterLate).toBe('genNewer0002');
    expect(seen.before).not.toBe('genNewer0002');
  });
});
