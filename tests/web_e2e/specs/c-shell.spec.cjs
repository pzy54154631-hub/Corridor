// C-line shell specs (fixtures mode): stage layout, device screen, evidence column, SMS page,
// request queue and adoption rules, accessibility basics. The page renders server state only.
const { test, expect } = require('playwright/test');
const { serve, load } = require('../lib/fixtures.cjs');
const { assertCornerOnTop, screenshot } = require('../lib/helpers.cjs');

const T = (id) => `[data-testid="${id}"]`;
const HEX64 = /[0-9a-f]{64}/;

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function withState(name, edit) {
  const body = clone(load(name));
  if (edit) edit(body.state);
  return body;
}

async function open(page, state, options = {}) {
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  const api = await serve(page, { state, ...options });
  await page.goto(options.url || '/');
  await expect(page.locator(T('stage-header'))).toBeVisible();
  return { api, errors };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('@c-shell customer stage: two columns, no evidence, corner on top', async ({ page }) => {
  const { errors } = await open(page, 'customer-red-code');
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  await expect(page.locator(T('evidence-pane'))).toHaveCount(0);
  await expect(page.locator(T('judge-strip'))).toHaveCount(0);
  await expect(page.locator(T('evidence-toggle'))).toHaveCount(0);
  await expect(page.getByRole('banner')).toBeVisible();
  await expect(page.getByRole('main')).toBeVisible();
  await expect(page.getByRole('complementary')).toHaveCount(0);
  for (const id of ['view-customer', 'view-judge', 'locale-zh', 'locale-en', 'stop-all', 'reset']) {
    await expect(page.locator(T(id))).toBeVisible();
  }
  await expect(page.locator(T('view-customer'))).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator(T('planner-badge'))).toHaveAttribute('data-kind', 'demo');
  await expect(page.locator(T('corner-notice'))).toHaveText('合成数据 · 模拟认证 · 无真实资金');
  const phone = await page.locator(T('phone-pane')).boundingBox();
  const device = await page.locator(T('device-pane')).boundingBox();
  expect(phone.width).toBeCloseTo(402, 0);
  expect(device.width).toBeCloseTo(360, 0);
  expect(device.x).toBeGreaterThan(phone.x + phone.width);
  // At 1920 the SMS window gets its own column beside the device; the group stays centred.
  const sms = await page.locator(T('sms-card')).boundingBox();
  expect(sms.x).toBeGreaterThan(device.x + device.width);
  expect(sms.y).toBeLessThan(device.y + 40);
  const center = (phone.x + sms.x + sms.width) / 2;
  expect(Math.abs(center - 960)).toBeLessThan(4);
  await expect(page.locator(T('phone-pane'))).toHaveAttribute('aria-label', '模拟手机银行 · 合成');
  await expect(page.locator(T('tab-assistant'))).toHaveAttribute('aria-selected', 'true');
  const body = await page.locator('body').innerText();
  expect(HEX64.test(body)).toBe(false);
  await assertCornerOnTop(page);
  await screenshot(page, 'c-shell', 'customer-red-code', { testOnly: true });
  expect(errors).toEqual([]);
});

