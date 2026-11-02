// User intents mapped onto endpoints (contract 7.4). Every state-changing call is queued in
// api.js; gestures carry the token and card version the user was looking at when they clicked.

import { currentTab, get, post, setTab } from "./api.js";
import { deriveVerifier } from "./kdf.js";
import { store } from "./store.js";

const enc = encodeURIComponent;
let pendingMessage = null;
let firstView = undefined;
let hmacLoad = null;

function messageId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function urlView() {
  if (firstView === undefined) {
    const value = new URLSearchParams(window.location.search).get("view");
    firstView = value === "customer" || value === "judge" ? value : null;
  }
  return firstView;
}

export function sessionBody() {
  const view = urlView();
  return view ? { view } : {};
}

function task(tkey) {
  const tasks = (store.state && store.state.device && store.state.device.tasks) || [];
  return tasks.find((item) => item.key === tkey) || null;
}

function done(result) {
  return Boolean(result && result.ok);
}

export async function openSession(opts) {
  const body = { ...sessionBody(), ...(opts && opts.view ? { view: opts.view } : {}) };
  const tab = opts && opts.fresh ? null : currentTab();
  if (tab) body.tab = tab;
  else setTab(null);
  return done(await post("/api/session", body, { force: true, session: true }));
}

export async function login() {
  return done(await post("/api/session/login", {}));
}

export async function setView(view) {
  return done(await post("/api/session/view", { view }));
}

export async function setLocale(locale) {
  return done(await post("/api/session/locale", { locale }));
}

export async function reset() {
  pendingMessage = null;
  const result = await post("/api/session/reset", {}, { force: true });
  if (done(result)) {
    store.set({ ui: { tab: "assistant", deviceOpen: false, draft: { text: "", attach: false } } });
  }
  return done(result);
}

// client_msg_id is reused only when the same text is sent again after a failed attempt.
export async function send(text, external) {
  const files = external ? 1 : 0;
  if (!pendingMessage || pendingMessage.text !== text || pendingMessage.files !== files) {
    pendingMessage = { text, files, id: messageId() };
  }
  const attempt = pendingMessage;
  store.set({ ui: { draft: { text, attach: Boolean(external) } } });
  const result = await post("/api/chat/message", {
    text,
    external_files: files,
    client_msg_id: attempt.id,
  });
  if (done(result) || (result && result.status >= 400 && result.status < 500 && result.status !== 409)) {
    if (pendingMessage === attempt) pendingMessage = null;
  }
  if (done(result)) store.set({ ui: { draft: { text: "", attach: false } } });
  return done(result);
}

export async function shortcut(key) {
  return done(await post("/api/chat/shortcut", { shortcut: key, client_msg_id: messageId() }));
}

export async function makeCurrent(rkey) {
  return done(await post(`/api/chat/responses/${enc(rkey)}/current`, {}));
}

export async function recipient(rkey, choice) {
  return done(await post(`/api/chat/responses/${enc(rkey)}/recipient`, { choice }));
}

export async function gateAnswer(rkey, version, answers) {
  return done(
    await post(`/api/chat/responses/${enc(rkey)}/gate/answer`, { gate_version: version, answers }),
  );
}

export async function gateRetry(rkey, version) {
  return done(await post(`/api/chat/responses/${enc(rkey)}/gate/retry`, { gate_version: version }));
}

export async function birthday(rkey, choice) {
  return done(await post(`/api/chat/responses/${enc(rkey)}/birthday`, { choice }));
}

export async function aa(rkey, choices) {
  return done(await post(`/api/chat/responses/${enc(rkey)}/aa`, { choices }));
}

export async function chatStop() {
  return done(await post("/api/chat/stop", {}));
}

export async function handoff() {
  return done(await post("/api/chat/handoff", {}));
}

// Compensation names the plan the panel showed; a newer plan makes the version stale (409).
export async function compensate(version) {
  if (typeof version !== "string") return false;
  return done(await post("/api/chat/compensate", { plan_version: version }));
}

export async function selectNodes(tkey, nodesVersion, nodeKeys) {
  return done(
    await post(`/api/device/tasks/${enc(tkey)}/nodes`, {
      card_version: nodesVersion,
      nodes: [...nodeKeys],
    }),
  );
}

function gesture(tkey, op) {
  const item = task(tkey);
  const g = item && item.gesture;
  return g && g.op === op ? { gesture: g.token, card_version: g.card_version } : null;
}

export async function confirm(tkey) {
  const body = gesture(tkey, "confirm");
  if (!body) return false;
  return done(await post(`/api/device/tasks/${enc(tkey)}/confirm`, body));
}

export async function mfaRequest(tkey) {
  const body = gesture(tkey, "mfa_request");
  if (!body) return false;
  return done(await post(`/api/device/tasks/${enc(tkey)}/mfa/request`, body));
}

export async function mfaVerify(tkey, code) {
  const body = gesture(tkey, "mfa_verify");
  if (!body || !/^[0-9]{6}$/.test(String(code))) return false;
  return done(await post(`/api/device/tasks/${enc(tkey)}/mfa/verify`, { ...body, code: String(code) }));
}

export async function cancel(tkey) {
  return done(await post(`/api/device/tasks/${enc(tkey)}/cancel`, {}));
}

export async function stopAll() {
  return done(await post("/api/stop", {}));
}

export async function loadTab(name) {
  const result = await get(`/api/tabs/${enc(name)}`);
  return done(result) && !result.stale;
}

export async function subsQuery() {
  return done(await post("/api/subscriptions/query", {}));
}

// A business choice carries the version of the list it was picked from.
export async function subsCancel(choice, version) {
  return done(await post("/api/subscriptions/cancel", { choice, list_version: version }));
}

