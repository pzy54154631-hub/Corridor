// An isolated, scripted experiment. Cards and evidence come only from LabVM.
// Neither historical gestures nor SMS codes are copied into current authorization.

import { render } from "../dom.js";

let codeField = null;
let codeStamp = null;
let requestedContext = null;

function currentTask(vm) {
  const device = vm && vm.state && vm.state.device;
  if (!device || !device.active) return null;
  return (device.tasks || []).find((task) => task.key === device.active) || null;
}

function textValue(value, ctx) {
  return value === null || value === undefined ? ctx.t("web.ui.lab.unavailable") : String(value);
}

function resultStatus(value, ctx) {
  const known = new Set(["refused", "done", "executed", "confirmed", "ok", "pending", "settled", "waiting_mfa", "unknown", "failed"]);
  return known.has(value) ? ctx.t("web.ui.lab.status." + value) : textValue(value, ctx);
}

function metric(label, value, testid, ctx) {
  return ctx.h("div", { class: "lab-metric" },
    ctx.h("dt", null, label),
    ctx.h("dd", { class: "num", "data-testid": testid }, textValue(value, ctx)));
}

function empty(text, ctx) {
  return ctx.h("div", { class: "lab-empty" }, ctx.h("p", null, text));
}

function column(title, subtitle, content, className, ctx) {
  const { h } = ctx;
  return h("section", { class: ["lab-column", className] },
    h("header", { class: "lab-column-head" }, h("h2", null, title), h("p", null, subtitle)),
    h("div", { class: "lab-column-body" }, content));
}

function steps(vm, ctx) {
  const { h, t } = ctx;
  const active = !vm || !vm.active ? 1 : !vm.previous ? 2 : !vm.previous_attempted ? 3 : 4;
  return h("ol", { class: "lab-steps", "aria-label": t("web.ui.lab.steps") },
    ["prepare", "change", "try", "confirm"].map((name, i) =>
      h("li", { class: i + 1 === active ? "is-current" : "", "aria-current": i + 1 === active ? "step" : null },
        h("span", { class: "lab-step-number", "aria-hidden": "true" }, i + 1),
        h("span", null, t("web.ui.lab.step." + name)))));
}

function scenarioChoices(vm, ctx) {
  const { h, t, components: c, actions, store } = ctx;
  return h("div", { class: "lab-scenarios", role: "group", "aria-label": t("web.ui.lab.scenario") },
    ["yellow", "red"].map((scenario) => c.button({
      label: t("web.ui.lab.scenario." + scenario),
      kind: vm && vm.active && vm.scenario === scenario ? "primary" : "secondary",
      testid: "lab-start-" + scenario,
      disabled: store.ui.inFlight,
      attrs: { "aria-pressed": vm && vm.active && vm.scenario === scenario ? "true" : "false" },
      onClick: () => actions.labStart(scenario),
    })));
}

function codeInput(vm, task, ctx) {
  const { h, t } = ctx;
  const g = task.gesture;
  const stamp = [vm.session_generation, task.key, g.card_version, g.token].join("|");
  if (stamp !== codeStamp) {
    codeStamp = stamp;
    const error = h("p", { class: "field-error", id: "lab-code-error", role: "alert" });
    const input = h("input", {
      id: "lab-code", type: "text", class: "code-input mono", inputmode: "numeric",
      autocomplete: "off", maxlength: "6", "data-testid": "lab-code-input",
      "aria-describedby": "lab-code-hint lab-code-error",
      on: { input: () => { input.value = input.value.replace(/[^0-9]/g, "").slice(0, 6); } },
    });
    const label = h("label", { class: "field-label", for: "lab-code" });
    const hint = h("p", { class: "field-hint", id: "lab-code-hint" });
    codeField = { input, error, label, hint, node: h("div", { class: "code-field" }, label, input, hint, error) };
  }
  codeField.label.textContent = t("web.ui.lab.code.label");
  codeField.hint.textContent = t("web.ui.lab.code.hint");
  codeField.input.disabled = ctx.store.ui.inFlight;
  return codeField;
}

// The child's simulated SMS: its own pairing's phone page in a same-origin frame (a
// separate document), opened only by an explicit click. No inbox is read here.
function phoneUrl(vm) {
  const sms = vm && vm.state && vm.state.device && vm.state.device.sms;
  const url = sms && typeof sms.url === "string" ? sms.url : "";
  return url.startsWith("/phone/") ? url : null;
}

function phone(vm, ctx) {
  const { h, t, store, actions, components: c } = ctx;
  const url = phoneUrl(vm);
  return h("section", { class: "lab-phone", "data-testid": "lab-phone" },
    c.button({ label: t("web.ui.lab.phone.open"), testid: "lab-phone-open", kind: "secondary",
      disabled: store.ui.inFlight || !url, onClick: () => actions.labPhone() }),
    h("p", { class: "lab-small" }, t("web.ui.lab.phone.hint")),
    store.ui.labPhoneOpen && url ? h("iframe", {
      class: "sms-frame lab-sms-frame", src: url, title: t("web.ui.lab.phone.title"),
      "data-testid": "lab-sms-frame",
    }) : null);
}