test('@c-shell judge stage: evidence column with three sections open', async ({ page }) => {
  const { errors } = await open(page, 'judge-red-code');
  await expect(page.locator(T('judge-strip'))).toBeVisible();
  await expect(page.getByRole('complementary')).toBeVisible();
  const pane = page.locator(T('evidence-pane'));
  await expect(pane).toBeVisible();
  const box = await pane.boundingBox();
  expect(box.width).toBeGreaterThanOrEqual(380);
  expect(box.width).toBeLessThanOrEqual(560);
  for (const name of ['kernel', 'consistency', 'dispatch']) {
    await expect(page.locator(T(`evidence-section-${name}`))).toHaveAttribute('data-open', 'true');
  }
  for (const name of ['planner', 'safety', 'audit', 'intent', 'planner_log', 'technical']) {
    await expect(page.locator(T(`evidence-section-${name}`))).toHaveAttribute('data-open', 'false');
  }
  await expect(page.locator(T('evidence-kernel'))).toHaveAttribute('data-outcome', 'Escalate');
  const consistency = page.locator(T('evidence-consistency'));
  await expect(consistency).toHaveAttribute('data-same-hash', 'true');
  await expect(consistency).toHaveAttribute('data-same-facts', 'true');
  await expect(page.locator(T('evidence-dispatch'))).toHaveAttribute('data-count', '0');
  const short = consistency.locator('.hash-short').first();
  await expect(short).toHaveText(/^[0-9a-f]{16}$/);
  await expect(consistency.locator('.hash-full').first()).toBeHidden();
  await consistency.locator('.hash-more > summary').first().click();
  await expect(consistency.locator('.hash-full').first()).toHaveText(/^[0-9a-f]{64}$/);
  await expect(page.locator(T('device-hash'))).toHaveAttribute('data-status', 'pending');
  await page.locator(T('evidence-section-audit')).locator(':scope > summary').click();
  await expect(page.locator(T('evidence-audit'))).toHaveAttribute('data-verified', 'true');
  await page.locator(T('evidence-section-technical')).locator(':scope > summary').click();
  await expect(page.locator(T('evidence-section-technical')).locator('pre.json')).toBeVisible();
  await assertCornerOnTop(page);
  await screenshot(page, 'c-shell', 'judge-red-code', { testOnly: true });
  expect(errors).toEqual([]);
});

test('@c-device red code step: label, autocomplete, auto submit only the code', async ({ page }) => {
  const { api } = await open(page, 'customer-red-code');
  const task = page.locator(T('device-task'));
  await expect(task).toHaveAttribute('data-step', 'mfa_code');
  await expect(task).toHaveAttribute('data-current', 'true');
  await expect(page.locator(T('device-confirm'))).toHaveCount(0);
  await expect(page.locator(T('device-mfa-request'))).toHaveCount(0);
  await expect(page.locator(T('device-cancel'))).toBeVisible();
  const input = page.locator(T('device-code-input'));
  await expect(input).toHaveAttribute('inputmode', 'numeric');
  await expect(input).toHaveAttribute('autocomplete', 'one-time-code');
  await expect(input).toHaveAttribute('maxlength', '6');
  await expect(page.getByLabel('短信验证码（6 位）')).toBeVisible();
  const state = api.state;
  const gesture = state.device.tasks[0].gesture;
  const sms = page.locator(T('sms-frame'));
  await expect(sms).toHaveAttribute('src', state.device.sms.url);
  const verify = page.waitForRequest((r) => r.url().endsWith('/api/device/tasks/TR2/mfa/verify'));
  await input.fill('12345');
  await sleep(300);
  expect(api.requests.filter((r) => r.path.endsWith('/mfa/verify'))).toHaveLength(0);
  await input.pressSequentially('6');
  const request = await verify;
  expect(JSON.parse(request.postData())).toEqual({
    gesture: gesture.token, card_version: gesture.card_version, code: '123456',
  });
  const posts = () => api.requests.filter((r) => r.method === 'POST' && r.path !== '/api/session')
    .map((r) => r.path);
  await expect.poll(posts).toEqual(['/api/device/tasks/TR2/mfa/verify']);
  await expect(input).toHaveValue('');
});

test('@c-device cards in both columns are field-for-field equal; notes stay outside', async ({ page }) => {
  await open(page, 'customer-red-code');
  const facts = async (owner) =>
    page.locator(`${T('card')}[data-owner="${owner}"] ${T('card-fact')}`).evaluateAll((items) =>
      items.map((el) => [el.dataset.key, el.querySelector('dt').textContent, el.querySelector('dd').textContent]));
  const device = await facts('device');
  expect(device.length).toBeGreaterThan(0);
  const phoneCount = await page.locator(`${T('card')}[data-owner="phone"]`).count();
  if (phoneCount) expect(await facts('phone')).toEqual(device);
  expect(await page.locator(`${T('card')} ${T('card-note')}`).count()).toBe(0);
  expect(await page.locator(`${T('card')} ${T('gate')}`).count()).toBe(0);
});

