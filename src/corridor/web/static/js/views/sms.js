// Simulated SMS inbox (/phone/<pair>). A separate document: it reads only its own pair's
// synthetic messages and never talks to the banking page around it.

import { h, render } from "../dom.js";
import { named } from "../icons.js";

const PAIR = /^\/phone\/([A-Za-z0-9_-]{16,64})\/?$/;
const params = new URLSearchParams(window.location.search);
const lang = params.get("lang") === "en" ? "en" : "zh-CN";
const view = params.get("view") === "judge" ? "judge" : "customer";
const match = PAIR.exec(window.location.pathname);
const pair = match ? match[1] : null;
const ICON = { active: "sms", used: "check", expired: "close" };
let version = null;
let root = null;

function message(item) {
  const status = ICON[item.status] ? item.status : "expired";
  return h(
    "li",
    {
      class: ["sms-msg", "sms-" + status, item.latest && "is-latest"],
      "data-testid": "sms-message",
      "data-status": status,
      "data-latest": item.latest ? "true" : "false",
    },
    h("span", { class: "sms-msg-icon", "aria-hidden": "true" }, named(ICON[status], 16)),
    h("div", { class: "sms-msg-body" },
      h("p", { class: "sms-msg-text" }, item.text),
      item.status_text ? h("p", { class: "sms-msg-status" }, item.status_text) : null),
  );
}

function draw(payload) {
  document.documentElement.lang = lang;
  document.title = payload.title || "";
  const messages = payload.messages || [];
  render(
    root,
    h("div", { class: "sms-page", "data-view": view },
      // Public mode's demo notice (absent otherwise), above the simulated phone's status bar.
      payload.banner
        ? h("div", { class: "public-banner", role: "status", "data-testid": "public-banner" }, payload.banner)
        : null,
      payload.public_disclosure
        ? h("details", { class: "public-details", "data-testid": "public-details" },
          h("summary", null, payload.public_details_title),
          h("p", null, payload.public_disclosure))
        : null,
      h("div", { class: "sms-status" }, h("span", null, payload.status_bar)),
      h("header", { class: "sms-head" },
        h("span", { class: "sms-head-icon", "aria-hidden": "true" }, named("sms", 18)),
        h("h1", { class: "sms-head-title" }, payload.title)),
      h("main", { class: "sms-main" },
        payload.unpaired
          ? h("p", { class: "sms-empty", "data-testid": "sms-unpaired" }, payload.unpaired)
          : messages.length
            ? h("ol", { class: "sms-list", "aria-live": "polite" }, messages.map(message))
            : h("p", { class: "sms-empty" }, payload.empty || "")),
      h("footer", { class: "sms-footer" },
        h("p", null, payload.footer),
        payload.disclosure ? h("p", { class: "sms-disclosure" }, payload.disclosure) : null)),
    h("div", { class: "corner corner-sms", "data-testid": "sms-corner" }, payload.corner),
  );
}

async function poll() {
  try {
    const url = "/api/phone/" + encodeURIComponent(pair) + "?lang=" + encodeURIComponent(lang) +
      "&view=" + encodeURIComponent(view);
    const response = await fetch(url, { cache: "no-store", credentials: "omit" });
    if (response.ok) {
      const payload = await response.json();
      if (payload && payload.ok && payload.version !== version) {
        version = payload.version;
        draw(payload);
      }
    }
  } catch {
    // Keep showing the last inbox; the next poll retries.
  }
  setTimeout(poll, 1000);
}

function start() {
  root = document.getElementById("sms");
  document.documentElement.lang = lang;
  if (!pair) {
    render(root, h("div", { class: "sms-page" }, h("p", { class: "sms-empty", "data-testid": "sms-unpaired" })));
    return;
  }
  poll();
}

start();
