// Demo stage: header, the phone / device / evidence columns, overlays and the corner notice.

import { render } from "../dom.js";
import * as phoneApp from "./phone-app.js";
import * as device from "./device.js";
import * as evidence from "./evidence.js";
import * as lab from "./lab.js";

const NARROW = "(max-width: 1023px)";
const WIDE = "(min-width: 1360px)";
const VERY_WIDE = "(min-width: 1800px)";
const DRAWER = "(max-width: 1359px)";
let live = null;
let liveLoading = null;
let parts = null;
let lastActive = null;

function media(query) {
  return window.matchMedia(query).matches;
}

function loadLive(ctx) {
  if (live || liveLoading) return;
  liveLoading = import("./live.js")
    .then((mod) => {
      live = { mod, mounted: false };
      ctx.store.set({});
    })
    .catch(() => {
      live = { mod: null, mounted: false };
    });
}

export function mount(root, ctx) {
  const { h: el } = ctx;
  const header = el("header", { class: "stage-header", "data-testid": "stage-header" });
  const strip = el("div", { class: "judge-strip", "data-testid": "judge-strip", role: "note" });
  const phone = el("section", { class: "col col-phone", "data-testid": "phone-pane" });
  const dev = el("section", { class: "col col-device", "data-testid": "device-pane", tabindex: "-1" });
  const aside = el("aside", { class: "col col-evidence", "data-testid": "evidence-pane", tabindex: "-1" });
  const sms = el("section", { class: "col col-sms" });
  const main = el("main", { class: "stage-main" }, phone, dev);
  const labHost = el("section", { class: "lab-host", "data-testid": "lab-workspace" });
  main.appendChild(labHost);
  const overlay = el("div", { class: "overlay-host" });
  const corner = el("div", { class: "corner", "data-testid": "corner-notice" });
  // Public mode's demo notice: the first thing on the page, with no way to close it;
  // empty (and hidden) outside public mode.
  const banner = el("div", { class: "public-banner", role: "status", "data-testid": "public-banner" });
  const disclosure = el("details", { class: "public-details", "data-testid": "public-details" });
  disclosure.hidden = true;
  const scrim = el("div", {
    class: "scrim",
    "aria-hidden": "true",
    on: { click: () => closeOverlays(ctx) },
  });
  const shell = el("div", { class: "stage" }, banner, disclosure, header, strip, main, scrim, overlay, corner);
  render(root, shell);
  parts = { root, shell, banner, disclosure, header, strip, main, phone, dev, aside, sms, overlay, corner, scrim, labHost };
  phoneApp.mount(phone, ctx);
  device.mount(dev, ctx);
  evidence.mount(aside, ctx);
  lab.mount(labHost, ctx);
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const ui = ctx.store.ui;
    if (ui.deviceOpen || ui.evidenceOpen || ui.liveOpen || ui.resetOpen) {
      event.preventDefault();
      closeOverlays(ctx);
    }
  });
  window.matchMedia(NARROW).addEventListener("change", () => ctx.store.set({}));
  window.matchMedia(DRAWER).addEventListener("change", () => ctx.store.set({}));
  window.matchMedia(VERY_WIDE).addEventListener("change", () => ctx.store.set({}));
}

function closeOverlays(ctx) {
  const ui = ctx.store.ui;
  for (const el of [parts.dev, parts.aside]) ctx.a11y.release(el);
  const sheetEl = parts.overlay.querySelector(".sheet");
  if (sheetEl) ctx.a11y.release(sheetEl);
  if (ui.deviceOpen || ui.evidenceOpen || ui.liveOpen || ui.resetOpen) {
    ctx.store.set({ ui: { deviceOpen: false, evidenceOpen: false, liveOpen: false, resetOpen: false } });
  }
}

function labAllowed(state) {
  return state.view === "judge" && Boolean(state.capabilities && state.capabilities.lab);
}