test('@c-device locked shell: banner and no gesture buttons', async ({ page }) => {
  await open(page, 'judge-locked');
  const banner = page.locator(T('safety-banner'));
  await expect(banner).toHaveAttribute('data-locked', 'true');
  await expect(banner).toBeVisible();
  await expect(page.locator(T('device-confirm'))).toHaveCount(0);
  await expect(page.locator(T('device-mfa-request'))).toHaveCount(0);
  await screenshot(page, 'c-shell', 'judge-locked', { testOnly: true });
});

test('@c-device confirm sends the token shown; card_changed redraws and explains', async ({ page }) => {
  const yellow = withState('customer-red-code', (state) => {
    const task = state.device.tasks[0];
    task.step = 'confirm';
    task.step_text = '核对上面的内容无误后再确认';
    delete task.mfa;
    task.gesture = { op: 'confirm', token: 'AbCdEfGhIjKlMnOpQrStUv', card_version: '0123456789abcdef', label: '确认' };
  });
  const { api } = await open(page, yellow, { posts: ['err-card-changed'] });
  const confirm = page.locator(T('device-confirm'));
  await expect(confirm).toBeVisible();
  await expect.poll(async () => {
    const box = await confirm.boundingBox();
    return box ? Math.min(box.width, box.height) : 0;
  }).toBeGreaterThanOrEqual(44);
  await confirm.click();
  const toast = page.locator(T('toast')).last();
  await expect(toast).toHaveText('卡片内容已更新，请重新核对后再确认');
  await expect.poll(() => api.requests.filter((r) => r.path.endsWith('/confirm')).length).toBe(1);
  const sent = api.requests.find((r) => r.path.endsWith('/confirm'));
  expect(sent.body).toEqual({ gesture: 'AbCdEfGhIjKlMnOpQrStUv', card_version: '0123456789abcdef' });
  expect(sent.headers['x-corridor-tab']).toBeTruthy();
  expect(sent.headers['x-corridor-generation']).toBe(yellow.generation);
  await expect(page.locator(T('device-task'))).toHaveAttribute('data-step', 'mfa_code');
});

test('@c-queue state-changing requests go one at a time, in click order', async ({ page }) => {
  const { api } = await open(page, 'customer-red-code');
  const log = [];
  await page.route('**/api/stop', async (route) => {
    log.push('stop:start');
    await sleep(400);
    log.push('stop:end');
    await route.fallback();
  });
  await page.route('**/api/session/locale', async (route) => {
    log.push('locale:start');
    await route.fallback();
  });
  await page.locator(T('stop-all')).click();
  await expect(page.locator(T('stop-all'))).toBeVisible();
  await page.locator(T('locale-en')).click();
  await expect.poll(() => log.length).toBe(3);
  expect(log).toEqual(['stop:start', 'stop:end', 'locale:start']);
  const paths = api.requests.filter((r) => r.method === 'POST').map((r) => r.path);
  expect(paths.slice(-2)).toEqual(['/api/stop', '/api/session/locale']);
});

test('@c-queue stale generation adopts the new session and drops older responses', async ({ page }) => {
  const stale = load('err-stale-generation');
  const { api } = await open(page, 'customer-red-code', { posts: [stale] });
  await page.locator(T('stop-all')).click();
  await expect(page.locator(T('toast')).last()).toHaveAttribute('data-level', 'warning');
  await page.locator(T('locale-en')).click();
  await expect.poll(() => api.requests.filter((r) => r.path === '/api/session/locale').length).toBe(1);
  const last = api.requests.filter((r) => r.path === '/api/session/locale')[0];
  expect(last.headers['x-corridor-generation']).toBe(stale.state.generation);
});

test('@c-queue a 503 keeps the page and says the service is busy', async ({ page }) => {
  await open(page, 'customer-red-code', {
    posts: [{ status: 503, body: { ok: false, error: { code: 'busy', message: 'busy', retry: true } } }],
  });
  await page.locator(T('stop-all')).click();
  await expect(page.locator(T('toast')).last()).toHaveText('服务忙，请稍后重试');
  await expect(page.locator(T('device-task'))).toHaveAttribute('data-step', 'mfa_code');
});

