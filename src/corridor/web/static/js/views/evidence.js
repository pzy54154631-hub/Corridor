// Judge evidence column. Three sections open by default; the rest stay folded until asked for.
// The panel is a display preference, not access control: customer view never receives it.

import { render } from "../dom.js";

let parts = null;
const userOpen = new Map();

export function mount(root, ctx) {
  const { h, icons } = ctx;
  const title = h("h2", { class: "evidence-title" });
  const head = h("header", { class: "evidence-head" },
    h("span", { class: "evidence-head-icon", "aria-hidden": "true" }, icons.named("evidence", 18)), title);
  const body = h("div", { class: "evidence-body" });
  const foot = h("footer", { class: "evidence-foot" });
  render(root, head, body, foot);
  parts = { root, title, body, foot };
}

function yesNo(value, ctx, yes, no) {
  if (value === true) return ctx.t(yes);
  if (value === false) return ctx.t(no);
  return ctx.t("web.ui.evidence.unknown");
}

function flag(value) {
  return value === true ? "true" : value === false ? "false" : "null";
}

function row(ctx, label, value, opts) {
  const { h } = ctx;
  return h("div", { class: ["kv", opts && opts.tone && "kv-" + opts.tone] },
    h("dt", null, label),
    h("dd", { class: opts && opts.mono ? "mono" : null }, value));
}

function section(ctx, name, title, open, children, extra) {
  const { components: c } = ctx;
  const isOpen = userOpen.has(name) ? userOpen.get(name) : open;
  return c.details({
    summary: title,
    open: isOpen,
    testid: "evidence-section-" + name,
    className: "evidence-section",
    extra,
    onToggle: (value) => {
      userOpen.set(name, value);
      if (value && name === "technical") loadTechnical(ctx);
    },
    children,
  });
}

function loadTechnical(ctx) {
  const ev = ctx.store.state && ctx.store.state.evidence;
  const rkey = ev && ev.response && ev.response.rkey;
  if (rkey && !ctx.store.evidence[rkey]) ctx.actions.openEvidence(rkey);
}

function hashRow(ctx, label, vm, status) {
  const { h, t, components: c } = ctx;
  return h("div", { class: "hash-block" },
    h("p", { class: "hash-caption" }, label),
    vm
      ? c.hash(vm, {
        label: t("web.ui.hash.caption", { algorithm: vm.algorithm }) + (status ? " · " + status : ""),
        showAlgorithm: false,
      })
      : h("p", { class: "muted small" }, t("web.ui.evidence.not_yet")));
}

function kernelSection(ev, ctx) {
  const { h, t, components: c } = ctx;
  const k = ev.kernel;
  if (!k) return h("p", { class: "muted" }, t("web.ui.evidence.empty"));
  return h("div", { class: "evidence-kernel", "data-testid": "evidence-kernel", "data-outcome": k.outcome || "none" },
    h("div", { class: "verdict" },
      c.outcomeBadge(k.outcome, k.outcome || t("web.ui.evidence.unknown")),
      k.tier ? h("span", { class: "chip" }, k.tier) : null),
    k.rule_ids && k.rule_ids.length
      ? h("ul", { class: "rules" }, k.rule_ids.map((id, i) =>
        h("li", null, h("code", { class: "rule-id" }, id), h("span", null, (k.rule_lines || [])[i] || ""))))
      : null,
    k.note ? h("p", { class: "muted small" }, k.note) : null);
}

