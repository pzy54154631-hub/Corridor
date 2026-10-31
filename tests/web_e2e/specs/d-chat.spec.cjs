// D-line screen specs (fixtures mode): conversation, replies, the two
// self-check questions, clarification, plan selection, receipts and
// failures. The page renders server state only; these specs check what
// is drawn and which request each control sends.
const { test, expect } = require('playwright/test');
const { serve } = require('../lib/fixtures.cjs');
const S = require('./d-states.cjs');

const T = (id) => `[data-testid="${id}"]`;

async function open(page, state, options = {}) {
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  const api = await serve(page, { state: S.ok(state), ...options });
  await page.goto('/');
  return { api, errors };
}

function posted(page, fragment) {
  return page.waitForRequest(
    (request) => request.method() === 'POST' && new URL(request.url()).pathname.endsWith(fragment),
  );
}

async function click(page, locator, fragment) {
  const [request] = await Promise.all([posted(page, fragment), locator.click()]);
  return { path: new URL(request.url()).pathname, body: JSON.parse(request.postData() || 'null') };
}

const reply = (page, rkey) => page.locator(`${T('response')}[data-rkey="${rkey}"]`);

test('@d-login enters the demo and shows the greeting with shortcuts', async ({ page }) => {
  const { errors } = await open(page, S.base({ loggedIn: false }), { posts: [S.ok(S.base())] });
  await expect(page.locator(T('composer-input'))).toBeHidden();
  const sent = await click(page, page.locator(T('login-enter')), '/api/session/login');
  expect(sent.body).toEqual({});
  await expect(page.locator(T('login-enter'))).toHaveCount(0);
  await expect(page.locator(T('composer-input'))).toBeVisible();
  for (const key of ['transfer', 'cards', 'scam_drill']) {
    await expect(page.locator(T(`shortcut-${key}`))).toBeVisible();
  }
  await expect(page.locator(T('drill-caption'))).toBeVisible();
  expect(errors).toEqual([]);
});