test('@c-shell reset asks first, then posts once', async ({ page }) => {
  const { api } = await open(page, 'customer-red-code');
  await page.locator(T('reset')).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  expect(api.requests.filter((r) => r.path === '/api/session/reset')).toHaveLength(0);
  await page.locator(T('reset')).click();
  await page.locator(T('reset-confirm')).click();
  await expect.poll(() => api.requests.filter((r) => r.path === '/api/session/reset').length).toBe(1);
});

test('@c-shell english catalog switches lang and shell copy', async ({ page }) => {
  await open(page, 'customer-red-code-en');
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.locator(T('stop-all'))).toHaveText('Stop all');
  await expect(page.locator(T('locale-en'))).toHaveAttribute('aria-pressed', 'true');
  await screenshot(page, 'c-shell', 'customer-red-code-en', { testOnly: true });
});

test('@c-a11y focus ring, reduced motion and contrast', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page, 'judge-red-code');
  const duration = await page.locator(T('stop-all')).evaluate((el) => getComputedStyle(el).transitionDuration);
  expect(duration.split(',').every((d) => d.trim() === '0s'), duration).toBe(true);
  await page.locator(T('view-customer')).focus();
  await page.keyboard.press('Tab');
  const outline = await page.evaluate(() => {
    const el = document.querySelector(':focus-visible');
    return el ? parseFloat(getComputedStyle(el).outlineWidth) : 0;
  });
  expect(outline).toBeGreaterThanOrEqual(2);
  const ratios = await page.evaluate(() => {
    const parse = (c) => (c.match(/[\d.]+/g) || []).map(Number);
    const lum = ([r, g, b]) => {
      const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const bg = (el) => {
      for (let n = el; n; n = n.parentElement) {
        const c = parse(getComputedStyle(n).backgroundColor);
        if (c.length === 3 || (c.length === 4 && c[3] > 0.9)) return c.slice(0, 3);
      }
      return [255, 255, 255];
    };
    const ratio = (el) => {
      const a = lum(parse(getComputedStyle(el).color).slice(0, 3));
      const b = lum(bg(el));
      return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    };
    const pick = ['.tier-label', '.btn-primary', '.card-title', '.evidence-body .kv dt', '.status-label',
      '.mfa-method', '.tab', '.queue-sub', '.hash-label', '.evidence-foot', '.cancel-hint', '.brand-sub'];
    const out = {};
    for (const sel of pick) {
      const el = document.querySelector(sel);
      if (el) out[sel] = Math.round(ratio(el) * 100) / 100;
    }
    return out;
  });
  for (const [selector, value] of Object.entries(ratios)) {
    expect(value, selector).toBeGreaterThanOrEqual(4.5);
  }
});

test('@c-responsive 1280: evidence becomes a drawer that Esc closes', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await open(page, 'judge-red-code');
  const toggle = page.locator(T('evidence-toggle'));
  await expect(toggle).toBeVisible();
  await expect(page.locator(T('evidence-pane'))).toBeHidden();
  await toggle.click();
  await expect(page.locator(T('evidence-pane'))).toBeVisible();
  await assertCornerOnTop(page);
  await page.keyboard.press('Escape');
  await expect(page.locator(T('evidence-pane'))).toBeHidden();
  await expect(toggle).toBeFocused();
});

test('@c-responsive 900: the device opens as a sheet without any gesture', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 1000 });
  const { api } = await open(page, 'customer-red-code');
  const pane = page.locator(T('device-pane'));
  await expect(pane).toBeVisible();
  await expect(pane).toHaveClass(/is-open/);
  await assertCornerOnTop(page);
  await page.keyboard.press('Escape');
  await expect(pane).toBeHidden();
  const gestures = api.requests.filter((r) => /\/api\/device\//.test(r.path));
  expect(gestures).toHaveLength(0);
});

