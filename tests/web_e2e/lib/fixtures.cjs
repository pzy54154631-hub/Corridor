// Fixtures mode (contract 10): no server. Every request the page makes is answered through
// page.route: /api/* from fixtures/*.json, everything else from the static root.
//
//   const { serve, load } = require('../lib/fixtures.cjs');
//   const api = await serve(page, { state: 'customer-red-code', posts: ['err-card-changed'] });
//   await page.goto('/');
//   ... api.requests holds every /api/* request (method, path, headers, body).
//
// options:
//   state    fixture name, an Ok<{state}> response or a bare AppState; answers GET /api/state
//            and POST /api/session.
//   i18n     strings object; default: messages-web-ui.json for the state's locale.
//   tabs     {name: vm} for GET /api/tabs/{name}.
//   live     LiveVM for GET /api/live (default: the v0 shape, contract 5.3).
//   phone    PhonePayload for GET /api/phone/{pair} (default: paired, empty inbox).
//   evidence {rkey: EvidenceTechnical} for GET /api/evidence/responses/{rkey} (judge view).
//   posts    answers for the other POSTs, in order: fixture name, response object,
//            {status, body} or a function (request) => one of those. When the list runs out
//            the current state is echoed with revision + 1.
const fs = require('fs');
const path = require('path');

const FIXTURES = path.join(__dirname, '..', 'fixtures');
const STATIC_ROOT =
  process.env.CORRIDOR_E2E_STATIC_ROOT ||
  path.join(__dirname, '..', '..', '..', 'src', 'corridor', 'web', 'static');
const UI_CATALOG = path.join(STATIC_ROOT, '..', 'messages-web-ui.json');

const STATUS = {
  bad_json: 400, unknown_field: 400, no_session: 401, forbidden_origin: 403, not_found: 404,
  method_not_allowed: 405, stale_generation: 409, not_current: 409, gesture_unknown: 409,
  gesture_mismatch: 409, card_changed: 409, gesture_closed: 409, facts_incomplete: 409,
  gate_stale: 409, not_applicable: 409, msg_id_conflict: 409, not_at_boundary: 409,
  live_on: 409, live_unavailable: 409, too_large: 413, invalid_input: 422,
  operation_failed: 422, outcome_unknown: 500, internal: 500, busy: 503,
};

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
};

function load(name) {
  const file = path.join(FIXTURES, name.endsWith('.json') ? name : `${name}.json`);
  return JSON.parse(fs.readFileSync(file, 'utf-8'));
}

function list() {
  return fs.readdirSync(FIXTURES).filter((name) => name.endsWith('.json')).sort();
}

function asResponse(value) {
  if (typeof value === 'string') return asResponse(load(value));
  if (value && typeof value === 'object' && 'status' in value && 'body' in value) return value;
  if (value && value.ok === false) return { status: STATUS[value.error.code] || 500, body: value };
  if (value && value.ok === true) return { status: 200, body: value };
  if (value && value.schema === 1) {
    return {
      status: 200,
      body: { ok: true, generation: value.generation, revision: value.revision, state: value },
    };
  }
  throw new Error('fixtures: unrecognised response');
}

function catalogStrings(locale) {
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(UI_CATALOG, 'utf-8'));
  } catch {
    return {};
  }
  const strings = {};
  for (const [key, value] of Object.entries(raw)) {
    if (value && typeof value === 'object') {
      const text = value[locale] ?? value['zh-CN'];
      if (typeof text === 'string') strings[key] = text;
    } else if (typeof value === 'string') {
      strings[key] = value;
    }
  }
  return strings;
}

function liveV0(state) {
  return {
    enabled: false, config_invalid: false, title: '', intro: '',
    off_text: state.locale === 'en' ? 'Demonstrate the live model switch in the fallback UI'
      : '实时模型开关请在兜底界面演示',
    deployments: [], selected: null,
    unlock: { disabled: true, label: '', button: '', result_text: null },
    revoked_text: null,
    toggle: { on: false, disabled: true, label: '' },
    reason_text: null, boundary: true, boundary_text: null,
    caps_text: null, kernel_note: null, no_secret: null,
    badge: state.shell.badge, scope_note: null, notice: null,
  };
}

