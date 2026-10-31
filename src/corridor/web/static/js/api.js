// HTTP layer: one state-changing request at a time, and generation/revision adoption rules
// (contract 1.5 and 7.6). GET requests skip the queue but obey the same adoption rules.

import { store } from "./store.js";
import { matches } from "./i18n.js";

const TAB_KEY = "corridor.web.tab";
const queue = [];
let running = false;
let generation = null;
let revision = -1;
// Counts adopted generations: a request remembers the count it left with, so a late
// stale_generation answer can never move the page back to an older generation.
let epoch = 0;
let tab = readTab();
let i18nWanted = null;
let hooks = { onError() {}, sessionBody() { return {}; } };

export function configure(next) {
  hooks = { ...hooks, ...next };
}

function readTab() {
  try {
    const value = window.sessionStorage.getItem(TAB_KEY);
    return value && /^[A-Za-z0-9_-]{32}$/.test(value) ? value : null;
  } catch {
    return null;
  }
}

export function currentTab() {
  return tab;
}

export function setTab(value) {
  tab = value;
  try {
    if (value) window.sessionStorage.setItem(TAB_KEY, value);
    else window.sessionStorage.removeItem(TAB_KEY);
  } catch {
    // Storage may be unavailable; the tab then lives for this page load only.
  }
}

export function currentGeneration() {
  return generation;
}

function headers(method, path) {
  const out = {};
  if (method === "POST") out["Content-Type"] = "application/json";
  if (path === "/api/session") {
    out["X-Corridor-Web"] = "1";
  } else {
    if (tab) out["X-Corridor-Tab"] = tab;
    if (generation) out["X-Corridor-Generation"] = generation;
  }
  return out;
}

async function send(method, path, body) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: headers(method, path),
      body: method === "POST" ? JSON.stringify(body || {}) : undefined,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch {
    return { status: 0, data: null, network: true };
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  return { status: response.status, data, network: false };
}

// Returns true when the response was adopted into the store.
function adopt(data, force) {
  if (!data || typeof data.generation !== "string" || typeof data.revision !== "number") return false;
  if (!force && (data.generation !== generation || data.revision < revision)) return false;
  const fresh = data.generation !== generation;
  if (fresh) epoch += 1;
  generation = data.generation;
  revision = data.revision;
  const patch = {};
  if (fresh) {
    patch.tabs = null;
    patch.evidence = null;
    patch.lab = null;
    patch.labPhone = null;
    patch.ui = { labOpen: false, labPhoneOpen: false, labPhoneBusy: false };
  }
  if (data.state) patch.state = data.state;
  if (data.live) patch.live = data.live;
  if (Object.prototype.hasOwnProperty.call(data, "lab")) {
    const previous = fresh ? null : store.lab;
    const next = data.lab;
    // The child state never replaces the banking session's AppState.
    const older = previous && next && previous.session_generation === next.session_generation &&
      typeof next.revision === "number" && next.revision < previous.revision;
    if (!older) {
      patch.lab = next;
      if (!previous || !next || previous.session_generation !== next.session_generation ||
          previous.revision !== next.revision) {
        patch.labPhone = null;
        patch.ui = { ...patch.ui, labPhoneOpen: false, labPhoneBusy: false };
      }
    }
  }
  store.set(patch);
  if (data.tab && data.tab.name) store.set({ tabs: { [data.tab.name]: data.tab } });
  if (data.state) ensureI18n(data.state);
  return true;
}

function ensureI18n(state) {
  if (matches(store.i18n, state)) return;
  const want = state.locale + "|" + state.view;
  if (i18nWanted === want) return;
  i18nWanted = want;
  get("/api/i18n").then((result) => {
    if (i18nWanted === want) i18nWanted = null;
    const data = result.data;
    if (result.ok && !result.stale && store.state && matches(data, store.state)) {
      store.set({ i18n: { locale: data.locale, view: data.view, strings: data.strings || {} } });
    }
  });
}

async function run(method, path, body, opts) {
  const sent = { generation, epoch };
  const { status, data, network } = await send(method, path, body);
  if (network) {
    hooks.onError({ code: "network", status: 0, message: null, path });
    return { ok: false, code: "network", status: 0, data: null };
  }
  if (data && data.ok === true) {
    if (opts.session && typeof data.tab === "string") setTab(data.tab);
    const adopted = adopt(data, Boolean(opts.force));
    // A response from an older revision or another generation is reported as stale.
    return { ok: true, code: null, status, data, stale: !adopted };
  }
  const error = (data && data.error) || {};
  const code = error.code || (status === 503 ? "busy" : "internal");
  // A stale_generation answer may move the page only if no other generation was adopted
  // since this request left; a delayed one is reported but never rolls the page back.
  const current = sent.epoch === epoch && data && data.generation !== sent.generation;
  if (data && data.state) adopt(data, code === "stale_generation" && current);
  if (status === 401 && !opts.session) {
    queue.unshift(sessionJob());
    pump();
  }
  hooks.onError({ code, status, message: error.message || null, path });
  return { ok: false, code, status, data };
}

function sessionJob() {
  return {
    path: "/api/session",
    body: () => ({ ...hooks.sessionBody(), ...(tab ? { tab } : {}) }),
    opts: { force: true, session: true },
    resolve() {},
  };
}

async function pump() {
  if (running) return;
  running = true;
  store.set({ ui: { inFlight: true } });
  while (queue.length) {
    const job = queue.shift();
    const body = typeof job.body === "function" ? job.body() : job.body;
    let result;
    try {
      result = await run("POST", job.path, body, job.opts || {});
    } catch {
      result = { ok: false, code: "internal", status: 0, data: null };
    }
    job.resolve(result);
  }
  running = false;
  store.set({ ui: { inFlight: false } });
}

// Every state-changing request goes through here, strictly one after another.
export function post(path, body, opts) {
  return new Promise((resolve) => {
    queue.push({ path, body, opts: opts || {}, resolve });
    pump();
  });
}

export function get(path) {
  return run("GET", path, undefined, {});
}

export function busy() {
  return running || queue.length > 0;
}
