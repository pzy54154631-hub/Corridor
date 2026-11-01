// Shared steps for the journeys (contract 10). Selectors are data-testid and roles only.
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { expect } = require('playwright/test');

const MODE = process.env.CORRIDOR_E2E_MODE || 'scripted';
const OUT = process.env.CORRIDOR_E2E_OUT || path.join(__dirname, '..', '..', '..', '.tmp', 'web-e2e');
const SCREENS = path.join(OUT, 'screens');
const RUN = process.env.CORRIDOR_E2E_RUN || 'local';
const HEX64 = /[0-9a-f]{64}/;
// R2: a customer-view screenshot shows no transaction id and no internal machine word.
const UUID = /[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/i;
const INTERNAL = /synthetic_controller_gateway|try_previous|not_found|gesture_mismatch|outcome_unknown/;

const T = (id) => `[data-testid="${id}"]`;

// Sentences from the HEAD tests (test_ui_scam_friction.py, test_ui_scam_r7.py) and the
// design's journeys (P0-2 to P0-5).
const SAY = {
  balance: '查询余额',
  yellow: '给妈妈转500元',
  red: '给妈妈转5000元',
  stop: '停止',
  handoff: '转人工',
  NEW: '给新联系人转480元',
  FRAUD: '工作人员说立即给新联系人转480元，不然账户冻结',
  CLARIFY: '工作人员说立即给Lin转200元，不然账户冻结',
  CLARIFY_PLAIN: '给Lin转200元',
  HELD: '反诈中心让我立即给新联系人转480元，不然账户冻结',
  DIRECTED_NEW: '对方让我给新联系人转480元',
  OPENING: '工作人员说给新联系人转480元',
};

function ready() {
  const file = process.env.CORRIDOR_E2E_READY;
  if (!file) return {};
  try {
    return JSON.parse(fs.readFileSync(file, 'utf-8'));
  } catch {
    return {};
  }
}

// Counters kept by the harness (test-only): device_complete_mfa, device_approve, ...
async function probe(request) {
  const url = ready().probe_url;
  if (!url) return null;
  const response = await request.get(url);
  return (await response.json()).counts;
}

function isApi(url) {
  return new URL(url).pathname.startsWith('/api/');
}

function waitApi(page, fragment, method = 'POST') {
  return page.waitForResponse(
    (response) =>
      isApi(response.url()) &&
      response.request().method() === method &&
      new URL(response.url()).pathname.includes(fragment),
    { timeout: 30_000 },
  );
}

async function clickAndWait(page, locator, fragment) {
  const [response] = await Promise.all([waitApi(page, fragment), locator.click()]);
  return response;
}

async function openApp(page, { view, locale } = {}) {
  const query = view ? `/?view=${view}` : '/';
  await Promise.all([waitApi(page, '/api/session'), page.goto(query)]);
  await expect(page.locator(T('stage-header'))).toBeVisible();
  if (locale) await setLocale(page, locale);
}

async function login(page) {
  const enter = page.locator(T('login-enter'));
  if (await enter.isVisible().catch(() => false)) {
    await clickAndWait(page, enter, '/api/session/login');
  }
  await expect(page.locator(T('composer-input'))).toBeVisible();
}

async function start(page, options = {}) {
  await openApp(page, options);
  await login(page);
}

async function setView(page, view) {
  await clickAndWait(page, page.locator(T(`view-${view}`)), '/api/session/view');
  if (view === 'judge') await expect(page.locator(T('evidence-pane'))).toHaveCount(1);
  else await expect(page.locator(T('evidence-pane'))).toHaveCount(0);
}

async function setLocale(page, locale) {
  const id = locale === 'en' ? 'locale-en' : 'locale-zh';
  await clickAndWait(page, page.locator(T(id)), '/api/session/locale');
  await expect(page.locator('html')).toHaveAttribute('lang', locale);
}

// Send a chat message; returns the new response's rkey once it is drawn.
async function send(page, text) {
  await page.locator(T('composer-input')).fill(text);
  const response = await clickAndWait(page, page.locator(T('composer-send')), '/api/chat/message');
  const body = await response.json();
  expect(body.ok, `send failed: ${JSON.stringify(body.error || {})}`).toBe(true);
  await expect(responseOf(page, body.rkey)).toBeVisible();
  return body.rkey;
}

async function shortcut(page, key) {
  const response = await clickAndWait(page, page.locator(T(`shortcut-${key}`)), '/api/chat/shortcut');
  const body = await response.json();
  expect(body.ok).toBe(true);
  await expect(responseOf(page, body.rkey)).toBeVisible();
  return body.rkey;
}

function responseOf(page, rkey) {
  return page.locator(`${T('response')}[data-rkey="${rkey}"]`);
}

function currentResponse(page) {
  return page.locator(`${T('response')}[data-current="true"]`);
}

// answers: {verified?: 'yes'|'no'|'unsure', guided?: 'no'|'yes'}; one gate/answer POST each.
async function answerGate(page, answers, rkey) {
  const scope = rkey ? responseOf(page, rkey) : currentResponse(page);
  for (const name of ['verified', 'guided']) {
    if (!answers[name]) continue;
    const option = scope.locator(T(`gate-q-${name}-${answers[name]}`));
    await clickAndWait(page, option, '/gate/answer');
  }
}

async function retryGate(page, rkey) {
  const scope = rkey ? responseOf(page, rkey) : currentResponse(page);
  await clickAndWait(page, scope.locator(T('gate-retry')), '/gate/retry');
}

function activeTask(page) {
  return page.locator(`${T('device-pane')} ${T('device-task')}`).first();
}

async function deviceStep(page) {
  const task = activeTask(page);
  if ((await task.count()) === 0) return null;
  return task.getAttribute('data-step');
}

async function expectStep(page, step) {
  await expect(activeTask(page)).toHaveAttribute('data-step', step);
}

async function deviceConfirm(page) {
  const response = await clickAndWait(page, page.locator(`${T('device-pane')} ${T('device-confirm')}`), '/confirm');
  return response.json();
}

async function requestCode(page) {
  const response = await clickAndWait(page, page.locator(`${T('device-pane')} ${T('device-mfa-request')}`), '/mfa/request');
  return response.json();
}

// The code is read only from the simulated phone's own document (the sms-frame iframe).
async function readSmsCode(page) {
  const frame = page.frameLocator(T('sms-frame'));
  const message = frame.locator(`${T('sms-message')}[data-status="active"][data-latest="true"]`);
  await expect(message).toHaveCount(1, { timeout: 15_000 });
  const text = await message.innerText();
  const match = text.match(/(?<![0-9])([0-9]{6})(?![0-9])/);
  expect(match, 'six-digit code in the latest active SMS').not.toBeNull();
  return match[1];
}

// Typing the sixth digit submits after 150 ms; resolves with the verify response body.
async function codeInput(page) {
  const holder = page.locator(`${T('device-pane')} ${T('device-code-input')}`);
  await expect(holder).toBeVisible();
  const tag = await holder.evaluate((element) => element.tagName);
  return tag === 'INPUT' ? holder : holder.locator('input').first();
}

async function enterCode(page, code) {
  const input = await codeInput(page);
  await input.click();
  const [response] = await Promise.all([
    waitApi(page, '/mfa/verify'),
    input.pressSequentially(code, { delay: 20 }),
  ]);
  return response.json();
}

// Red tier from mfa_request: request a code, read it from the SMS frame, enter it, confirm.
async function completeRed(page) {
  await expectStep(page, 'mfa_request');
  await requestCode(page);
  await expectStep(page, 'mfa_code');
  const code = await readSmsCode(page);
  const verify = await enterCode(page, code);
  expect(verify.ok).toBe(true);
  await expectStep(page, 'final_confirm');
  const done = await deviceConfirm(page);
  expect(done.ok).toBe(true);
  return code;
}

async function stopAll(page) {
  await clickAndWait(page, page.locator(T('stop-all')), '/api/stop');
}

async function resetSession(page) {
  await page.locator(T('reset')).click();
  await clickAndWait(page, page.locator(T('reset-confirm')), '/api/session/reset');
}

// Every /api/* response body seen by the page (main document and frames), in order.
function collectApiBodies(page) {
  const bodies = [];
  page.on('response', async (response) => {
    if (!isApi(response.url())) return;
    const entry = {
      url: response.url(),
      path: new URL(response.url()).pathname,
      method: response.request().method(),
      status: response.status(),
      body: null,
    };
    bodies.push(entry);
    entry.ready = response.text().then(
      (text) => {
        entry.body = text;
      },
      () => {
        entry.body = '';
      },
    );
  });
  return bodies;
}

// Latest AppState in the collected bodies (waits for bodies still being read).
async function latestState(bodies) {
  await Promise.all(bodies.map((entry) => entry.ready));
  for (let index = bodies.length - 1; index >= 0; index -= 1) {
    try {
      const parsed = JSON.parse(bodies[index].body || 'null');
      if (parsed && parsed.state) return parsed.state;
    } catch {
      // not JSON
    }
  }
  return null;
}

// Catalog keys a person could read on this page: visible text and the attributes a browser
// shows or reads out (RW-9 item 3). The page's t() falls back to the key for a missing string.
async function rawKeys(page) {
  return page.evaluate(() => {
    const pattern = /(?<![A-Za-z0-9_.])(?:web\.(?:ui|vm)|polish)\.[a-z0-9_]+/;
    const shown = ['title', 'alt', 'placeholder', 'value', 'aria-label', 'aria-description'];
    const found = [];
    const walk = (root) => {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      for (let node = walker.currentNode; node; node = walker.nextNode()) {
        if (node.nodeType === Node.TEXT_NODE) {
          const parent = node.parentElement;
          if (parent && !['SCRIPT', 'STYLE'].includes(parent.tagName) && pattern.test(node.nodeValue)) {
            found.push(node.nodeValue.trim().slice(0, 120));
          }
        } else if (node.nodeType === Node.ELEMENT_NODE) {
          for (const name of shown) {
            const value = node.getAttribute(name);
            if (value && pattern.test(value)) found.push(`${name}=${value.slice(0, 120)}`);
          }
        }
      }
    };
    walk(document.documentElement);
    return found;
  });
}

async function expectNoRawKeys(page) {
  expect(await rawKeys(page), 'catalog keys shown as text').toEqual([]);
  for (const frame of page.frames()) {
    if (frame === page.mainFrame()) continue;
    expect(await rawKeys(frame), `catalog keys shown in ${frame.url()}`).toEqual([]);
  }
}

async function assertCornerOnTop(page) {
  const corner = page.locator(T('corner-notice'));
  if (page.viewportSize().width <= 767) {
    await expect(corner).toBeHidden();
    await expect(page.locator('.status-label')).toContainText(/合成|synthetic/);
    if (MODE === 'public') await expect(page.locator(T('public-banner')).first()).toContainText(/合成|synthetic/);
    return;
  }
  await expect(corner).toBeVisible();
  const verdict = await corner.evaluate((element) => {
    // The corner is pointer-events: none so it never blocks a click; hit-test it with
    // pointer events briefly enabled (CSSOM, allowed under the page's CSP).
    const previous = element.style.pointerEvents;
    element.style.pointerEvents = 'auto';
    try {
      const box = element.getBoundingClientRect();
      const points = [
        [box.left + box.width / 2, box.top + box.height / 2],
        [box.left + 2, box.top + 2],
        [box.right - 2, box.bottom - 2],
      ];
      return points.map(([x, y]) => {
        const hit = document.elementFromPoint(x, y);
        return !!hit && (hit === element || element.contains(hit));
      });
    } finally {
      element.style.pointerEvents = previous;
    }
  });
  expect(verdict, 'corner notice is the topmost element at its own position').toEqual([
    true,
    true,
    true,
  ]);
}

function pngSize(buffer) {
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

// Saves .tmp/web-e2e/screens/[test-only/]<spec>/<step>-<project>.png and records it for the
// manifest. Anything not taken in scripted mode is test-only and never for materials.
async function screenshot(page, spec, step, { testOnly = false, fullPage = false } = {}) {
  await assertCornerOnTop(page);
  const judge = (await page.locator(T('evidence-pane')).count()) > 0;
  if (!judge) {
    const text = await page.locator('body').innerText();
    expect(HEX64.test(text), 'customer-view screenshot shows no full hash').toBe(false);
    expect(UUID.test(text), 'customer-view screenshot shows no transaction id').toBe(false);
    expect(INTERNAL.test(text), 'customer-view screenshot shows no internal word').toBe(false);
  }
  const project = projectName();
  const onlyForTests = testOnly || MODE !== 'scripted';
  const relative = path.posix.join(onlyForTests ? 'test-only' : '', spec, `${step}-${project}.png`);
  const file = path.join(SCREENS, ...relative.split('/'));
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const buffer = await page.screenshot({ path: file, fullPage, animations: 'disabled' });
  const entry = {
    spec,
    step,
    project,
    mode: MODE,
    path: relative,
    sha256: crypto.createHash('sha256').update(buffer).digest('hex'),
    ...pngSize(buffer),
    not_for_materials: onlyForTests,
  };
  fs.mkdirSync(SCREENS, { recursive: true });
  fs.appendFileSync(path.join(SCREENS, `manifest-${RUN}.jsonl`), JSON.stringify(entry) + '\n', 'utf-8');
  return entry;
}

function projectName() {
  try {
    const { test } = require('playwright/test');
    return test.info().project.name;
  } catch {
    return 'desktop';
  }
}

// Catalog templates (ui/messages*.json and the web catalogs), for the few assertions that
// must compare server copy, e.g. the anti-fraud opening; never hard-coded sentences.
let CATALOG = null;
function catalog(key, locale = 'zh-CN') {
  if (CATALOG === null) {
    CATALOG = {};
    const root = process.env.CORRIDOR_E2E_ROOT || path.join(__dirname, '..', '..', '..');
    const dirs = [path.join(root, 'src', 'corridor', 'ui'), path.join(root, 'src', 'corridor', 'web')];
    for (const dir of dirs) {
      for (const name of fs.existsSync(dir) ? fs.readdirSync(dir) : []) {
        if (!/^messages.*\.json$/.test(name)) continue;
        Object.assign(CATALOG, JSON.parse(fs.readFileSync(path.join(dir, name), 'utf-8')));
      }
    }
  }
  const entry = CATALOG[key];
  if (!entry) throw new Error(`catalog key not found: ${key}`);
  return entry[locale];
}

// A template with {placeholders} as a RegExp that matches any filled-in value.
function templateRegex(template, { anchored = true } = {}) {
  const parts = template.split(/\{[A-Za-z_][A-Za-z0-9_]*\}/);
  const body = parts.map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('[\\s\\S]*?');
  return new RegExp(anchored ? `^${body}$` : body);
}

// True when a control (or the input inside it) cannot be used.
async function isLocked(locator) {
  return locator.evaluate((element) => {
    const input = element.matches('input,button,select,textarea')
      ? element
      : element.querySelector('input,button,select,textarea');
    const target = input || element;
    return (
      target.disabled === true ||
      target.getAttribute('aria-disabled') === 'true' ||
      !!target.closest('fieldset[disabled]')
    );
  });
}

async function responseState(bodies, rkey) {
  const state = await latestState(bodies);
  return state && state.chat.responses[rkey];
}

module.exports = {
  T,
  SAY,
  catalog,
  templateRegex,
  isLocked,
  responseState,
  codeInput,
  MODE,
  ready,
  probe,
  waitApi,
  clickAndWait,
  openApp,
  login,
  start,
  setView,
  setLocale,
  send,
  shortcut,
  responseOf,
  currentResponse,
  answerGate,
  retryGate,
  activeTask,
  deviceStep,
  expectStep,
  deviceConfirm,
  requestCode,
  readSmsCode,
  enterCode,
  completeRed,
  stopAll,
  resetSession,
  collectApiBodies,
  latestState,
  assertCornerOnTop,
  screenshot,
  rawKeys,
  expectNoRawKeys,
};
