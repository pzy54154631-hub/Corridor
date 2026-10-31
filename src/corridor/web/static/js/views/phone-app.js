// Phone column: thin frame, status bar, app header, lock banner, tab content and tab bar.
// The screens inside (login, chat, accounts, wealth, plans) are separate view modules.

import { render } from "../dom.js";

const SCREENS = {
  login: "./login.js",
  assistant: "./chat.js",
  accounts: "./accounts.js",
  wealth: "./wealth.js",
  plans: "./plans.js",
};
const TAB_ICONS = { assistant: "assistant", accounts: "accounts", wealth: "wealth", plans: "plans" };
const TAB_KEYS = {
  assistant: "web.ui.tab.assistant",
  accounts: "web.ui.tab.accounts",
  wealth: "web.ui.tab.wealth",
  plans: "web.ui.tab.plans",
};

let parts = null;
const screens = {};
const modules = {};
const loaded = {};

function fetchScreen(name) {
  if (!modules[name]) {
    modules[name] = import(SCREENS[name]).then(
      (mod) => (loaded[name] = { mod, failed: false }),
      () => (loaded[name] = { mod: null, failed: true }),
    );
  }
  return modules[name];
}

// Loading the screens before the first draw avoids a second full redraw right after boot.
export function preload() {
  return Promise.all(Object.keys(SCREENS).map(fetchScreen));
}

function load(name, ctx) {
  if (screens[name]) return screens[name];
  const root = ctx.h("div", {
    class: "screen",
    role: name === "login" ? null : "tabpanel",
    id: "screen-" + name,
    "aria-labelledby": name === "login" ? null : "tab-" + name,
    "data-screen": name,
  });
  const ready = loaded[name];
  const entry = { root, mod: ready ? ready.mod : null, mounted: false, failed: ready ? ready.failed : false };
  screens[name] = entry;
  if (!ready) {
    fetchScreen(name).then((result) => {
      entry.mod = result.mod;
      entry.failed = result.failed;
      if (entry.root.isConnected) ctx.store.set({});
    });
  }
  return entry;
}

export function mount(root, ctx) {
  const { h } = ctx;
  const status = h("div", { class: "status-bar" });
  const header = h("header", { class: "app-header" });
  const banners = h("div", { class: "app-banners" });
  const body = h("div", { class: "app-body" });
  const tabbar = h("nav", { class: "tabbar" });
  const screen = h("div", { class: "phone-screen" }, status, header, banners, body, tabbar);
  const frame = h("div", { class: "phone-frame" }, screen);
  render(root, frame);
  parts = { root, frame, status, header, banners, body, tabbar };
}

function clockOf(text) {
  const match = /(\d{1,2}:\d{2})/.exec(String(text || ""));
  return match ? match[1] : "";
}

function statusBar(state, ctx) {
  const { h, t, icons } = ctx;
  return [
    h("span", { class: "status-time num" }, clockOf(state.session.clock_text)),
    h("span", { class: "status-label" }, t("web.ui.pane.phone")),
    h("span", { class: "status-icons", "aria-hidden": "true" }, icons.signal(15), icons.battery(22)),
  ];
}

function appHeader(state, ctx) {
  const { h, icons } = ctx;
  return [
    h("span", { class: "app-mark" }, icons.mark(34)),
    h("div", { class: "app-title" },
      h("p", { class: "app-bank" }, state.session.bank_name),
      h("p", { class: "app-assistant" }, state.session.assistant_name)),
    h("span", { class: "app-shield", "aria-hidden": "true" }, icons.shield(20)),
  ];
}