function deviceActions(vm, task, ctx) {
  const { h, t, components: c, actions, store } = ctx;
  if (!vm.previous_attempted) return h("p", { class: "lab-guide" }, t("web.ui.lab.confirm.wait"));
  const g = task && task.gesture;
  if (!g) {
    codeField = null;
    codeStamp = null;
    return h("p", { class: "lab-guide" }, t("web.ui.lab.confirm.none"));
  }
  const field = g.op === "mfa_verify" ? codeInput(vm, task, ctx) : null;
  if (!field) { codeField = null; codeStamp = null; }
  const allowed = ["confirm", "mfa_request", "mfa_verify"].includes(g.op);
  return h("div", { class: "lab-device-actions", "data-testid": "lab-device-actions", "data-step": task.step },
    task.step_text ? h("p", { class: "lab-guide" }, task.step_text) : null,
    task.mfa && task.mfa.sent_text ? h("p", null, task.mfa.sent_text) : null,
    field ? field.node : null,
    allowed ? c.button({
      label: t(g.op === "mfa_request" ? "web.ui.lab.mfa.request" : g.op === "mfa_verify" ? "web.ui.lab.mfa.verify"
        : task.step === "final_confirm" ? "web.ui.lab.confirm.final" : "web.ui.lab.confirm.current"),
      kind: "primary", size: "lg", testid: "lab-" + g.op.replace(/_/g, "-"), busy: store.ui.inFlight,
      onClick: async () => {
        const code = field ? field.input.value : undefined;
        if (field && !/^[0-9]{6}$/.test(code)) {
          field.error.textContent = t("web.ui.lab.code.format");
          field.input.setAttribute("aria-invalid", "true");
          field.input.focus();
          return;
        }
        if (field) {
          field.input.value = "";
          field.error.textContent = "";
          field.input.removeAttribute("aria-invalid");
        }
        await actions.labGesture(vm.session_generation, task, code);
      },
    }) : null,
    g.op === "mfa_verify" ? phone(vm, ctx) : null);
}

function original(vm, ctx) {
  const { h, t, components: c } = ctx;
  const card = vm && vm.active ? vm.previous ? vm.previous.card : vm.card : null;
  return column(t("web.ui.lab.original.title"), t(!vm || !vm.active ? "web.ui.lab.original.waiting"
    : vm.previous ? "web.ui.lab.original.withdrawn" : "web.ui.lab.original.pending"),
    card ? [h("p", { class: "lab-request" }, t("web.ui.lab.prompt." + vm.scenario)),
      c.card(card, { owner: "device", plain: true, testid: "lab-original-card" }),
      h("p", { class: "lab-small" }, t("web.ui.lab.original.note"))]
      : empty(t("web.ui.lab.original.empty"), ctx), "lab-original", ctx);
}

function current(vm, ctx) {
  const { h, t, components: c } = ctx;
  const task = currentTask(vm);
  const retained = vm && vm.previous && vm.card && (!task || !task.gesture);
  return column(t(retained ? "web.ui.lab.current.retained.title" : "web.ui.lab.current.title"),
    t(retained ? "web.ui.lab.current.retained.subtitle" : "web.ui.lab.current.subtitle"),
    vm && vm.previous && vm.card ? [
      h("p", { class: "lab-request" }, t("web.ui.lab.prompt.changed." + vm.scenario)),
      c.card(vm.card, { owner: "device", size: "device", plain: true, testid: "lab-current-card" }),
      task && task.failure ? c.notice({ level: "warning", lines: task.failure.lines || [t("web.ui.lab.failure")] }) : null,
      deviceActions(vm, task, ctx)]
      : empty(t("web.ui.lab.current.empty"), ctx), "lab-current", ctx);
}

function evidence(vm, ctx) {
  const { h, t } = ctx;
  const proof = vm && vm.active ? vm.proof : null;
  const receipts = proof && Array.isArray(proof.bank_receipts) ? proof.bank_receipts : null;
  const attempt = vm && vm.last_attempt;
  return column(t("web.ui.lab.bank.title"), t("web.ui.lab.bank.subtitle"),
    proof ? [
      h("dl", { class: "lab-metrics", "data-testid": "lab-proof" },
        metric(t("web.ui.lab.dispatches"), proof.dispatches, "lab-dispatches", ctx),
        metric(t("web.ui.lab.receipts"), receipts ? receipts.length : null, "lab-receipts-count", ctx),
        metric(t("web.ui.lab.audit"), proof.audit_valid === true ? t("web.ui.lab.audit.valid")
          : proof.audit_valid === false ? t("web.ui.lab.audit.invalid") : null, "lab-audit", ctx)),
      attempt ? h("section", { class: "lab-attempt", "data-testid": "lab-last-attempt", role: "status" },
        h("h3", null, t("web.ui.lab.last")),
        h("p", { class: "lab-attempt-status", "data-testid": "lab-attempt-status" }, resultStatus(attempt.status, ctx)),
        h("dl", { class: "lab-facts" },
          metric(t("web.ui.lab.operation"), attempt.operation, null, ctx),
          metric(t("web.ui.lab.http"), attempt.operation_status, null, ctx),
          attempt.code ? metric(t("web.ui.lab.code"), attempt.code, "lab-attempt-code", ctx) : null),
        checks(attempt, ctx)) : null,
      h("section", { class: "lab-receipts", "data-testid": "lab-bank-receipts" },
        h("h3", null, t("web.ui.lab.receipts.title")),
        receipts && receipts.length ? h("ol", null, receipts.map((item) => h("li", { class: "lab-receipt" },
          h("p", { class: "mono-soft" }, item.id),
          h("p", null, [item.kind, item.status, item.time_text].filter(Boolean).join(" · ")))))
          : h("p", { class: "lab-small" }, t(receipts ? "web.ui.lab.receipts.empty" : "web.ui.lab.unavailable"))),
      h("p", { class: "lab-small lab-source" }, t("web.ui.lab.source"))]
      : empty(t("web.ui.lab.bank.empty"), ctx), "lab-bank", ctx);
}