function consistencySection(ev, ctx) {
  const { h, t } = ctx;
  const cs = ev.consistency;
  if (!cs) return h("p", { class: "muted" }, t("web.ui.evidence.empty"));
  const facts = cs.same_business_facts;
  return h("div", {
    class: "evidence-consistency",
    "data-testid": "evidence-consistency",
    "data-same-hash": flag(cs.same_hash),
    "data-same-facts": flag(cs.same_business_facts),
  },
  facts === null || facts === undefined
    ? null
    : h("p", { class: ["consistency-verdict", facts ? "is-ok" : "is-bad"] },
      yesNo(facts, ctx, "web.ui.evidence.facts_same", "web.ui.evidence.facts_differ")),
  h("dl", { class: "kv-list" },
    row(ctx, t("web.ui.evidence.same_hash"), yesNo(cs.same_hash, ctx, "web.ui.evidence.equal", "web.ui.evidence.not_equal")),
    row(ctx, t("web.ui.evidence.challenge"), t("web.ui.evidence.challenge." + (cs.challenge || "none")))),
  hashRow(ctx, t("web.ui.evidence.display_card"), cs.display_card),
  cs.proposal_card
    ? hashRow(ctx, t("web.ui.evidence.proposal_card"), cs.proposal_card)
    : h("p", { class: "not-yet", "data-testid": "evidence-not-yet" }, cs.not_yet_text || t("web.ui.evidence.not_yet")),
  cs.action ? hashRow(ctx, "H(action)", cs.action) : null,
  cs.cert ? hashRow(ctx, "H(cert)", cs.cert) : null,
  cs.card_core ? hashRow(ctx, "H(card)", cs.card_core) : null,
  cs.mismatch_text ? h("p", { class: "warn-text" }, cs.mismatch_text) : null);
}

function dispatchSection(ev, ctx) {
  const { h, t } = ctx;
  const d = ev.dispatch || { count: 0, receipts: [] };
  return h("div", { class: "evidence-dispatch", "data-testid": "evidence-dispatch", "data-count": String(d.count) },
    h("p", { class: "big-count num" }, String(d.count), h("span", { class: "count-label" }, t("web.ui.evidence.dispatch_count"))),
    d.receipts && d.receipts.length
      ? h("ul", { class: "receipts" }, d.receipts.map((r) =>
        h("li", null,
          h("span", { class: "receipt-kind" }, r.kind),
          h("span", { class: "receipt-status chip" }, r.status),
          h("span", { class: "receipt-time num" }, r.time_text),
          h("code", { class: "receipt-id mono" }, r.id))))
      : h("p", { class: "muted small" }, t("web.ui.evidence.no_receipts")));
}

function plannerSection(ev, ctx) {
  const { h } = ctx;
  const p = ev.planner || { badge: "", source: "", counters: [] };
  return h("div", { class: "evidence-planner" },
    h("p", null, h("span", { class: "chip" }, p.badge), " ", p.source),
    (p.counters || []).map((line) => h("p", { class: "muted small" }, line)));
}

function auditSection(ev, ctx) {
  const { h, t } = ctx;
  const a = ev.audit;
  if (!a) return null;
  return h("div", { class: "evidence-audit", "data-testid": "evidence-audit", "data-verified": a.verified ? "true" : "false" },
    h("dl", { class: "kv-list" },
      row(ctx, t("web.ui.evidence.audit_count"), String(a.count)),
      row(ctx, t("web.ui.evidence.audit_verified"), yesNo(a.verified, ctx, "web.ui.evidence.pass", "web.ui.evidence.fail"),
        { tone: a.verified ? "ok" : "bad" })),
    hashRow(ctx, t("web.ui.evidence.audit_head"), a.head),
    a.note ? h("p", { class: "muted small" }, a.note) : null);
}

function table(ctx, testid, columns, rows) {
  const { h } = ctx;
  return h("div", { class: "table-wrap", "data-testid": testid },
    h("table", { class: "data-table" },
      h("thead", null, h("tr", null, columns.map((col) => h("th", null, col)))),
      h("tbody", null, rows.map((cells) => h("tr", null, cells.map((cell) => h("td", null, cell)))))));
}

