// Demo walkthrough for the video: drives the scripted demo through P0-core and the S7
// panel in presentation order, at 1920x1080 and a readable pace.
//
//   node tests/web_e2e/demo_walkthrough.cjs [--url http://127.0.0.1:N] [--headless]
//        [--slow 250] [--pause 1800] [--frames] [--video] [--only <scene,...>]
//
// By default the Edge window is shown at 1920x1080 for a screen recorder. --frames saves the
// page's own screencast as numbered JPEG frames with timestamps (frames.json) under
// .tmp/web-e2e/demo/frames-<n>/; --video uses Playwright's recorder, which needs its ffmpeg
// (not installed here, and this script never downloads it).
//
// Without --url it starts the test harness in scripted mode on a free loopback port, with the
// live panel offered but no model configured (the switch stays closed). Only the scripted
// planner is recorded here; nothing calls a model and nothing leaves loopback.
const fs = require('fs');
const path = require('path');
const Module = require('module');
const { spawn } = require('child_process');

const ROOT = path.resolve(__dirname, '..', '..');
const OUT = path.join(ROOT, '.tmp', 'web-e2e', 'demo');
const VERSION = '1.64.1';

function playwrightModules() {
  const override = process.env.CORRIDOR_PLAYWRIGHT_NODE_MODULES;
  const cache = path.join(process.env.LOCALAPPDATA || '', 'npm-cache', '_npx');
  const candidates = override
    ? [override]
    : fs.existsSync(cache)
      ? fs.readdirSync(cache).map((name) => path.join(cache, name, 'node_modules'))
      : [];
  for (const modules of candidates) {
    try {
      const pkg = JSON.parse(
        fs.readFileSync(path.join(modules, 'playwright', 'package.json'), 'utf-8'),
      );
      if (pkg.version === VERSION) return modules;
    } catch {
      // not this one
    }
  }
  throw new Error(`playwright ${VERSION} not found; set CORRIDOR_PLAYWRIGHT_NODE_MODULES`);
}

const modules = playwrightModules();
process.env.NODE_PATH = modules;
Module._initPaths();
const { chromium } = require('playwright');
const h = require('./lib/helpers.cjs');

const { T, SAY } = h;
// Later red cards stay within the synthetic balance left after the earlier scenes.
const RED_LATER = '给妈妈转3000元';

function args(argv) {
  const out = {
    url: null, headed: true, slow: 250, pause: 1800, frames: false, video: false, only: null,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === '--url') out.url = argv[++index];
    else if (value === '--headless') out.headed = false;
    else if (value === '--headed') out.headed = true;
    else if (value === '--frames') out.frames = true;
    else if (value === '--video') out.video = true;
    else if (value === '--slow') out.slow = Number(argv[++index]);
    else if (value === '--pause') out.pause = Number(argv[++index]);
    else if (value === '--only') out.only = argv[++index].split(',');
    else throw new Error(`unknown argument ${value}`);
  }
  return out;
}