test('@c-sms phone page lists messages, newest marked, corner kept', async ({ page }) => {
  const phone = {
    ok: true,
    version: 'v1',
    title: '模拟手机',
    status_bar: '模拟设备 · 只接收合成短信',
    corner: '合成数据 · 模拟认证 · 无真实资金',
    messages: [
      { text: '【演示银行·模拟短信】验证码 ●●●●●●（已遮罩）', status: 'active', latest: true },
      { text: '【演示银行·模拟短信】验证码 ●●●●●●', status: 'used', latest: false, status_text: '已使用' },
    ],
    empty: null,
    unpaired: null,
    footer: '合成短信 · 无短信网关 · 无真实手机号',
  };
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  await serve(page, { state: 'customer-red-code', phone });
  await page.goto('/phone/Qm9nUjVxa2Zv_xT1aA?lang=zh-CN&view=customer');
  await expect(page.locator(T('sms-message'))).toHaveCount(2);
  await expect(page.locator(T('sms-message')).first()).toHaveAttribute('data-latest', 'true');
  await expect(page.locator(T('sms-message')).nth(1)).toHaveAttribute('data-status', 'used');
  await expect(page.locator(T('sms-message')).nth(1)).toContainText('已使用');
  await expect(page.locator(T('sms-corner'))).toHaveText(phone.corner);
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  expect(errors).toEqual([]);
});

test('@c-sms an unknown pair shows only the unpaired line', async ({ page }) => {
  await serve(page, {
    state: 'customer-red-code',
    phone: { ok: true, version: 'u', title: 't', status_bar: 's', corner: 'c', messages: [], empty: null, unpaired: '未配对', footer: 'f' },
  });
  await page.goto('/phone/AAAAAAAAAAAAAAAAAAAA?lang=en&view=judge');
  await expect(page.locator(T('sms-unpaired'))).toHaveText('未配对');
  await expect(page.locator(T('sms-message'))).toHaveCount(0);
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
});

test('@c-tabdup a duplicated tab gives up the shared tab and opens a new session', async ({ context }) => {
  const first = await context.newPage();
  const api1 = await serve(first, { state: 'customer-red-code' });
  await first.goto('/');
  await expect(first.locator(T('stage-header'))).toBeVisible();
  const held = await first.evaluate(() => sessionStorage.getItem('corridor.web.tab'));
  expect(held).toBeTruthy();
  const second = await context.newPage();
  await second.addInitScript((tab) => sessionStorage.setItem('corridor.web.tab', tab), held);
  const api2 = await serve(second, { state: 'customer-red-code' });
  await second.goto('/');
  await expect(second.locator(T('stage-header'))).toBeVisible();
  const opened = api2.requests.filter((r) => r.path === '/api/session');
  expect(opened).toHaveLength(1);
  expect(opened[0].body.tab).toBeUndefined();
  await expect(second.locator(T('toast')).first()).toHaveText('已为这个标签页新开一个合成会话');
  expect(api1.requests.filter((r) => r.path === '/api/session')).toHaveLength(1);
});

test('@c-shell ?view=judge is sent once, only when the session is created', async ({ page }) => {
  const { api } = await open(page, 'judge-red-code', { url: '/?view=judge' });
  const opened = api.requests.filter((r) => r.path === '/api/session');
  expect(opened).toHaveLength(1);
  expect(opened[0].body.view).toBe('judge');
  expect(opened[0].headers['x-corridor-web']).toBe('1');
});

test('@c-device plan subset: only the chosen cards, the count, and "create the new card"', async ({ page }) => {
  const { api } = await open(page, 'judge-plan-subset');
  const task = page.locator(T('device-task'));
  await expect(task).toHaveAttribute('data-step', 'confirm_subset');
  await expect(page.locator(`${T('device-pane')} ${T('device-selection')}`)).toHaveText('已选 2 / 4 项');
  await expect(task.locator(T('card'))).toHaveCount(2);
  await expect(page.locator(T('device-confirm'))).toHaveCount(0);
  const subset = page.locator(T('device-subset'));
  await expect(subset).toHaveText('生成新卡');
  await subset.click();
  await expect.poll(() => api.requests.filter((r) => r.path.endsWith('/api/device/tasks/TR4/confirm')).length).toBe(1);
  const sent = api.requests.find((r) => r.path.endsWith('/confirm'));
  expect(Object.keys(sent.body).sort()).toEqual(['card_version', 'gesture']);
  await expect(page.locator(T('tab-plans'))).toBeVisible();
  await screenshot(page, 'c-shell', 'judge-plan-subset', { testOnly: true });
});