function intentSection(ev, ctx) {
  const { h, t } = ctx;
  const items = ev.intent_log || [];
  return [
    h("p", { class: "muted small" }, ev.intent_log_note),
    table(ctx, "evidence-intent-log",
      [t("web.ui.evidence.col.intent"), t("web.ui.evidence.col.status"), t("web.ui.evidence.col.response"), t("web.ui.evidence.col.utterance")],
      items.map((item) => [item.intent, item.status, item.response, item.utterance])),
  ];
}

function plannerLogSection(ev, ctx) {
  const { t } = ctx;
  return table(ctx, "evidence-planner-log",
    [t("web.ui.evidence.col.reply"), t("web.ui.evidence.col.planner"), t("web.ui.evidence.col.intent"),
      t("web.ui.evidence.col.outcome"), t("web.ui.evidence.col.tier"), t("web.ui.evidence.col.rules")],
    (ev.planner_log || []).map((item) => [item.rkey, item.planner, item.intent, item.outcome || "—",
      item.tier || "—", (item.rule_ids || []).join(", ")]));
}

function technicalSection(ev, ctx) {
  const { h, t, store } = ctx;
  const rkey = ev.response && ev.response.rkey;
  if (!rkey) return h("p", { class: "muted small" }, t("web.ui.evidence.empty"));
  const data = store.evidence[rkey];
  if (!data) return h("p", { class: "muted small" }, t("web.ui.loading"));
  return h("pre", { class: "json" }, JSON.stringify(data, null, 2));
}

function responseLine(ev, ctx) {
  const { h } = ctx;
  const r = ev.response;
  if (!r) return null;
  return h("div", { class: "evidence-current" },
    h("span", { class: "chip chip-brand" }, r.rkey),
    h("span", { class: "evidence-intent" }, r.intent),
    h("span", { class: "chip" }, r.status));
}

export function update(root, state, ctx) {
  if (!parts) mount(root, ctx);
  const { t } = ctx;
  const ev = state.evidence;
  if (!ev) return;
  const opens = new Set(ev.open_by_default || []);
  parts.title.textContent = t("web.ui.evidence.title");
  const empty = !ev.response && !ev.kernel;
  const sections = [
    section(ctx, "kernel", t("web.ui.evidence.section.kernel"), opens.has("kernel"), [kernelSection(ev, ctx)]),
    section(ctx, "consistency", t("web.ui.evidence.section.consistency"), opens.has("consistency"), [consistencySection(ev, ctx)]),
    section(ctx, "dispatch", t("web.ui.evidence.section.dispatch"), opens.has("dispatch"), [dispatchSection(ev, ctx)]),
    section(ctx, "planner", t("web.ui.evidence.section.planner"), false, [plannerSection(ev, ctx)]),
    ev.gate_record
      ? section(ctx, "gate", t("web.ui.evidence.section.gate"), false, [ctx.h("p", { class: "pre-line" }, ev.gate_record)])
      : null,
    section(ctx, "safety", t("web.ui.evidence.section.safety"), false, [
      ctx.h("dl", { class: "kv-list" },
        row(ctx, t("web.ui.evidence.safety_state"), ev.safety ? ev.safety.state : "—"),
        row(ctx, t("web.ui.evidence.safety_reason"), ev.safety && ev.safety.reason ? ev.safety.reason : "—")),
    ]),
    section(ctx, "audit", t("web.ui.evidence.section.audit"), false, [auditSection(ev, ctx)]),
    section(ctx, "intent", t("web.ui.evidence.section.intent"), false, intentSection(ev, ctx)),
    section(ctx, "planner_log", t("web.ui.evidence.section.planner_log"), false, [plannerLogSection(ev, ctx)]),
    section(ctx, "technical", t("web.ui.evidence.section.technical"), false, [technicalSection(ev, ctx)]),
  ];
  render(parts.body,
    empty ? ctx.h("p", { class: "evidence-empty" }, t("web.ui.evidence.empty")) : responseLine(ev, ctx),
    sections);
  parts.foot.textContent = ev.footer || "";
}