function headerView(state, ctx) {
  const { t, h: el, components: c, actions, store, icons } = ctx;
  const judge = state.view === "judge";
  const badge = state.shell.badge || { kind: "demo", text: "" };
  const badgeAttrs = {
    class: ["planner-badge", "badge-" + badge.kind],
    "data-testid": "planner-badge",
    "data-kind": badge.kind,
  };
  const badgeEl = state.capabilities.live
    ? el("button", {
      ...badgeAttrs,
      type: "button",
      "aria-haspopup": "dialog",
      on: {
        click: () => {
          store.set({ ui: { liveOpen: true } });
          actions.liveOpen();
        },
      },
    }, el("span", { class: "dot", "aria-hidden": "true" }), badge.text)
    : el("span", badgeAttrs, el("span", { class: "dot", "aria-hidden": "true" }), badge.text);
  return [
    el("div", { class: "brand" },
      el("span", { class: "brand-mark" }, icons.mark(30)),
      el("div", { class: "brand-text" },
        el("p", { class: "brand-name" }, t("web.ui.stage.title")),
        el("p", { class: "brand-sub" }, t("web.ui.stage.subtitle")))),
    el("div", { class: "header-controls" },
      // The authorization lab is a judge-view tool behind its own capability (R2).
      labAllowed(state) ? c.button({
        label: t(store.ui.labOpen ? "web.ui.lab.back" : "web.ui.lab.entry"),
        kind: "secondary",
        testid: "lab-toggle",
        onClick: () => {
          closeOverlays(ctx);
          if (store.ui.labOpen) actions.labClose();
          else actions.labOpen();
        },
      }) : null,
      c.segmented({
        label: t("web.ui.view.label"),
        value: state.view,
        options: [
          { value: "customer", label: t("web.ui.view.customer"), testid: "view-customer" },
          { value: "judge", label: t("web.ui.view.judge"), testid: "view-judge" },
        ],
        onChange: (v) => actions.setView(v),
      }),
      c.segmented({
        label: t("web.ui.locale.label"),
        value: state.locale,
        options: [
          { value: "zh-CN", label: t("web.ui.locale.zh"), testid: "locale-zh" },
          { value: "en", label: t("web.ui.locale.en"), testid: "locale-en" },
        ],
        onChange: (v) => actions.setLocale(v),
      }),
      store.ui.labOpen ? null : badgeEl,
      judge && !store.ui.labOpen
        ? el("button", {
          type: "button",
          class: "btn btn-secondary btn-md evidence-toggle",
          "data-testid": "evidence-toggle",
          "aria-expanded": store.ui.evidenceOpen ? "true" : "false",
          on: { click: () => store.set({ ui: { evidenceOpen: !store.ui.evidenceOpen } }) },
        }, icons.named("evidence", 18), el("span", { class: "btn-label" }, t("web.ui.evidence.title")))
        : null,
      el("span", { class: "header-sep", "aria-hidden": "true" }),
      store.ui.labOpen ? null : c.button({
        label: t("web.ui.stop_all"),
        kind: "danger",
        icon: "stop",
        testid: "stop-all",
        disabled: !state.session.logged_in,
        onClick: async () => {
          if (await actions.stopAll()) c.toast(t("web.ui.stop_all.done"), { level: "info" });
        },
      }),
      c.button({
        label: t("web.ui.reset"),
        kind: "ghost",
        icon: "reset",
        testid: "reset",
        onClick: () => store.set({ ui: { resetOpen: true } }),
      })),
  ];
}

function resetDialog(ctx) {
  const { t, h: el, components: c, actions } = ctx;
  const close = () => closeOverlays(ctx);
  return c.sheet({
    title: t("web.ui.reset"),
    side: "dialog",
    testid: "reset-dialog",
    onClose: close,
    children: [el("p", { class: "dialog-text" }, t("web.ui.reset.confirm"))],
    footer: [
      c.button({ label: t("web.ui.cancel"), kind: "secondary", testid: "reset-cancel", onClick: close }),
      c.button({
        label: t("web.ui.reset"),
        kind: "primary",
        testid: "reset-confirm",
        onClick: async () => {
          close();
          await actions.reset();
        },
      }),
    ],
  });
}