export async function subscriptionChoice(rkey, choice, version) {
  return done(await post(`/api/chat/responses/${enc(rkey)}/subscription`, {
    choice, list_version: version,
  }));
}

export async function scheduleCancelChoice(rkey, choice, version) {
  return done(await post(`/api/chat/responses/${enc(rkey)}/schedule-cancel`, {
    choice, list_version: version,
  }));
}

// A model suggestion: the server sends its own sentence; the page sends only the choice.
export async function suggestionChoice(rkey, choice, version) {
  return done(await post(`/api/chat/responses/${enc(rkey)}/suggestion`, {
    choice, list_version: version, client_msg_id: messageId(),
  }));
}

export async function wealthCompare(objective) {
  return done(await post("/api/wealth/compare", { objective }));
}

export async function cardPreview(body) {
  return done(await post("/api/cards/preview", body));
}

export async function riskSubmit(answers, version) {
  return done(await post("/api/wealth/risk", { answers, version }));
}

export async function riskRetake(version) {
  return done(await post("/api/wealth/risk/retake", { version }));
}

export async function wealthPurchase(choice, version) {
  return done(
    await post("/api/wealth/purchase", {
      choice: choice === undefined ? null : choice,
      list_version: version,
    }),
  );
}

export async function wealthRedeem(choice, version) {
  return done(await post("/api/wealth/redeem", { choice, list_version: version }));
}

export async function presetR4() {
  return done(await post("/api/presets/suitability-r4", {}));
}

export async function openEvidence(rkey) {
  const result = await get(`/api/evidence/responses/${enc(rkey)}`);
  if (done(result) && !result.stale && result.data.technical) {
    store.set({ evidence: { [rkey]: result.data.technical } });
  }
  return done(result);
}

export async function liveOpen() {
  const result = await get("/api/live");
  if (done(result) && !result.stale && result.data.live) store.set({ live: result.data.live });
  return done(result);
}

function hmac() {
  if (window.CorridorHmac) return Promise.resolve(window.CorridorHmac);
  if (!hmacLoad) {
    hmacLoad = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/vendor/corridor-hmac.js";
      script.addEventListener("load", () => resolve(window.CorridorHmac));
      script.addEventListener("error", () => {
        hmacLoad = null;
        reject(new Error("hmac"));
      });
      document.head.appendChild(script);
    });
  }
  return hmacLoad;
}

// The access code stays in the browser: only a proof over the server nonce is sent.
// The caller clears its input field as soon as this is called. A challenge that carries
// `kdf` (public mode) is proved with the code's PBKDF2 verifier instead of the code; when
// that cannot be derived here, nothing is sent at all.
export async function liveUnlock(code) {
  let lib;
  try {
    lib = await hmac();
  } catch {
    return false;
  }
  if (!lib) return false;
  const challenge = await post("/api/live/challenge", {});
  const nonce = challenge.ok && challenge.data ? challenge.data.nonce : null;
  if (typeof nonce !== "string" || !/^[0-9a-f]{32}$/.test(nonce)) return false;
  let secret = String(code);
  if (challenge.data.kdf !== undefined) {
    try {
      secret = await deriveVerifier(secret, challenge.data.kdf);
    } catch {
      return false;
    }
    if (typeof secret !== "string") return false;
  }
  const proof = lib.proof(secret, nonce);
  return done(await post("/api/live/unlock", { nonce, proof }));
}

export async function liveSelect(id) {
  return done(await post("/api/live/select", { deployment: id }));
}

export async function liveToggle(on) {
  return done(await post("/api/live/toggle", { on: Boolean(on) }));
}

// The isolated experiment uses the same queue and outer session headers as banking.
// Every gesture is captured from the card actually shown when the user clicked.
export async function labOpen() {
  store.set({ lab: null, labPhone: null,
    ui: { labOpen: true, labPhoneOpen: false, deviceOpen: false, evidenceOpen: false, liveOpen: false } });
  return labRefresh();
}

export async function labRefresh() {
  const result = await get("/api/lab");
  return done(result) && !result.stale;
}

export function labClose() {
  store.set({ labPhone: null, ui: { labOpen: false, labPhoneOpen: false } });
}

async function labPost(path, body) {
  const result = await post(path, body);
  const accepted = done(result) && !result.stale;
  if (!accepted) {
    // A failed mutation can retire the child. Never leave its earlier gesture clickable.
    store.set({ lab: null, labPhone: null, ui: { labPhoneOpen: false } });
    await labRefresh();
  }
  return accepted;
}

export async function labStart(scenario) {
  if (!["yellow", "red"].includes(scenario)) return false;
  store.set({ labPhone: null, ui: { labPhoneOpen: false } });
  return labPost("/api/lab/start", { mode: "scripted", scenario });
}

export async function labChange(labGeneration) {
  return labPost("/api/lab/change", { lab_generation: labGeneration });
}

export async function labTryPrevious(labGeneration) {
  return labPost("/api/lab/try-previous", { lab_generation: labGeneration });
}

export async function labGesture(labGeneration, item, code) {
  const g = item && item.gesture;
  if (!g || !["confirm", "mfa_request", "mfa_verify"].includes(g.op)) return false;
  const body = {
    lab_generation: labGeneration,
    tkey: item.key,
    op: g.op,
    gesture: g.token,
    card_version: g.card_version,
  };
  if (g.op === "mfa_verify") {
    if (!/^[0-9]{6}$/.test(String(code))) return false;
    body.code = String(code);
  }
  return labPost("/api/lab/gesture", body);
}

// The child's SMS is shown by its own pairing's phone page in a frame, opened only by
// this explicit click; the main page never reads an inbox (R2).
export function labPhone() {
  const lab = store.lab;
  if (!lab || !lab.active) return false;
  store.set({ ui: { labPhoneOpen: true } });
  return true;
}
