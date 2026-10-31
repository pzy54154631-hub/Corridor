// One assistant reply: cards, notes outside the card frame, clarification,
// plan selection, the gate, receipt or failure, route hint and cancel.
// Which controls exist and whether they are enabled is decided by the
// server; this module never filters cards or evaluates rules.

import { renderGate } from "./gate.js";
import { button, el, failureBlock, lineList, lockable, para, resultBlock, setFlags } from "./result.js";
import { suggestionBlock } from "./suggest.js";

// Radio choice made before pressing "review" is a form draft, not state.
const clarifyDrafts = new Map();

function draftKey(state, rkey) {
  return `${state.generation}:${rkey}`;
}

export function forgetDraft(state, rkey) {
  clarifyDrafts.delete(draftKey(state, rkey));
}

export function cardWithNotes(ctx, card, opts = {}) {
  const node = ctx.components.card(card, { testid: "card", compact: Boolean(opts.compact), owner: "phone" });
  const notes = (opts.compact ? [] : card.notes || []).map((note) =>
    ctx.components.notice(note, { notSignedLabel: card.not_signed_label }),
  );
  return el(
    ctx,
    "div",
    { class: "scr-card-unit" },
    node,
    notes.length ? el(ctx, "div", { class: "scr-notes" }, notes) : null,
  );
}

function clarifyBlock(ctx, state, rvm) {
  const vm = rvm.clarify;
  if (!vm) return null;
  const key = draftKey(state, rvm.rkey);
  const picked = clarifyDrafts.get(key) || vm.chosen;
  let review = null;
  const choices = vm.choices.map((choice, i) => {
    const input = el(ctx, "input", {
      type: "radio",
      name: `clarify-${rvm.rkey}`,
      value: choice.key,
      "data-testid": `clarify-choice-${i}`,
      on: {
        change: () => {
          clarifyDrafts.set(key, choice.key);
          if (rvm.current && !vm.locked) {
            review.dataset.off = "0";
            review.disabled = Boolean(ctx.store.ui && ctx.store.ui.inFlight);
          }
        },
      },
    });
    setFlags(input, { checked: picked === choice.key });
    lockable(input, vm.locked);
    return el(ctx, "label", { class: "scr-radio scr-radio-lg" }, input, el(ctx, "span", {}, choice.label));
  });
  review = button(ctx, {
    label: vm.review_label,
    kind: "primary",
    testid: "clarify-review",
    disabled: vm.locked || !picked || !rvm.current,
    onClick: () => {
      const choice = clarifyDrafts.get(key) || vm.chosen;
      if (choice && rvm.current) ctx.actions.recipient(rvm.rkey, choice);
    },
  });
  return el(
    ctx,
    "fieldset",
    { class: "scr-clarify", "data-locked": String(Boolean(vm.locked)) },
    el(ctx, "legend", { class: "scr-gate-legend" }, vm.label),
    choices,
    vm.error_text ? el(ctx, "p", { class: "scr-error", role: "alert" }, vm.error_text) : null,
    vm.locked && vm.locked_text ? para(ctx, vm.locked_text, "scr-hint", "clarify-locked") : null,
    review,
    lineList(ctx, vm.evidence_lines, "scr-lines scr-judge-block"),
  );
}

// An explanation under a transfer no contact matched; the names are not choices.
function contactHint(ctx, rvm) {
  const vm = rvm.contact_hint;
  if (!vm) return null;
  return el(
    ctx,
    "section",
    { class: "scr-contact-hint", "data-testid": "contact-hint" },
    para(ctx, vm.lead),
    el(ctx, "ul", { class: "scr-contact-list" }, vm.contacts.map((name) => el(ctx, "li", {}, name))),
    para(ctx, vm.retry, "scr-hint"),
  );
}

function taskFor(state, tkey) {
  return (state.device.tasks || []).find((task) => task.key === tkey) || null;
}

export function planSelect(ctx, state, vm, cards, readOnly = false) {
  if (!vm) return null;
  const task = taskFor(state, vm.tkey);
  const version = task ? task.nodes_version : null;
  const register = (key, on) => {
    const keys = vm.nodes.filter((n) => (n.key === key ? on : n.checked)).map((n) => n.key);
    if (version) ctx.actions.selectNodes(vm.tkey, version, keys);
  };
  const rows = vm.nodes.map((node, i) => {
    const input = el(ctx, "input", {
      type: "checkbox",
      value: node.key,
      "data-testid": `plan-node-${i}`,
      "data-checked": String(Boolean(node.checked)),
      on: { change: (ev) => register(node.key, ev.target.checked) },
    });
    setFlags(input, { checked: node.checked });
    lockable(input, !version || readOnly);
    const card = cards && cards.length === vm.nodes.length ? cardWithNotes(ctx, cards[i]) : null;
    return el(
      ctx,
      "div",
      { class: "scr-plan-node" },
      el(ctx, "label", { class: "scr-check" }, input, el(ctx, "span", {}, node.label)),
      card,
    );
  });
  return el(
    ctx,
    "div",
    { class: `scr-plan-select scr-plan-${vm.mode}` },
    para(ctx, vm.rule_text, "scr-hint"),
    rows,
    el(ctx, "p", { class: "scr-plan-count scr-num" }, `${vm.count} / ${vm.total}`),
    vm.error_text ? el(ctx, "p", { class: "scr-error", "data-testid": "plan-error", role: "alert" }, vm.error_text) : null,
  );
}