test('@c-components task graph and bars draw from the view model only', async ({ page }) => {
  await open(page, 'customer-red-code');
  const drawn = await page.evaluate(async () => {
    const { dag } = await import('/static/js/components/dag.js');
    const { bars } = await import('/static/js/components/bars.js');
    const graph = dag({
      nodes: [
        { key: 'n0', label: '<b>gift</b>', status: 'settled', status_text: 'ok', tone: 'ok', layer: 0, order: 0 },
        { key: 'n1', label: 'dinner', status: 'pending', status_text: 'wait', tone: 'wait', layer: 1, order: 0 },
      ],
      edges: [['n0', 'n1']],
      blocked_text: null,
    }, { testid: 'plans-dag' });
    const chart = bars([{ label: 'food', value: '¥10.00', ratio: '0.25' }, { label: 'x', value: '1', ratio: 'NaN' }]);
    document.body.append(graph, chart);
    const fills = [...chart.querySelectorAll('.bar-fill')].map((el) => el.style.getPropertyValue('--ratio'));
    return {
      nodes: graph.querySelectorAll('.dag-node').length,
      edges: graph.querySelectorAll('.dag-edge').length,
      bold: graph.querySelectorAll('b').length,
      label: graph.querySelector('.dag-label').textContent,
      fills,
    };
  });
  expect(drawn).toEqual({ nodes: 2, edges: 1, bold: 0, label: '<b>gift</b>', fills: ['0.25', '0'] });
});

test('@c-components h() refuses unlisted attributes and foreign URLs', async ({ page }) => {
  await open(page, 'customer-red-code');
  const result = await page.evaluate(async () => {
    const { h } = await import('/static/js/dom.js');
    const tries = [
      () => h('a', { href: 'https://example.invalid/' }),
      () => h('a', { href: '//example.invalid/' }),
      () => h('img', { src: 'javascript:alert(1)' }),
      () => h('div', { onclick: 'x()' }),
      () => h('div', { style: 'color:red' }),
      () => h('div', { on: { click: 'x()' } }),
    ];
    const refused = tries.map((fn) => { try { fn(); return false; } catch { return true; } });
    const ok = h('a', { href: '/phone/abc', 'data-testid': 'x', 'aria-label': 'y' }, '<i>t</i>');
    return { refused, html: ok.outerHTML };
  });
  expect(result.refused).toEqual([true, true, true, true, true, true]);
  expect(result.html).toBe('<a href="/phone/abc" data-testid="x" aria-label="y">&lt;i&gt;t&lt;/i&gt;</a>');
});