function staticFile(urlPath) {
  let relative = decodeURIComponent(urlPath);
  if (relative === '/' || relative === '') relative = '/index.html';
  if (/^\/phone\/[A-Za-z0-9_-]{16,64}$/.test(relative)) relative = '/phone.html';
  const candidates = [relative, relative.replace(/^\/static\//, '/')];
  for (const candidate of candidates) {
    const file = path.normalize(path.join(STATIC_ROOT, candidate));
    if (!file.startsWith(path.normalize(STATIC_ROOT))) return null;
    if (fs.existsSync(file) && fs.statSync(file).isFile()) return file;
  }
  return null;
}

async function serve(page, options = {}) {
  let current = asResponse(options.state || 'customer-red-code').body;
  if (!current.state) throw new Error('fixtures: options.state must carry an AppState');
  const posts = [...(options.posts || [])];
  const handle = { requests: [], get state() { return current.state; } };
  const json = (route, status, body) =>
    route.fulfill({
      status,
      contentType: 'application/json; charset=utf-8',
      headers: { 'Cache-Control': 'no-store' },
      body: JSON.stringify(body),
    });

  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const apiPath = url.pathname;
    if (!apiPath.startsWith('/api/')) {
      const file = staticFile(apiPath);
      if (!file) return route.fulfill({ status: 404, body: 'not found' });
      const type = TYPES[path.extname(file)] || 'application/octet-stream';
      return route.fulfill({ status: 200, contentType: type, body: fs.readFileSync(file) });
    }
    let body = null;
    if (method === 'POST') {
      try {
        body = JSON.parse(request.postData() || 'null');
      } catch {
        body = request.postData();
      }
    }
    handle.requests.push({ method, path: apiPath, headers: request.headers(), body });
    const state = current.state;
    const base = { ok: true, generation: state.generation, revision: state.revision };

    if (method === 'GET') {
      if (apiPath === '/api/state') return json(route, 200, current);
      if (apiPath === '/api/i18n') {
        const strings = options.i18n || catalogStrings(state.locale);
        return json(route, 200, { ...base, locale: state.locale, view: state.view, strings });
      }
      if (apiPath.startsWith('/api/tabs/')) {
        const name = apiPath.slice('/api/tabs/'.length);
        const vm = options.tabs && options.tabs[name];
        if (!vm) return json(route, 404, notFound(state));
        return json(route, 200, { ...base, tab: { name, revision: state.revision, vm } });
      }
      if (apiPath === '/api/live') {
        return json(route, 200, { ...base, live: options.live || liveV0(state) });
      }
      if (apiPath.startsWith('/api/evidence/responses/')) {
        const rkey = apiPath.split('/').pop();
        if (state.view !== 'judge') return json(route, 200, { ...base, available: false });
        const technical = (options.evidence && options.evidence[rkey]) || {
          envelope: {}, cards: [], kernel: null, gate_record: null,
        };
        return json(route, 200, { ...base, available: true, technical });
      }
      if (apiPath.startsWith('/api/phone/')) {
        return json(route, 200, options.phone || phonePayload(state));
      }
      return json(route, 404, notFound(state));
    }

    if (method !== 'POST') return json(route, 405, err('method_not_allowed'));
    if (apiPath === '/api/session') {
      return json(route, 200, { ...current, tab: 'E2eFixtureTab_0123456789abcdefgh' });
    }
    let next = posts.length ? posts.shift() : null;
    if (typeof next === 'function') next = next(handle.requests[handle.requests.length - 1]);
    let response;
    if (next) {
      response = asResponse(next);
    } else {
      const revision = state.revision + 1;
      response = {
        status: 200,
        body: { ok: true, generation: state.generation, revision, state: { ...state, revision } },
      };
    }
    if (response.body && response.body.state) current = { ...response.body, ok: true };
    return json(route, response.status, response.body);
  });
  return handle;
}

function err(code) {
  return { ok: false, error: { code, message: code, retry: false } };
}

function notFound(state) {
  return {
    ...err('not_found'),
    generation: state.generation,
    revision: state.revision,
    state,
  };
}

function phonePayload(state) {
  return {
    ok: true,
    version: 'fixture-empty',
    title: '', status_bar: '', corner: state.shell.corner,
    messages: [], empty: '', unpaired: null, footer: '',
  };
}

module.exports = { serve, load, list, asResponse, STATUS };