function liveTitle(ctx) {
  const vm = ctx.store.live;
  return vm && vm.title ? vm.title : ctx.t("web.ui.live.title");
}

// The live panel's content belongs to the live view module; the stage only hosts the sheet.
function updateLive(state, ctx) {
  loadLive(ctx);
  const body = parts.liveBody;
  if (!body) return;
  const title = parts.overlay.querySelector(".sheet-title");
  if (title) title.textContent = liveTitle(ctx);
  if (live && live.mod) {
    if (!live.mounted || live.body !== body) {
      body.replaceChildren();
      live.mod.mount(body, ctx);
      live.mounted = true;
      live.body = body;
    }
    live.mod.update(body, state, ctx);
  } else if (!body.firstChild) {
    body.appendChild(ctx.h("p", { class: "muted" }, ctx.t("web.ui.loading")));
  }
}

function liveSheet(ctx) {
  parts.liveBody = ctx.h("div", { class: "live-host" });
  return ctx.components.sheet({
    title: liveTitle(ctx),
    side: "right",
    testid: "live-panel",
    onClose: () => closeOverlays(ctx),
    children: [parts.liveBody],
  });
}

function overlays(state, ctx) {
  const { store, a11y } = ctx;
  const ui = store.ui;
  const prior = parts.overlay.querySelector(".sheet");
  const priorKind = prior ? prior.getAttribute("data-testid") : null;
  let next = null;
  if (ui.resetOpen) next = "reset-dialog";
  else if (ui.liveOpen && state.capabilities.live) next = "live-panel";
  if (priorKind === next) {
    if (next === "live-panel") updateLive(state, ctx);
    return;
  }
  if (prior) a11y.release(prior);
  if (!next) {
    parts.overlay.replaceChildren();
    parts.liveBody = null;
    return;
  }
  const sheetEl = next === "reset-dialog" ? resetDialog(ctx) : liveSheet(ctx);
  parts.overlay.replaceChildren(sheetEl);
  if (next === "live-panel") updateLive(state, ctx);
  a11y.trapFocus(sheetEl, { onEscape: () => closeOverlays(ctx) });
}

function narrowDevice(state, ctx) {
  const { store, a11y } = ctx;
  const narrow = media(NARROW);
  const active = state.device && state.device.active;
  let reveal = false;
  if (narrow && active && active !== lastActive && !store.ui.deviceOpen) {
    // A new task reveals the sheet in this same frame; no gesture is performed.
    reveal = true;
    queueMicrotask(() => store.set({ ui: { deviceOpen: true } }));
  }
  lastActive = active || null;
  const open = narrow && (store.ui.deviceOpen || reveal);
  parts.dev.classList.toggle("is-open", open);
  if (open && !a11y.trapped(parts.dev)) {
    queueMicrotask(() => {
      const title = parts.dev.querySelector(".device-title");
      a11y.trapFocus(parts.dev, { initial: title || parts.dev, onEscape: () => closeOverlays(ctx) });
    });
  } else if (!open && a11y.trapped(parts.dev)) {
    a11y.release(parts.dev);
  }
}

// Wide screens show the simulated SMS in its own column next to the device, so the code and
// the field it goes into are both on screen; otherwise it sits under the device.
function smsColumn(state, ctx) {
  const own = state.session.logged_in && media(WIDE) && (!state.evidence || media(VERY_WIDE));
  if (own) {
    if (parts.sms.parentNode !== parts.main || parts.sms.previousSibling !== parts.dev) {
      parts.main.insertBefore(parts.sms, parts.dev.nextSibling);
    }
    parts.sms.setAttribute("aria-label", state.device.sms.label || ctx.t("web.ui.sms.title"));
    device.dockSms(parts.sms);
  } else {
    device.dockSms(null);
    if (parts.sms.isConnected) parts.sms.remove();
  }
}