test('@d-composer sends on Enter, keeps Shift+Enter, attaches and clears on success', async ({ page }) => {
  await open(page, S.base(), { posts: [S.ok(S.yellow())] });
  const input = page.locator(T('composer-input'));
  await input.click();
  await input.pressSequentially('给妈妈转');
  await input.press('Shift+Enter');
  await input.pressSequentially('500元');
  await expect(input).toHaveValue('给妈妈转\n500元');
  await page.locator(T('composer-attach')).check();
  const [request] = await Promise.all([posted(page, '/api/chat/message'), input.press('Enter')]);
  const body = JSON.parse(request.postData());
  expect(body.text).toBe('给妈妈转\n500元');
  expect(body.external_files).toBe(1);
  expect(body.client_msg_id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
  await expect(reply(page, 'R1')).toBeVisible();
  await expect(input).toHaveValue('');
  await expect(page.locator(T('composer-attach'))).not.toBeChecked();
});

test('@d-composer keeps the draft when the service is busy', async ({ page }) => {
  await open(page, S.base(), {
    posts: [{ status: 503, body: { ok: false, error: { code: 'busy', message: 'busy', retry: true } } }],
  });
  const input = page.locator(T('composer-input'));
  await input.fill('给妈妈转500元');
  await click(page, page.locator(T('composer-send')), '/api/chat/message');
  await expect(input).toHaveValue('给妈妈转500元');
});

test('@d-shortcut posts the shortcut key', async ({ page }) => {
  await open(page, S.base(), { posts: [S.ok(S.yellow())] });
  const sent = await click(page, page.locator(T('shortcut-transfer')), '/api/chat/shortcut');
  expect(sent.body.shortcut).toBe('transfer');
  await expect(reply(page, 'R1')).toBeVisible();
});

test('@d-response yellow card: phone owner, notes outside the frame, cancel mirrors the device', async ({ page }) => {
  const state = S.yellow();
  await open(page, state);
  const r = reply(page, 'R1');
  await expect(r).toHaveAttribute('data-current', 'true');
  await expect(r).toHaveAttribute('data-status', 'awaiting_device');
  const phoneCard = r.locator(`${T('card')}[data-owner="phone"]`);
  await expect(phoneCard).toHaveCount(1);
  await expect(phoneCard).toHaveAttribute('data-tier', 'yellow');
  const facts = await phoneCard.locator(T('card-fact')).evaluateAll((rows) =>
    rows.map((row) => row.getAttribute('data-key')),
  );
  expect(facts).toEqual(state.chat.responses.R1.cards[0].facts.map((f) => f.key));
  await expect(r.locator(`${T('card-note')}[data-kind="fraud"]`)).toHaveCount(1);
  await expect(phoneCard.locator(T('card-note'))).toHaveCount(0);
  await expect(r.locator(T('note-fraud'))).toHaveCount(1);
  await expect(r.locator(T('device-confirm'))).toHaveCount(0);
  const sent = await click(page, r.locator(T('cancel-chat')), '/api/device/tasks/TR1/cancel');
  expect(sent.body).toEqual({});
});

test('@d-gate answers go to the server and the pause latches the radios', async ({ page }) => {
  const paused = S.gated('pause', { answers: { verified: 'no' } });
  paused.revision = 4;
  await open(page, S.gated('incomplete'), { posts: [S.ok(paused)] });
  const r = reply(page, 'R1');
  const gate = r.locator(T('gate'));
  await expect(gate).toHaveAttribute('data-decision', 'incomplete');
  await expect(gate).toHaveAttribute('data-interactive', 'true');
  await expect(r.locator(T('card')).locator(T('gate'))).toHaveCount(0);
  const sent = await click(page, gate.locator(T('gate-q-verified-no')), '/api/chat/responses/R1/gate/answer');
  expect(sent.body).toEqual({ gate_version: 'dScreensGateVer1', answers: { verified: 'no' } });
  await expect(gate).toHaveAttribute('data-decision', 'pause');
  await expect(gate.locator(T('gate-pause'))).toBeVisible();
  await expect(gate.locator(T('gate-q-verified-no'))).toBeChecked();
  for (const id of ['gate-q-verified-yes', 'gate-q-verified-no', 'gate-q-guided-no', 'gate-q-guided-yes']) {
    await expect(gate.locator(T(id))).toBeDisabled();
  }
  await expect(gate.locator(T('gate-self-report'))).toBeVisible();
  const retry = await click(page, gate.locator(T('gate-retry')), '/api/chat/responses/R1/gate/retry');
  expect(retry.body).toEqual({ gate_version: 'dScreensGateVer1' });
});

test('@d-gate R7 preset pauses at once and says so', async ({ page }) => {
  await open(page, S.gated('pause', { preset: true }, { view: 'judge' }));
  const gate = reply(page, 'R1').locator(T('gate'));
  await expect(gate).toHaveAttribute('data-preset', 'true');
  await expect(gate.locator(T('gate-pause'))).toContainText('预置');
  await expect(gate.locator(T('gate-q-guided-yes'))).toBeChecked();
  await expect(gate.locator(T('gate-retry'))).toBeEnabled();
});

test('@d-gate an earlier reply is read-only and offers the way back', async ({ page }) => {
  await open(page, S.earlier());
  const old = reply(page, 'R1');
  await expect(old).toHaveAttribute('data-current', 'false');
  const gate = old.locator(T('gate'));
  await expect(gate).toHaveAttribute('data-interactive', 'false');
  await expect(gate.locator(T('gate-q-verified-yes'))).toBeDisabled();
  await expect(gate.locator(T('gate-retry'))).toHaveCount(0);
  await expect(page.locator(T('gated-elsewhere'))).toBeVisible();
  const sent = await click(page, gate.locator(T('gate-back')), '/api/chat/responses/R1/current');
  expect(sent.body).toEqual({});
});

test('@d-gate controls are locked while a request is in flight', async ({ page }) => {
  await open(page, S.gated('incomplete'));
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  await page.route('**/gate/answer', async (route) => {
    await held;
    await route.fallback();
  });
  const gate = reply(page, 'R1').locator(T('gate'));
  await gate.locator(T('gate-q-verified-yes')).click();
  await expect(gate.locator(T('gate-q-guided-no'))).toBeDisabled();
  await expect(reply(page, 'R1').locator(T('cancel-chat'))).toBeDisabled();
  release();
  await expect(gate.locator(T('gate-q-guided-no'))).toBeEnabled();
});

test('@d-clarify review needs a pick, posts the choice, and locks afterwards', async ({ page }) => {
  const locked = S.clarify({ locked: true });
  locked.revision = 4;
  await open(page, S.clarify(), { posts: [S.ok(locked)] });
  const r = reply(page, 'R1');
  const review = r.locator(T('clarify-review'));
  await expect(review).toBeDisabled();
  await r.locator(T('clarify-choice-1')).check();
  await expect(review).toBeEnabled();
  const sent = await click(page, review, '/api/chat/responses/R1/recipient');
  expect(sent.body).toEqual({ choice: 'c1' });
  await expect(r.locator(T('clarify-locked'))).toBeVisible();
  await expect(r.locator(T('clarify-choice-0'))).toBeDisabled();
  await expect(r.locator(T('clarify-review'))).toBeDisabled();
});

test('@d-clarify on an earlier reply goes back first; cancel without a task stops', async ({ page }) => {
  await open(page, S.clarify({ current: false }));
  const r = reply(page, 'R1');
  await r.locator(T('clarify-choice-0')).check();
  await expect(r.locator(T('clarify-review'))).toBeDisabled();
  const back = await click(page, r.locator(T('response-back')), '/api/chat/responses/R1/current');
  expect(back.body).toEqual({});
  const stop = await click(page, r.locator(T('cancel-chat')), '/api/chat/stop');
  expect(stop.body).toEqual({});
});

test('@d-plan node selection is registered on the server, never filtered locally', async ({ page }) => {
  await open(page, S.planSubset());
  const r = reply(page, 'R1');
  await expect(r.locator(T('plan-node-0'))).toHaveAttribute('data-checked', 'true');
  await expect(r.locator(T('plan-node-1'))).toHaveAttribute('data-checked', 'false');
  await expect(r.locator(T('card'))).toHaveCount(3);
  const sent = await click(page, r.locator(T('plan-node-1')), '/api/device/tasks/TR1/nodes');
  expect(sent.body).toEqual({ card_version: 'fedcba9876543210', nodes: ['n0', 'n1'] });
});

test('@d-plan error text comes from the server', async ({ page }) => {
  const state = S.planSubset();
  state.chat.responses.R1.plan_select.mode = 'invalid';
  state.chat.responses.R1.plan_select.error_text = '只能从第一项起连续勾选';
  await open(page, state);
  await expect(reply(page, 'R1').locator(T('plan-error'))).toHaveText('只能从第一项起连续勾选');
});

test('@d-result receipt banner, receipt line and the task panel controls', async ({ page }) => {
  await open(page, S.done());
  const r = reply(page, 'R1');
  await expect(r.locator(T('result-banner'))).toHaveAttribute('data-tone', 'success');
  await expect(r.locator(T('receipt-line'))).toContainText('AB12CD34');
  await expect(r.locator(T('cancel-chat'))).toHaveCount(0);
  const panel = page.locator(T('task-panel'));
  await expect(panel).toBeVisible();
  const sent = await click(page, panel.locator(T('handoff')), '/api/chat/handoff');
  expect(sent.body).toEqual({});
});

test('@d-result phrase gate: fixed copy, no card, judge note only in judge view', async ({ page }) => {
  await open(page, S.phraseGate({ view: 'judge' }));
  const r = reply(page, 'R1');
  await expect(r.locator(T('card'))).toHaveCount(0);
  await expect(r.locator(T('phrase-gate-note'))).toBeVisible();
  await expect(page.locator(T('turn-assistant')).last()).toContainText('96110');
});

test('@d-result customer view carries no judge-only blocks', async ({ page }) => {
  await open(page, S.phraseGate());
  await expect(reply(page, 'R1').locator(T('phrase-gate-note'))).toHaveCount(0);
});

for (const kind of ['failed', 'unknown']) {
  test(`@d-failure ${kind}: failure block, no confirm prompt, cancel stays`, async ({ page }) => {
    await open(page, S.failure(kind));
    const r = reply(page, 'R1');
    await expect(r.locator(T('failure-block'))).toHaveAttribute('data-kind', kind);
    await expect(r.locator(T('device-open'))).toHaveCount(0);
    await expect(r.locator(T('cancel-chat'))).toBeEnabled();
  });
}

test('@d-route operator hint opens the masked snapshot', async ({ page }) => {
  const state = S.done();
  state.chat.responses.R1.route_hint = { target: 'operator', label: '打开坐席', note: '人工接管 · 自动执行暂停' };
  await open(page, state, { tabs: { operator: S.TABS.operator } });
  const hint = reply(page, 'R1').locator(T('route-hint'));
  await expect(hint).toHaveAttribute('data-target', 'operator');
  await hint.locator(T('operator-open')).click();
  await expect(page.locator(T('operator-snapshot'))).toContainText('合成客户 A');
  await expect(page.locator(T('operator-snapshot'))).toHaveCount(1);
});

test('@d-route a fallback hint is plain text, never a dead link', async ({ page }) => {
  const state = S.done();
  state.chat.responses.R1.route_hint = { target: 'fallback', label: '此项请在兜底界面办理', note: null };
  await open(page, state);
  const hint = reply(page, 'R1').locator(T('route-hint'));
  await expect(hint).toContainText('兜底界面');
  expect(await hint.evaluate((node) => node.tagName)).toBe('DIV');
  await expect(hint.getByRole('button')).toHaveCount(0);
  await expect(hint.locator('a')).toHaveCount(0);
});

test('@d-birthday candidates post the choice; disabled ones stay disabled', async ({ page }) => {
  const state = S.base({ capabilities: { plan_select: true, shortcuts: ['transfer', 'cards', 'scam_drill', 'birthday'] } });
  S.withTurn(
    state,
    'R1',
    '帮我准备老婆的生日',
    S.response('R1', {
      status: 'awaiting_input',
      status_text: '请选择',
      birthday: {
        lines: ['日历里有一位家人的生日'],
        choices: [
          { key: 'c0', label: '老婆 · 09-20', enabled: true },
          { key: 'c1', label: '妈妈 · 12-02', enabled: false },
        ],
        error_text: null,
      },
    }),
  );
  await open(page, state);
  const r = reply(page, 'R1');
  await expect(r.locator(T('birthday-choice-1'))).toBeDisabled();
  const sent = await click(page, r.locator(T('birthday-choice-0')), '/api/chat/responses/R1/birthday');
  expect(sent.body).toEqual({ choice: 'c0' });
});

test('@d-compensate appears only with the capability and posts its exact plan version', async ({ page }) => {
  const state = S.done({ capabilities: { compensate: true, plan_select: true, tabs: ['plans'] } });
  const version = '0123456789abcdef0123456789abcdef';
  state.chat.task_panel.controls.compensate = '退回补偿';
  state.chat.task_panel.controls.compensate_version = version;
  await open(page, state);
  const sent = await click(page, page.locator(T('task-compensate')), '/api/chat/compensate');
  expect(sent.body).toEqual({ plan_version: version });
});

test('@d-compensate is hidden when the server does not offer it', async ({ page }) => {
  const state = S.done();
  state.chat.task_panel.controls.compensate = '退回补偿';
  await open(page, state);
  await expect(page.locator(T('task-panel'))).toBeVisible();
  await expect(page.locator(T('task-compensate'))).toHaveCount(0);
});

test('@d-stop a system stop receipt is drawn as a system turn with its lines', async ({ page }) => {
  const state = S.yellow();
  const stopped = S.clone(state);
  stopped.revision += 1;
  const r1 = stopped.chat.responses.R1;
  r1.status = 'withdrawn';
  r1.status_text = '已撤回';
  r1.device_task = null;
  r1.cancel = null;
  stopped.chat.turns.push({ id: 'u2', role: 'user', rkey: 'R2', text: '（已点击“取消”）' });
  stopped.chat.turns.push({ id: 's2', role: 'system', rkey: 'R2', text: '已停止' });
  stopped.chat.responses.R2 = S.response('R2', {
    status: 'stopped',
    status_text: '已停止',
    copy_lines: ['已停止'],
    result: {
      banner: { tone: 'info', text: '已停止' },
      receipt_line: null,
      facts_lines: [],
      message_lines: [],
      stop_receipt_lines: ['未执行（已取消）：转账 ¥500.00', '撤回 1 张卡'],
      why: { lines: [] },
    },
  });
  stopped.chat.current = 'R2';
  stopped.device.tasks = [];
  stopped.device.active = null;
  await open(page, state, { posts: [S.ok(stopped)] });
  await click(page, reply(page, 'R1').locator(T('cancel-chat')), '/api/device/tasks/TR1/cancel');
  await expect(page.locator(T('turn-system'))).toHaveCount(1);
  await expect(reply(page, 'R1')).toHaveAttribute('data-status', 'withdrawn');
  await expect(reply(page, 'R2').locator(T('result-banner'))).toHaveAttribute('data-tone', 'info');
  await expect(reply(page, 'R2')).toContainText('撤回 1 张卡');
});

test('@d-gate the gated-elsewhere list leads back to the waiting reply', async ({ page }) => {
  await open(page, S.earlier());
  const list = page.locator(T('gated-elsewhere'));
  await expect(list.getByRole('button')).toHaveCount(1);
  const sent = await click(page, list.locator(T('gated-back-R1')), '/api/chat/responses/R1/current');
  expect(sent.body).toEqual({});
});

test('@d-layout a later marker turn for the same reply does not move its block', async ({ page }) => {
  const state = S.yellow();
  state.chat.turns.push({ id: 'm1', role: 'user', rkey: 'R1', text: '（已点击“取消”）' });
  await open(page, state);
  await expect(page.locator(T('turn-user'))).toHaveCount(2);
  const order = await page.locator(`${T('chat-log')} > *`).evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute('data-testid')),
  );
  expect(order).toEqual(['turn-user', 'response', 'turn-user']);
  await expect(reply(page, 'R1').locator(T('turn-assistant'))).toHaveCount(1);
});

test('@d-composer shows the pending text and a thinking line until the reply lands', async ({ page }) => {
  await open(page, S.base(), { posts: [S.ok(S.yellow())] });
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  await page.route('**/api/chat/message', async (route) => {
    await held;
    await route.fallback();
  });
  await page.locator(T('composer-input')).fill('给妈妈转500元');
  await page.locator(T('composer-send')).click();
  await expect(page.locator(T('chat-thinking'))).toBeVisible();
  await expect(page.locator(T('composer-send'))).toBeDisabled();
  release();
  await expect(reply(page, 'R1')).toBeVisible();
  await expect(page.locator(T('chat-thinking'))).toHaveCount(0);
});