// The two verify-only checks of the saved old request (never sent to the controller).
function checks(attempt, ctx) {
  const { h, t } = ctx;
  const items = Array.isArray(attempt.checks) ? attempt.checks : [];
  if (!items.length) return null;
  return h("ol", { class: "lab-checks", "data-testid": "lab-checks", "aria-label": t("web.ui.lab.checks") },
    items.map((item) => h("li", { "data-testid": "lab-check", "data-target": item.target },
      h("strong", null, t("web.ui.lab.check." + (item.target === "current_card" ? "current_card" : "withdrawn_request"))),
      h("span", { class: "mono-soft" }, [item.code || t("web.ui.lab.check.accepted"), textValue(item.operation_status, ctx)].join(" · ")))));
}

function controls(vm, ctx) {
  const { h, t, components: c, actions, store } = ctx;
  if (!vm || !vm.active) return null;
  return h("section", { class: "lab-control-row", "aria-label": t("web.ui.lab.actions") },
    h("div", { class: "lab-control-copy" },
      h("h2", null, t(!vm.previous ? "web.ui.lab.change.title" : !vm.previous_attempted ? "web.ui.lab.try.title" : "web.ui.lab.continue.title")),
      h("p", null, t(!vm.previous ? "web.ui.lab.change.hint" : !vm.previous_attempted ? "web.ui.lab.try.hint" : "web.ui.lab.continue.hint"))),
    !vm.previous ? c.button({ label: t("web.ui.lab.change"), kind: "primary", size: "lg", testid: "lab-change",
      disabled: !vm.can_change, busy: store.ui.inFlight, onClick: () => actions.labChange(vm.session_generation) })
      : !vm.previous_attempted ? c.button({ label: t("web.ui.lab.try"), kind: "primary", size: "lg", testid: "lab-try-previous",
        busy: store.ui.inFlight, onClick: () => actions.labTryPrevious(vm.session_generation) }) : null);
}

function timeline(vm, ctx) {
  const { h, t } = ctx;
  const items = vm && vm.timeline;
  if (!items || !items.length) return null;
  return h("details", { class: "lab-timeline", "data-testid": "lab-timeline" },
    h("summary", null, t("web.ui.lab.timeline")),
    h("ol", null, items.map((item) => h("li", null,
      h("strong", null, item.operation), h("span", null, resultStatus(item.status, ctx)),
      h("span", null, t("web.ui.lab.timeline.counts", { dispatches: textValue(item.dispatches, ctx), receipts: textValue(item.bank_receipts, ctx) }))))));
}

export function mount(root) {
  root.setAttribute("aria-labelledby", "lab-title");
}

export function update(root, state, ctx) {
  const { h, t, store } = ctx;
  let vm = store.lab;
  const mismatch = vm && vm.active && vm.state && (vm.state.locale !== state.locale || vm.state.view !== state.view);
  if (mismatch) {
    const context = [vm.session_generation, state.locale, state.view].join("|");
    if (requestedContext !== context) {
      requestedContext = context;
      ctx.actions.labRefresh();
    }
    vm = null;
  } else requestedContext = null;
  if (!vm || !vm.active) { codeField = null; codeStamp = null; }
  render(root,
    h("header", { class: "lab-hero" },
      h("p", { class: "lab-eyebrow" }, t("web.ui.lab.eyebrow")),
      h("h1", { id: "lab-title" }, t("web.ui.lab.title")),
      h("p", { class: "lab-lead" }, t("web.ui.lab.lead")),
      h("p", { class: "lab-disclosure" }, t("web.ui.lab.disclosure"))),
    mismatch ? ctx.components.button({ label: t("web.ui.lab.refresh"), testid: "lab-refresh", onClick: () => ctx.actions.labRefresh() }) : null,
    h("div", { class: "lab-preparation" }, scenarioChoices(vm, ctx), h("p", { class: "lab-small" }, t("web.ui.lab.restart.hint"))),
    steps(vm, ctx), controls(vm, ctx),
    h("div", { class: "lab-columns" }, original(vm, ctx), current(vm, ctx), evidence(vm, ctx)),
    timeline(vm, ctx),
    h("footer", { class: "lab-footnote" }, t("web.ui.lab.limit")));
}
