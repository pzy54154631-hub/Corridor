// Simulated signing device: the active task's screen, the waiting queue and the SMS window.
// Gestures happen only here, and each one carries the token the server issued for this step.

import { render } from "../dom.js";
import { openRogue } from "./rogue.js";

let parts = null;
let lastActive = null;
const FINISHED = new Set(["done", "refused", "withdrawn"]);
const codeFields = new Map();
const verifyFailed = new Set();
const MOBILE = "(max-width: 767px)";
let mobileSms = false;
let smsChallenge = null;

export function mount(root, ctx) {
  const { h, icons } = ctx;
  const title = h("h2", { class: "device-title", tabindex: "-1" });
  const head = h("header", { class: "device-head" },
    h("span", { class: "device-head-icon", "aria-hidden": "true" }, icons.shield(20)),
    title,
    h("span", { class: "device-led", "aria-hidden": "true" }));
  const screen = h("div", { class: "device-screen" });
  const status = h("p", { class: "sr-only", "aria-live": "polite", role: "status" });
  const queue = h("div", { class: "device-queue-host" });
  const smsTitle = h("span", { class: "sms-title" });
  const smsFrame = h("iframe", { class: "sms-frame", "data-testid": "sms-frame" });
  const smsOpen = h("button", {
    type: "button",
    class: "btn btn-ghost btn-sm sms-open",
    "data-testid": "sms-open",
    on: {
      click: () => {
        const url = smsFrame.getAttribute("src");
        if (url) window.open(url, "_blank", "noopener,noreferrer");
      },
    },
  }, icons.named("external", 16), h("span", { class: "btn-label" }));
  const sms = h("details", { class: "sms-card", "data-testid": "sms-card", "data-open": "true" },
    h("summary", { class: "sms-summary" },
      h("span", { class: "sms-icon", "aria-hidden": "true" }, icons.named("sms", 18)),
      smsTitle,
      h("span", { class: "disclosure-chevron", "aria-hidden": "true" }, icons.named("chevron", 16))),
    h("div", { class: "sms-body" }, smsFrame, h("div", { class: "sms-row" }, smsOpen)));
  sms.open = true;
  sms.addEventListener("toggle", () => {
    sms.setAttribute("data-open", sms.open ? "true" : "false");
    smsLabel(ctx);
  });
  const slot = h("div", { class: "sms-slot" }, sms);
  const body = h("div", { class: "device-body" }, h("div", { class: "device-unit" }, head, screen, status), queue, slot);
  render(root, body);
  parts = { root, title, screen, status, queue, sms, slot, smsTitle, smsFrame, smsOpen };
  window.matchMedia(MOBILE).addEventListener("change", () => ctx.store.set({}));
}

// The stage may dock the SMS window in its own column on wide screens; null puts it back
// under the device. Moving an iframe reloads it, so this happens only on layout changes.
export function dockSms(target) {
  if (!parts) return;
  const host = target || parts.slot;
  if (parts.sms.parentNode !== host) host.appendChild(parts.sms);
}

function active(state) {
  const device = state.device;
  if (!device || !device.active) return null;
  return device.tasks.find((task) => task.key === device.active) || null;
}

function resultBlock(result, ctx) {
  if (!result) return null;
  const { h } = ctx;
  return h("div", { class: ["device-result", result.banner && "tone-" + result.banner.tone] },
    result.banner ? h("p", { class: "result-title", "data-tone": result.banner.tone }, result.banner.text) : null,
    result.receipt_line ? h("p", { class: "receipt mono-soft" }, result.receipt_line) : null,
    (result.facts_lines || []).map((line) => h("p", { class: "result-line" }, line)),
    (result.message_lines || []).map((line) => h("p", { class: "result-line muted" }, line)));
}