function birthdayBlock(ctx, rvm) {
  const vm = rvm.birthday;
  if (!vm) return null;
  return el(
    ctx,
    "div",
    { class: "scr-birthday" },
    lineList(ctx, vm.lines),
    el(
      ctx,
      "div",
      { class: "scr-choice-row" },
      vm.choices.map((choice, i) =>
        button(ctx, {
          label: choice.label,
          kind: "secondary",
          testid: `birthday-choice-${i}`,
          disabled: !choice.enabled || !rvm.current,
          onClick: () => ctx.actions.birthday(rvm.rkey, choice.key),
        }),
      ),
    ),
    vm.error_text ? el(ctx, "p", { class: "scr-error", role: "alert" }, vm.error_text) : null,
  );
}

// Choosing a subscription prepares its approval card; it never confirms that card.
function subscriptionBlock(ctx, rvm) {
  const vm = rvm.subscription_select;
  if (!vm) return null;
  return el(ctx, "section", { class: "scr-birthday", "data-testid": "subscription-select" },
    para(ctx, ctx.t("web.ui.subs.cancel"), "scr-hint"),
    el(ctx, "div", { class: "scr-choice-row" },
      (vm.choices || []).map((choice, i) => button(ctx, {
        label: choice.label,
        kind: "secondary",
        testid: `subscription-choice-${i}`,
        disabled: vm.locked || !rvm.current,
        onClick: () => ctx.actions.subscriptionChoice(rvm.rkey, choice.key, vm.list_version),
      }))));
}

// A listed scheduled authorization creates its revocation card; it never executes it.
function scheduleCancelBlock(ctx, rvm) {
  const vm = rvm.schedule_cancel_select;
  if (!vm) return null;
  return el(ctx, "section", {
    class: "scr-birthday scr-schedule-cancel-select",
    "data-testid": "schedule-cancel-select",
  },
    para(ctx, ctx.t("web.ui.schedule.cancel.choose"), "scr-hint"),
    el(ctx, "div", { class: "scr-choice-row" },
      (vm.choices || []).map((choice, i) => button(ctx, {
        label: choice.label,
        kind: "secondary",
        testid: `schedule-cancel-choice-${i}`,
        disabled: vm.locked || !rvm.current,
        onClick: () => ctx.actions.scheduleCancelChoice(rvm.rkey, choice.key, vm.list_version),
      }))));
}

function routeHint(ctx, state, rvm, onOperator) {
  const hint = rvm.route_hint;
  if (!hint) return null;
  const tabs = state.capabilities.tabs || [];
  let go = null;
  if (tabs.includes(hint.target)) {
    go = () => ctx.store.set({ ui: { ...ctx.store.ui, tab: hint.target } });
  } else if (hint.target === "operator") {
    go = onOperator;
  }
  const attrs = { class: "scr-route", "data-testid": "route-hint", "data-target": hint.target };
  const note = hint.note ? el(ctx, "span", { class: "scr-hint scr-route-note" }, hint.note) : null;
  if (!go) return el(ctx, "div", attrs, el(ctx, "span", { class: "scr-route-label" }, hint.label), note);
  // The whole hint is a click target; the inner button carries focus and
  // keyboard activation, and its click bubbles up to the same handler.
  const testid = hint.target === "operator" ? "operator-open" : "route-hint-go";
  return el(
    ctx,
    "div",
    { ...attrs, class: "scr-route scr-route-go", on: { click: go } },
    el(ctx, "button", { type: "button", class: "scr-route-label scr-link", "data-testid": testid }, hint.label),
    note,
  );
}

// An earlier reply's own controls stay inert until it is made current again.
function backRow(ctx, rvm) {
  if (rvm.current || rvm.gate || !(rvm.clarify || rvm.birthday || rvm.plan_select)) return null;
  return button(ctx, {
    label: ctx.t("web.ui.device.back"),
    kind: "secondary",
    testid: "response-back",
    onClick: () => ctx.actions.makeCurrent(rvm.rkey),
  });
}