async function startHarness() {
  fs.mkdirSync(OUT, { recursive: true });
  const ready = path.join(OUT, 'ready-demo.json');
  fs.rmSync(ready, { force: true });
  const python = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
  const log = fs.openSync(path.join(OUT, 'harness-demo.log'), 'w');
  const child = spawn(
    fs.existsSync(python) ? python : 'python',
    [
      '-m', 'tests.web_e2e.harness', '--port', '0', '--mode', 'scripted', '--ready', ready,
      '--features', 'live',
    ],
    { cwd: ROOT, env: { ...process.env, PYTHONPATH: ROOT }, stdio: ['ignore', log, log] },
  );
  for (let waited = 0; waited < 120_000; waited += 200) {
    if (child.exitCode !== null) throw new Error('harness exited; see harness-demo.log');
    if (fs.existsSync(ready)) {
      try {
        return { child, url: JSON.parse(fs.readFileSync(ready, 'utf-8')).url };
      } catch {
        // still being written
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  child.kill();
  throw new Error('harness not ready');
}

// Scenes in presentation order. Each one leaves the page at a calm resting point.
function scenes(page, rest) {
  const sendSlow = async (text) => {
    await page.locator(T('composer-input')).click();
    await page.locator(T('composer-input')).pressSequentially(text, { delay: 90 });
    await rest(0.4);
    const response = await h.clickAndWait(page, page.locator(T('composer-send')), '/api/chat/message');
    const body = await response.json();
    await page.locator(`${T('response')}[data-rkey="${body.rkey}"]`).waitFor();
    return body.rkey;
  };
  const confirm = async () => {
    await rest(0.6);
    return h.deviceConfirm(page);
  };
  const red = async () => {
    await h.requestCode(page);
    await rest(1);
    const code = await h.readSmsCode(page);
    const input = await h.codeInput(page);
    await input.click();
    await Promise.all([h.waitApi(page, '/mfa/verify'), input.pressSequentially(code, { delay: 220 })]);
    await rest(0.8);
    await confirm();
  };
  // Confirm on the device and, when the card is red, finish the simulated SMS step too.
  const finish = async () => {
    await confirm();
    if ((await h.deviceStep(page)) === 'mfa_request') await red();
  };
  return [
    ['login', async () => {
      await h.openApp(page);
      await rest(1.2);
      await h.login(page);
    }],
    ['green', async () => {
      await sendSlow(SAY.balance);
    }],
    ['yellow', async () => {
      await sendSlow(SAY.yellow);
      await confirm();
    }],
    ['red', async () => {
      await sendSlow(SAY.red);
      await confirm();
      await red();
    }],
    ['judge-evidence', async () => {
      await h.setView(page, 'judge');
      await rest(1);
      await page.locator(T('evidence-pane')).hover();
      await page.mouse.wheel(0, 400);
    }],
    ['new-payee-gate', async () => {
      await sendSlow(SAY.NEW);
      await rest(1);
      await h.answerGate(page, { verified: 'yes', guided: 'yes' });
      await rest(1.5);
      await h.retryGate(page);
      await h.answerGate(page, { verified: 'yes', guided: 'no' });
      await finish();
    }],
    ['fraud-opening', async () => {
      await sendSlow(SAY.FRAUD);
      await rest(1.5);
      await h.answerGate(page, { verified: 'no', guided: 'no' });
      await rest(1);
      await h.clickAndWait(page, page.locator(`${T('device-pane')} ${T('device-cancel')}`), '/cancel');
    }],
    ['r7-preset', async () => {
      await sendSlow(SAY.DIRECTED_NEW);
      await rest(1.5);
      await h.clickAndWait(page, page.locator(`${T('device-pane')} ${T('device-cancel')}`), '/cancel');
    }],
    ['phrase-gate', async () => {
      await sendSlow(SAY.HELD);
      await rest(1);
      await h.shortcut(page, 'scam_drill');
    }],
    ['clarify', async () => {
      const rkey = await sendSlow(SAY.CLARIFY_PLAIN);
      const reply = h.responseOf(page, rkey);
      await rest(0.8);
      await reply.locator(T('clarify-choice-0')).click();
      await h.clickAndWait(page, reply.locator(T('clarify-review')), '/recipient');
      if ((await h.deviceStep(page)) === 'gate_blocked') {
        await h.answerGate(page, { verified: 'yes', guided: 'no' });
      }
      await finish();
    }],
    ['cancel-stop-handoff', async () => {
      const rkey = await sendSlow(RED_LATER);
      await confirm();
      await h.requestCode(page);
      await rest(1);
      await h.clickAndWait(page, h.responseOf(page, rkey).locator(T('cancel-chat')), '/');
      await rest(1);
      await sendSlow(SAY.yellow);
      await sendSlow(SAY.stop);
      await sendSlow(SAY.yellow);
      await sendSlow(SAY.handoff);
      const open = page.locator(`${T('operator-open')}, ${T('route-hint')}[data-target="operator"]`);
      if (await open.count()) await open.first().click();
    }],
    ['lockout', async () => {
      // The hand-off above paused automation; start a new synthetic session first.
      await h.resetSession(page);
      await h.login(page);
      await sendSlow(SAY.red);
      await confirm();
      await h.requestCode(page);
      const code = await h.readSmsCode(page);
      const wrong = code.slice(0, 5) + String((Number(code[5]) + 1) % 10);
      for (let attempt = 0; attempt < 3 && (await h.deviceStep(page)) === 'mfa_code'; attempt += 1) {
        await h.enterCode(page, wrong);
        await rest(0.6);
      }
      await rest(1.5);
      await h.resetSession(page);
      await h.login(page);
      await sendSlow(SAY.yellow);
      await confirm();
    }],
    ['i18n', async () => {
      await h.setLocale(page, 'en');
      await rest(2);
      await h.setLocale(page, 'zh-CN');
    }],
    ['live-panel', async () => {
      // Scripted mode: the panel shows the switch closed (no model is configured here).
      await page.locator(T('planner-badge')).click();
      const opened = await page
        .locator(T('live-sheet'))
        .waitFor({ timeout: 5_000 })
        .then(() => true)
        .catch(() => false);
      if (!opened) {
        console.log('  live panel not offered by this build (capabilities.live is false)');
        return;
      }
      await rest(2.5);
      await page.keyboard.press('Escape');
    }],
    ['stop-all', async () => {
      await h.stopAll(page);
    }],
  ];
}

// The page's own screencast (CDP), one JPEG per painted frame, with its timestamp.
async function startFrames(context, page) {
  const dir = path.join(OUT, `frames-${process.pid}`);
  fs.mkdirSync(dir, { recursive: true });
  const index = [];
  const cdp = await context.newCDPSession(page);
  cdp.on('Page.screencastFrame', async ({ data, metadata, sessionId }) => {
    const name = `frame-${String(index.length).padStart(6, '0')}.jpg`;
    fs.writeFileSync(path.join(dir, name), Buffer.from(data, 'base64'));
    index.push({ name, timestamp: metadata.timestamp });
    await cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => null);
  });
  await cdp.send('Page.startScreencast', {
    format: 'jpeg', quality: 90, maxWidth: 1920, maxHeight: 1080, everyNthFrame: 1,
  });
  return async () => {
    await cdp.send('Page.stopScreencast').catch(() => null);
    fs.writeFileSync(path.join(dir, 'frames.json'), JSON.stringify(index), 'utf-8');
    console.log(`frames: ${index.length} in ${dir}`);
  };
}

async function main() {
  const options = args(process.argv.slice(2));
  let harness = null;
  let url = options.url;
  if (!url) {
    harness = await startHarness();
    url = harness.url;
  }
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({
    channel: 'msedge',
    headless: !options.headed,
    slowMo: options.slow,
    args: ['--window-size=1920,1080'],
  });
  const context = await browser.newContext({
    baseURL: url,
    viewport: { width: 1920, height: 1080 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    recordVideo: options.video ? { dir: OUT, size: { width: 1920, height: 1080 } } : undefined,
  });
  const page = await context.newPage();
  const stopFrames = options.frames ? await startFrames(context, page) : null;
  const rest = (seconds) => page.waitForTimeout(options.pause * seconds);
  const started = Date.now();
  let failed = null;
  try {
    for (const [name, run] of scenes(page, rest)) {
      if (options.only && !options.only.includes(name) && name !== 'login') continue;
      const at = ((Date.now() - started) / 1000).toFixed(1);
      console.log(`[${at}s] ${name}`);
      await run();
      await h.assertCornerOnTop(page);
      await rest(1);
    }
  } catch (error) {
    failed = error;
    const task = h.activeTask(page);
    if (await task.count().catch(() => 0)) {
      console.error(`device step: ${await task.getAttribute('data-step')}`);
      console.error((await task.innerText()).slice(0, 400));
    }
  } finally {
    if (stopFrames) await stopFrames();
    const video = page.video();
    await context.close();
    await browser.close();
    if (video) console.log(`video: ${await video.path()}`);
    if (harness) harness.child.kill();
  }
  if (failed) {
    console.error(failed);
    process.exitCode = 1;
  }
}

main();