function evidenceDrawer(state, ctx) {
  const { store, a11y } = ctx;
  const judge = Boolean(state.evidence);
  if (judge && parts.aside.parentNode !== parts.main) parts.main.appendChild(parts.aside);
  if (!judge && parts.aside.parentNode) {
    a11y.release(parts.aside);
    parts.aside.remove();
  }
  const drawer = judge && media(DRAWER);
  const open = drawer && store.ui.evidenceOpen;
  parts.aside.classList.toggle("is-drawer", drawer);
  parts.aside.classList.toggle("is-open", open);
  if (open && !a11y.trapped(parts.aside)) {
    queueMicrotask(() => a11y.trapFocus(parts.aside, { onEscape: () => closeOverlays(ctx) }));
  } else if (!open && a11y.trapped(parts.aside)) {
    a11y.release(parts.aside);
  }
  return open;
}

export function update(root, state, ctx) {
  if (!parts) mount(root, ctx);
  const { t, store } = ctx;
  const judge = state.view === "judge";
  parts.shell.setAttribute("data-view", state.view);
  parts.shell.setAttribute("data-locale", state.locale);
  parts.shell.classList.toggle("has-evidence", Boolean(state.evidence));
  parts.shell.classList.toggle("is-lab", Boolean(store.ui.labOpen));
  parts.banner.textContent = state.shell.public_banner || "";
  parts.disclosure.hidden = !state.shell.public_disclosure;
  parts.disclosure.replaceChildren(
    ctx.h("summary", null, state.shell.public_details_title || ""),
    ctx.h("p", null, state.shell.public_disclosure || ""),
  );
  render(parts.header, ...headerView(state, ctx));
  parts.header.setAttribute("aria-label", t("web.ui.stage.title"));
  if (judge && state.shell.judge_strip) {
    parts.strip.textContent = state.shell.judge_strip;
    if (!parts.strip.isConnected) parts.shell.insertBefore(parts.strip, parts.main);
  } else if (parts.strip.isConnected) {
    parts.strip.remove();
  }
  parts.phone.setAttribute("aria-label", t("web.ui.pane.phone"));
  parts.dev.setAttribute("aria-label", state.device ? state.device.title : t("web.ui.device.title"));
  parts.aside.setAttribute("aria-label", t("web.ui.evidence.title"));
  parts.corner.textContent = state.shell.corner;
  if (store.ui.labOpen && !labAllowed(state)) {
    // Outside the judge view (or with the capability closed) the lab is not shown at all.
    queueMicrotask(() => ctx.actions.labClose());
  } else if (store.ui.labOpen) {
    ctx.a11y.release(parts.dev);
    ctx.a11y.release(parts.aside);
    parts.shell.classList.remove("evidence-open");
    parts.scrim.classList.toggle("is-on", Boolean(store.ui.resetOpen));
    lab.update(parts.labHost, state, ctx);
    overlays(state, ctx);
    return;
  }
  const drawerOpen = evidenceDrawer(state, ctx);
  parts.shell.classList.toggle("evidence-open", Boolean(drawerOpen));
  parts.shell.style.setProperty("--header-live", parts.header.offsetHeight + "px");
  narrowDevice(state, ctx);
  const scrimOn = (media(NARROW) && store.ui.deviceOpen) || drawerOpen || store.ui.resetOpen ||
    (store.ui.liveOpen && state.capabilities.live);
  parts.scrim.classList.toggle("is-on", Boolean(scrimOn));
  phoneApp.update(parts.phone, state, ctx);
  device.update(parts.dev, state, ctx);
  smsColumn(state, ctx);
  if (state.evidence) evidence.update(parts.aside, state, ctx);
  overlays(state, ctx);
}

export function bootView(root, ctx) {
  render(root, ctx.h("div", { class: "boot", role: "status", "aria-busy": "true" },
    ctx.h("span", { class: "spinner spinner-lg", "aria-hidden": "true" })));
}

