// Boot: build ctx, open (or resume) the server session, and redraw the stage on every change.

import { h, svg } from "./dom.js";
import { t } from "./i18n.js";
import * as icons from "./icons.js";
import * as a11y from "./a11y.js";
import * as api from "./api.js";
import * as actions from "./actions.js";
import { store } from "./store.js";
import * as tabdup from "./tabdup.js";
import * as stage from "./views/stage.js";
import { preload } from "./views/phone-app.js";
import { button } from "./components/button.js";
import { outcomeBadge, tierBadge } from "./components/tier.js";
import { card } from "./components/card.js";
import { notice } from "./components/notice.js";
import { scrim, sheet } from "./components/sheet.js";
import { toast } from "./components/toast.js";
import { segmented } from "./components/segmented.js";
import { codeInput } from "./components/codeinput.js";
import { dag } from "./components/dag.js";
import { bars } from "./components/bars.js";
import { details } from "./components/details.js";
import { hash } from "./components/hash.js";

export const components = {
  button, tierBadge, outcomeBadge, card, notice, sheet, scrim, toast, segmented, codeInput, dag, bars,
  details, hash,
};
export const ctx = { t, h, svg, icons, components, actions, store, a11y };

const ERROR_KEYS = {
  network: "web.ui.err.network",
  busy: "web.ui.err.busy",
  no_session: "web.ui.err.session",
  stale_generation: "web.ui.err.stale_generation",
  not_current: "web.ui.err.not_current",
  card_changed: "web.ui.err.card_changed",
  gesture_unknown: "web.ui.err.gesture",
  gesture_mismatch: "web.ui.err.gesture",
  gesture_closed: "web.ui.err.gesture",
  gate_stale: "web.ui.err.gesture",
  outcome_unknown: "web.ui.err.unknown",
};
const LEVELS = { network: "error", busy: "warning", outcome_unknown: "error", operation_failed: "error", internal: "error" };
const pendingToasts = [];

function say(text, opts) {
  if (store.i18n) toast(text(), opts);
  else if (pendingToasts.push([text, opts]) > 3) pendingToasts.shift();
}

api.configure({
  sessionBody: actions.sessionBody,
  onError({ code, message }) {
    const key = ERROR_KEYS[code];
    say(() => (key ? t(key) : message || t("web.ui.err.generic")), { level: LEVELS[code] || "warning" });
  },
});

function showNotices(state) {
  for (const item of (state.shell && state.shell.notices) || []) {
    toast(item.text, { level: item.level, id: item.id });
  }
}

function draw(root) {
  const state = store.state;
  if (!state || !store.i18n) return;
  document.documentElement.lang = state.locale;
  document.title = t("web.ui.stage.title");
  stage.update(root, state, ctx);
  while (pendingToasts.length) {
    const [text, opts] = pendingToasts.shift();
    toast(text(), opts);
  }
  showNotices(state);
}

async function boot() {
  const root = document.getElementById("app");
  stage.bootView(root, ctx);
  let mounted = false;
  let screensReady = false;
  preload().then(() => {
    screensReady = true;
    store.set({});
  });
  store.subscribe(() => {
    if (!mounted && screensReady && store.state && store.i18n) {
      stage.mount(root, ctx);
      mounted = true;
    }
    if (mounted) draw(root);
  });
  tabdup.watch(api.currentTab);
  const held = api.currentTab();
  if (held && (await tabdup.probe(held))) {
    api.setTab(null);
    say(() => t("web.ui.tab_dup"), { level: "info" });
  }
  // Opening a session is not a gesture, so retrying it after a network failure is safe.
  while (!(await actions.openSession())) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

boot();