function failureBlock(failure, ctx) {
  if (!failure) return null;
  const { h, icons } = ctx;
  return h("div", { class: "failure", role: "alert", "data-testid": "failure-block", "data-kind": failure.kind },
    h("span", { class: "failure-icon", "aria-hidden": "true" }, icons.named("error", 20)),
    h("div", null,
      (failure.lines || []).map((line) => h("p", null, line)),
      failure.cancel_hint ? h("p", { class: "muted small" }, failure.cancel_hint) : null));
}

function codeField(task, ctx) {
  const { t, components: c } = ctx;
  let field = codeFields.get(task.key);
  if (!field) {
    field = c.codeInput({
      label: t("web.ui.device.code_label"),
      testid: "device-code-input",
      hint: t("web.ui.device.code_hint"),
      onComplete: (code) => submitCode(task.key, code, ctx),
    });
    codeFields.set(task.key, field);
    // The field sits in the action bar, always in view; the user's own tap brought it here.
    queueMicrotask(() => {
      if (!field.isConnected || field.input.offsetParent === null) return;
      field.input.focus({ preventScroll: true });
    });
  }
  field.setError(verifyFailed.has(task.key) ? t("web.ui.device.code_retry") : "");
  return field;
}

async function submitCode(tkey, code, ctx) {
  const field = codeFields.get(tkey);
  verifyFailed.delete(tkey);
  await ctx.actions.mfaVerify(tkey, code);
  // The page never remembers a verified state: clear the field whatever happened.
  if (field) field.clear();
  const now = (ctx.store.state.device.tasks || []).find((task) => task.key === tkey);
  if (now && now.step === "mfa_code") verifyFailed.add(tkey);
  ctx.store.set({});
}

function gestureButtons(task, ctx) {
  const { t, components: c, actions, store } = ctx;
  const busy = store.ui.inFlight;
  const g = task.gesture;
  const out = [];
  if (g && g.op === "confirm") {
    const subset = task.step === "confirm_subset";
    out.push(c.button({
      label: g.label || t(subset ? "web.ui.device.subset" : task.step === "final_confirm" ? "web.ui.device.final" : "web.ui.device.confirm"),
      kind: "primary",
      size: "device",
      testid: subset ? "device-subset" : "device-confirm",
      busy,
      onClick: () => actions.confirm(task.key),
    }));
  } else if (g && g.op === "mfa_request") {
    out.push(c.button({
      label: g.label || t("web.ui.device.mfa_request"),
      kind: "primary",
      size: "device",
      icon: "sms",
      testid: "device-mfa-request",
      busy,
      onClick: () => actions.mfaRequest(task.key),
    }));
  } else if (g && g.op === "mfa_verify") {
    out.push(c.button({
      label: t("web.ui.device.code_submit"),
      kind: "secondary",
      size: "lg",
      testid: "device-code-submit",
      busy,
      onClick: () => {
        const field = codeFields.get(task.key);
        const code = field ? field.codeValue() : "";
        if (/^[0-9]{6}$/.test(code)) submitCode(task.key, code, ctx);
        else if (field) field.setError(t("web.ui.device.code_format"));
      },
    }));
  }
  if (task.back) {
    out.push(c.button({
      label: task.back.label || t("web.ui.device.back"),
      kind: "secondary",
      size: "lg",
      testid: "device-back",
      attrs: { "data-rkey": task.back.rkey },
      onClick: () => actions.makeCurrent(task.back.rkey),
    }));
  }
  if (task.cancel) {
    out.push(c.button({
      label: task.cancel.label,
      kind: "ghost",
      size: "lg",
      testid: "device-cancel",
      onClick: () => actions.cancel(task.key),
    }));
  }
  return out;
}