function judgeBits(ctx, rvm) {
  const parts = [
    rvm.card_source ? el(ctx, "span", { class: "scr-judge-tag" }, rvm.card_source) : null,
    para(ctx, rvm.card_mismatch, "scr-judge-line scr-error"),
    para(ctx, rvm.phrase_gate_note, "scr-judge-line", "phrase-gate-note"),
    lineList(ctx, rvm.parse_note, "scr-lines scr-judge-line"),
    rvm.technical_url
      ? ctx.components.button({
          label: ctx.t("web.ui.response.technical"),
          kind: "ghost",
          testid: "response-technical",
          onClick: async () => {
            await ctx.actions.openEvidence(rvm.rkey);
            ctx.store.set({ ui: { ...ctx.store.ui, evidenceOpen: true } });
          },
        })
      : null,
  ].filter(Boolean);
  return parts.length ? el(ctx, "div", { class: "scr-judge-block" }, parts) : null;
}

function cardsSection(ctx, state, rvm) {
  if (rvm.plan_select) return planSelect(ctx, state, rvm.plan_select, rvm.cards, !rvm.current);
  if (!rvm.cards.length) return null;
  // Once a reply has a receipt or is no longer current, its cards fold so
  // the outcome reads first; the full card is one click away.
  const compact = !rvm.current || Boolean(rvm.result);
  const units = rvm.cards.map((card) => cardWithNotes(ctx, card, { compact }));
  return el(ctx, "div", { class: "scr-cards" }, units);
}

// "Confirm on the device" only while the device actually offers a gesture
// for this reply's task (the device pane explains every other step).
function deviceLook(ctx, state, rvm) {
  if (!rvm.current || !rvm.device_task || rvm.result || rvm.failure) return null;
  const task = taskFor(state, rvm.device_task);
  if (!task || !task.gesture) return null;
  return el(
    ctx,
    "div",
    { class: "scr-device-look" },
    el(ctx, "span", { class: "scr-device-look-text" }, ctx.t("web.ui.device.look")),
    ctx.components.button({
      label: ctx.t("web.ui.device.open"),
      kind: "secondary",
      testid: "device-open",
      onClick: () => ctx.store.set({ ui: { ...ctx.store.ui, deviceOpen: true } }),
    }),
  );
}

function cancelRow(ctx, state, rvm) {
  if (!rvm.cancel) return null;
  return el(
    ctx,
    "div",
    { class: "scr-cancel-row" },
    button(ctx, {
      label: rvm.cancel.label,
      kind: "ghost",
      testid: "cancel-chat",
      onClick: () => {
        forgetDraft(state, rvm.rkey);
        if (rvm.device_task) ctx.actions.cancel(rvm.device_task);
        else ctx.actions.chatStop();
      },
    }),
    para(ctx, rvm.cancel.hint, "scr-hint"),
  );
}

export function renderResponse(ctx, state, rvm, opts = {}) {
  const settled = Boolean(rvm.result) && !rvm.failure;
  const tier = rvm.tier && !rvm.cards.length
    ? ctx.components.tierBadge(rvm.tier, settled ? { settled: true, label: ctx.t("web.ui.tier.done." + rvm.tier.code) } : undefined)
    : null;
  const parts = [
    rvm.redisplay_notice ? ctx.components.notice({ level: "warning", text: rvm.redisplay_notice }) : null,
    (rvm.live_notes || []).map((n) => ctx.components.notice({ level: n.level, text: n.text })),
    failureBlock(ctx, rvm.failure),
    resultBlock(ctx, rvm.result),
    clarifyBlock(ctx, state, rvm),
    contactHint(ctx, rvm),
    cardsSection(ctx, state, rvm),
    birthdayBlock(ctx, rvm),
    subscriptionBlock(ctx, rvm),
    scheduleCancelBlock(ctx, rvm),
    suggestionBlock(ctx, rvm),
    renderGate(ctx, rvm.gate, rvm),
    deviceLook(ctx, state, rvm),
    routeHint(ctx, state, rvm, opts.onOperator),
    backRow(ctx, rvm),
    cancelRow(ctx, state, rvm),
    judgeBits(ctx, rvm),
  ];
  // A reply with nothing but its status (the phrase gate's answer) keeps the status as a
  // small line under the bubble instead of an empty card.
  const bare = parts.every((part) => part === null || (Array.isArray(part) && part.length === 0));
  const body = el(
    ctx,
    "div",
    { class: "scr-response-body" },
    el(
      ctx,
      "div",
      { class: "scr-response-head" },
      el(ctx, "span", { class: `scr-status scr-status-${rvm.status}` }, rvm.status_text),
      rvm.current ? null : el(ctx, "span", { class: "scr-muted" }, ctx.t("web.ui.response.earlier")),
      tier,
    ),
    ...parts,
  );
  return el(
    ctx,
    "article",
    {
      class: `scr-response${rvm.current ? " scr-current" : " scr-earlier"}${bare ? " scr-response-bare" : ""}`,
      "data-testid": "response",
      "data-rkey": rvm.rkey,
      "data-status": rvm.status,
      "data-current": String(Boolean(rvm.current)),
      "aria-label": rvm.status_text,
    },
    opts.lead || null,
    body,
  );
}