function banners(state, ctx) {
  const { h, icons } = ctx;
  const out = [];
  const safety = state.shell.safety;
  if (safety && safety.locked) {
    const judge = [];
    if (safety.state) judge.push(safety.state + (safety.reason ? " · " + safety.reason : ""));
    if (safety.unlock_after_text) judge.push(safety.unlock_after_text);
    if (safety.rule_ids && safety.rule_ids.length) judge.push(safety.rule_ids.join(" · "));
    out.push(h("section", { class: "lock-banner", role: "alert", "data-testid": "safety-banner", "data-locked": "true" },
      h("span", { class: "lock-icon", "aria-hidden": "true" }, icons.lock(20)),
      h("div", { class: "lock-body" },
        safety.text ? h("p", { class: "lock-text" }, safety.text) : null,
        safety.hint ? h("p", { class: "lock-hint" }, safety.hint) : null,
        judge.length ? h("p", { class: "lock-judge mono" }, judge.join(" · ")) : null)));
  }
  if (state.shell.paused_banner) {
    out.push(h("p", { class: "paused-banner", role: "status" },
      icons.named("headset", 16), h("span", null, state.shell.paused_banner)));
  }
  return out;
}

function tabs(state, ctx) {
  const allowed = ["assistant", ...(state.capabilities.tabs || [])];
  let current = ctx.store.ui.tab;
  if (!allowed.includes(current)) current = "assistant";
  return { allowed, current };
}

function tabBar(state, ctx, allowed, current) {
  const { h, t, icons, store, actions } = ctx;
  const select = (name) => {
    if (name === store.ui.tab) return;
    store.set({ ui: { tab: name } });
    if (name !== "assistant") actions.loadTab(name);
  };
  const onKey = (event) => {
    const index = allowed.indexOf(current);
    let next = null;
    if (event.key === "ArrowRight") next = allowed[(index + 1) % allowed.length];
    if (event.key === "ArrowLeft") next = allowed[(index - 1 + allowed.length) % allowed.length];
    if (!next) return;
    event.preventDefault();
    select(next);
    queueMicrotask(() => {
      const el = parts.tabbar.querySelector(`[data-testid="tab-${next}"]`);
      if (el) el.focus();
    });
  };
  return h("div", { class: "tablist", role: "tablist", "aria-label": t("web.ui.tabs.label"), on: { keydown: onKey } },
    allowed.map((name) =>
      h("button", {
        type: "button",
        role: "tab",
        id: "tab-" + name,
        class: ["tab", name === current && "is-on"],
        "aria-selected": name === current ? "true" : "false",
        "aria-controls": "screen-" + name,
        tabindex: name === current ? "0" : "-1",
        "data-testid": "tab-" + name,
        on: { click: () => select(name) },
      }, icons.named(TAB_ICONS[name], 22), h("span", { class: "tab-label" }, t(TAB_KEYS[name])))));
}

function placeholder(ctx, failed) {
  const { h, t } = ctx;
  return h("div", { class: "skeleton-block", "aria-busy": failed ? "false" : "true" },
    h("span", { class: "skeleton-line w-60" }),
    h("span", { class: "skeleton-line w-90" }),
    h("span", { class: "skeleton-line w-75" }),
    h("p", { class: "sr-only" }, t("web.ui.loading")));
}

function show(name, state, ctx) {
  const entry = load(name, ctx);
  if (entry.root.parentNode !== parts.body) parts.body.appendChild(entry.root);
  // Hidden screens leave the document so their test ids and controls never duplicate.
  for (const other of Object.values(screens)) {
    if (other !== entry && other.root.isConnected) other.root.remove();
  }
  if (entry.mod) {
    if (!entry.mounted) {
      entry.root.replaceChildren();
      entry.mod.mount(entry.root, ctx);
      entry.mounted = true;
    }
    entry.mod.update(entry.root, state, ctx);
  } else if (!entry.root.firstChild || entry.failed) {
    render(entry.root, placeholder(ctx, entry.failed));
  }
}

export function update(root, state, ctx) {
  if (!parts) mount(root, ctx);
  render(parts.status, ...statusBar(state, ctx));
  render(parts.header, ...appHeader(state, ctx));
  render(parts.banners, ...banners(state, ctx));
  const loggedIn = state.session.logged_in && !state.login;
  parts.frame.classList.toggle("is-login", !loggedIn);
  if (!loggedIn) {
    render(parts.tabbar);
    show("login", state, ctx);
    return;
  }
  const { allowed, current } = tabs(state, ctx);
  if (current !== ctx.store.ui.tab) queueMicrotask(() => ctx.store.set({ ui: { tab: current } }));
  render(parts.tabbar, tabBar(state, ctx, allowed, current));
  show(current, state, ctx);
}