test('@c-live the badge opens the live panel; unlock sends only a proof over a fresh nonce', async ({ page }) => {
  const fs = require('fs');
  const path = require('path');
  const state = withState('customer-red-code', (s) => {
    s.capabilities.live = true;
  });
  const live = {
    enabled: true, config_invalid: false, title: '规划来源', intro: '选择部署并解锁后可开启实时模型',
    off_text: null, deployments: [{ id: 'd1', label: '部署一（测试假模型）' }], selected: 'd1',
    unlock: { disabled: false, label: '访问码', button: '解锁', result_text: null }, revoked_text: null,
    toggle: { on: false, disabled: true, label: '实时模型' }, reason_text: null, boundary: true,
    boundary_text: null, caps_text: null, kernel_note: null, no_secret: null,
    badge: { kind: 'demo', text: '脚本演示' }, scope_note: null, notice: null,
  };
  const nonce = 'ab'.repeat(16);
  const base = { ok: true, generation: state.generation };
  const api = await serve(page, {
    state,
    live,
    posts: [
      { ...base, revision: state.revision + 1, nonce },
      { ...base, revision: state.revision + 2, result: 'accepted', live, state: { ...state.state, revision: state.revision + 2 } },
    ],
  });
  const hmac = path.join(process.env.CORRIDOR_E2E_ROOT || path.join(__dirname, '..', '..', '..'),
    'src', 'corridor', 'ui', 'live_access', 'hmac.js');
  await page.route('**/vendor/corridor-hmac.js', (route) =>
    route.fulfill({ status: 200, contentType: 'text/javascript', body: fs.readFileSync(hmac) }));
  await page.goto('/');
  const badge = page.locator(T('planner-badge'));
  await expect(badge).toBeVisible();
  await badge.click();
  const sheet = page.locator(T('live-sheet'));
  await expect(sheet).toBeVisible();
  await expect(page.getByRole('dialog')).toBeVisible();
  const code = page.locator(`${T('live-sheet')} ${T('live-code')}`);
  await code.fill('sentinel-access-code');
  await page.locator(T('live-unlock')).click();
  await expect(code).toHaveValue('');
  await expect.poll(() => api.requests.filter((r) => r.path === '/api/live/unlock').length).toBe(1);
  const paths = api.requests.filter((r) => r.method === 'POST' && r.path.startsWith('/api/live/')).map((r) => r.path);
  expect(paths).toEqual(['/api/live/challenge', '/api/live/unlock']);
  const unlock = api.requests.find((r) => r.path === '/api/live/unlock');
  expect(Object.keys(unlock.body).sort()).toEqual(['nonce', 'proof']);
  expect(unlock.body.nonce).toBe(nonce);
  expect(unlock.body.proof).toMatch(/^[0-9a-f]{64}$/);
  expect(JSON.stringify(api.requests.map((r) => r.body))).not.toContain('sentinel-access-code');
  await page.keyboard.press('Escape');
  await expect(sheet).toBeHidden();
});

test('@c-device business sources: request code, final confirm and a queued task with its way back', async ({ page }) => {
  const body = withState('customer-red-code', (s) => {
    const first = s.device.tasks[0];
    const wealth = JSON.parse(JSON.stringify(first));
    wealth.key = 'TB1';
    wealth.source = 'wealth';
    wealth.source_label = '理财';
    delete wealth.rkey;
    wealth.step = 'mfa_request';
    wealth.step_text = '获取验证码后在设备上输入';
    delete wealth.mfa;
    wealth.gesture = { op: 'mfa_request', token: 'Wm1hcmtfcmVxdWVzdF90b2', card_version: 'aaaaaaaaaaaaaaaa', label: '获取短信验证码（模拟）' };
    wealth.cards = wealth.cards.map((card) => ({ ...card, key: 'TB1-0' }));
    first.current = false;
    first.step = 'not_current';
    first.step_text = '请先回到那条回复';
    first.gesture = null;
    first.back = { rkey: 'R2', label: '回到那条回复' };
    s.device.tasks = [wealth, first];
    s.device.active = 'TB1';
    s.device.queue_text = '待确认队列：1 项';
  });
  const { api } = await open(page, body);
  const task = page.locator(T('device-task'));
  await expect(task).toHaveAttribute('data-source', 'wealth');
  await expect(page.locator(T('device-mfa-request'))).toHaveText('获取短信验证码（模拟）');
  await expect(page.locator(T('device-queue'))).toContainText('待确认队列：1 项');
  await page.locator(T('device-mfa-request')).click();
  await expect.poll(() => api.requests.filter((r) => r.path === '/api/device/tasks/TB1/mfa/request').length).toBe(1);
  const sent = api.requests.find((r) => r.path === '/api/device/tasks/TB1/mfa/request');
  expect(sent.body).toEqual({ gesture: 'Wm1hcmtfcmVxdWVzdF90b2', card_version: 'aaaaaaaaaaaaaaaa' });
});