function taskScreen(task, state, ctx) {
  const { h, t, components: c } = ctx;
  const g = task.gesture;
  const code = task.step === "mfa_code" && g && g.op === "mfa_verify" ? codeField(task, ctx) : null;
  if (!code) codeFields.delete(task.key);
  const mfa = task.mfa;
  return h("article", {
    class: ["device-task", "step-" + task.step],
    "data-testid": "device-task",
    "data-step": task.step,
    "data-source": task.source,
    "data-current": task.current ? "true" : "false",
    "data-key": task.key,
  },
  h("p", { class: "task-source" },
    h("span", { class: "chip" }, task.source_label),
    task.current ? null : h("span", { class: "chip chip-muted" }, t("web.ui.device.not_current"))),
  task.purchase_review ? h("section", { class: "purchase-review", "data-testid": "purchase-review" },
    h("h3", null, task.purchase_review.title),
    h("p", { class: "muted" }, task.purchase_review.notice),
    h("dl", { class: "fact-list" }, task.purchase_review.facts.map((fact) =>
      h("div", { class: "fact-row", "data-field": fact.key },
        h("dt", null, fact.label), h("dd", null, fact.value))))) : null,
  h("div", { class: "device-cards" },
    (task.cards || []).map((vm) => c.card(vm, { owner: "device", size: "device", plain: true }))),
  task.gate_notice
    ? c.notice({ level: "warning", text: task.gate_notice.text }, { testid: "device-gate-notice", tone: "gate" })
    : null,
  mfa
    ? h("div", { class: "mfa" },
      mfa.sent_text ? h("p", { class: "mfa-sent" }, mfa.sent_text) : null,
      mfa.method_text ? h("p", { class: "mfa-method" }, mfa.method_text) : null,
      mfa.hint ? h("p", { class: "mfa-hint" }, mfa.hint) : null,
      mfa.expired_text ? h("p", { class: "mfa-expired" }, mfa.expired_text) : null)
    : null,
  failureBlock(task.failure, ctx),
  resultBlock(task.result, ctx),
  task.hash
    ? c.hash(task.hash.status === "not_yet" ? null : task.hash, {
      status: task.hash.status,
      label: task.hash.label,
      testid: "device-hash",
    })
    : null,
  // The step's own sentence travels with its buttons, so both stay in view together; on the
  // code step the field itself goes there, above its submit button (its label says it all).
  h("div", { class: "device-actions" },
    task.selection
      ? h("p", { class: "selection", "data-testid": "device-selection" }, task.selection.text)
      : null,
    task.step === "confirm_plan" && (task.cards || []).length > 1
      ? h("ol", { "data-testid": "device-batch-review" }, task.cards.map((card) =>
        h("li", null, [card.purpose, card.amount_text,
          ...(card.facts || []).filter((fact) => fact.key.startsWith("objects."))
            .map((fact) => `${fact.label}: ${fact.value}`)].filter(Boolean).join(" · "))))
      : null,
    task.step_text && !code ? h("p", { class: "step-text" }, task.step_text) : null,
    code,
    gestureButtons(task, ctx)),
  task.cancel && task.cancel.hint ? h("p", { class: "cancel-hint" }, task.cancel.hint) : null);
}

function queueView(state, ctx) {
  const { h, t, components: c, actions } = ctx;
  const device = state.device;
  const rest = device.tasks.filter((task) => task.key !== device.active);
  if (!rest.length) return null;
  return h("section", { class: "device-queue", "data-testid": "device-queue" },
    h("p", { class: "queue-title" }, device.queue_text || t("web.ui.device.queue", { count: String(rest.length) })),
    h("ul", { class: "queue-list" }, rest.map((task) => {
      const first = (task.cards || [])[0];
      return h("li", { class: "queue-row", "data-key": task.key, "data-step": task.step },
        first ? c.tierBadge(first.tier) : null,
        h("div", { class: "queue-text" },
          h("p", { class: "queue-main" }, [task.source_label, first && (first.purpose || first.title), first && first.amount_text].filter(Boolean).join(" · ")),
          h("p", { class: "queue-sub" }, task.step_text || "")),
        task.back
          ? h("button", {
            type: "button",
            class: "btn btn-ghost btn-sm",
            "data-rkey": task.back.rkey,
            on: { click: () => actions.makeCurrent(task.back.rkey) },
          }, task.back.label || t("web.ui.device.back"))
          : null);
    })));
}

function idleView(state, ctx) {
  const { h, icons, t } = ctx;
  return h("div", { class: "device-idle" },
    h("span", { class: "idle-icon", "aria-hidden": "true" }, icons.named("device", 36)),
    h("h3", { class: "idle-title", "data-testid": "device-idle-title" }, t("web.ui.device.idle_title")),
    h("p", { class: "idle-text" }, t("web.ui.device.idle_intro")),
    h("ol", { class: "device-idle-steps" },
      [["cards", "web.ui.device.idle_review"], ["shield", "web.ui.device.idle_confirm"], ["check", "web.ui.device.idle_receipt"]].map(([icon, key]) =>
        h("li", null, h("span", { "aria-hidden": "true" }, icons.named(icon, 24)),
          h("span", null, t(key))))),
    h("button", { type: "button", class: "btn btn-secondary", "data-testid": "rogue-device-entry",
      on: { click: () => openRogue(ctx) } }, t("web.ui.rogue.entry")));
}

function smsLabel(ctx) {
  if (!parts) return;
  const sms = ctx.store.state.device.sms;
  parts.smsTitle.textContent = window.matchMedia(MOBILE).matches
    ? ctx.t(parts.sms.open ? "web.ui.sms.collapse" : "web.ui.sms.expand")
    : sms.label || ctx.t("web.ui.sms.title");
}

function updateSms(state, ctx) {
  const { t } = ctx;
  const sms = state.device.sms;
  const mobile = window.matchMedia(MOBILE).matches;
  const task = active(state);
  // The existing step and gesture identify a pending challenge; the main page never
  // reads the SMS code. Preserve a manual collapse until a different challenge arrives.
  const challenge = task && task.step === "mfa_code" && task.gesture && task.gesture.op === "mfa_verify"
    ? task.key : null;
  if (mobile) {
    if (!mobileSms || challenge !== smsChallenge) parts.sms.open = Boolean(challenge);
  } else if (mobileSms) {
    parts.sms.open = true;
  }
  mobileSms = mobile;
  smsChallenge = challenge;
  smsLabel(ctx);
  parts.smsOpen.lastChild.textContent = sms.open_label || t("web.ui.sms.open");
  parts.smsFrame.setAttribute("title", sms.label || t("web.ui.sms.title"));
  if (sms.url && parts.smsFrame.getAttribute("src") !== sms.url && sms.url.startsWith("/") && !sms.url.startsWith("//")) {
    parts.smsFrame.setAttribute("src", sms.url);
  }
  parts.sms.classList.toggle("is-hidden", !state.session.logged_in);
}

export function update(root, state, ctx) {
  if (!parts) mount(root, ctx);
  const { t, a11y } = ctx;
  const device = state.device;
  parts.title.textContent = device.title || t("web.ui.device.title");
  const task = active(state);
  for (const key of [...codeFields.keys()]) {
    if (!device.tasks.some((item) => item.key === key && item.step === "mfa_code")) {
      codeFields.delete(key);
      verifyFailed.delete(key);
    }
  }
  render(parts.screen, task ? taskScreen(task, state, ctx) : idleView(state, ctx));
  // The amber light means something waits; a finished task turns it back to green.
  const waiting = Boolean(task) && !FINISHED.has(task.step);
  parts.root.classList.toggle("has-task", waiting);
  parts.root.classList.toggle("is-done", Boolean(task) && !waiting);
  parts.root.setAttribute("data-step", task ? task.step : "idle");
  render(parts.queue, queueView(state, ctx));
  if (device.active && device.active !== lastActive) a11y.announce(t("web.ui.device.new_task"));
  lastActive = device.active;
  parts.status.textContent = task ? task.step_text || "" : device.idle_text || "";
  updateSms(state, ctx);
}
