// Receipt and failure blocks, plus the small DOM helpers shared by the
// business screens. Everything renders server-provided text as-is.

export function el(ctx, tag, attrs, ...children) {
  const flat = [];
  const walk = (items) => {
    for (const item of items) {
      if (item === null || item === undefined || item === false) continue;
      if (Array.isArray(item)) walk(item);
      else flat.push(item);
    }
  };
  walk(children);
  return ctx.h(tag, attrs || {}, ...flat);
}

// Shell strings arrive after the state they belong to; anything rendered
// with t() is keyed on this tag so it is redrawn once they are in.
export function textTag(ctx) {
  const i18n = ctx.store.i18n;
  if (!i18n) return "none";
  return `${i18n.locale}|${i18n.view}|${Object.keys(i18n.strings || {}).length}`;
}

export function setFlags(node, { checked, disabled } = {}) {
  if (checked !== undefined) node.checked = Boolean(checked);
  if (disabled !== undefined) node.disabled = Boolean(disabled);
  return node;
}

// Controls that must not fire while a request is in flight carry
// data-lock; data-off remembers whether the server state disabled them.
export function lockable(node, off) {
  node.dataset.lock = "1";
  node.dataset.off = off ? "1" : "0";
  node.disabled = Boolean(off);
  return node;
}

export function applyBusy(root, inFlight) {
  for (const node of root.querySelectorAll("[data-lock]")) {
    node.disabled = Boolean(inFlight) || node.dataset.off === "1";
  }
  root.toggleAttribute("aria-busy", Boolean(inFlight));
}

export function button(ctx, opts) {
  const node = ctx.components.button({ kind: "secondary", ...opts, disabled: Boolean(opts.disabled) });
  return lockable(node, opts.disabled);
}

export function lineList(ctx, items, cls, testid) {
  if (!items || !items.length) return null;
  const attrs = { class: cls || "scr-lines" };
  if (testid) attrs["data-testid"] = testid;
  return el(ctx, "ul", attrs, items.map((line) => el(ctx, "li", {}, line)));
}

export function para(ctx, text, cls, testid) {
  if (text === null || text === undefined || text === "") return null;
  const attrs = { class: cls || "scr-p" };
  if (testid) attrs["data-testid"] = testid;
  return el(ctx, "p", attrs, text);
}

export function jsonBlock(ctx, label, value) {
  if (value === null || value === undefined) return null;
  return ctx.components.details({
    summary: label,
    open: false,
    children: [el(ctx, "pre", { class: "scr-json" }, JSON.stringify(value, null, 2))],
  });
}

export function whyBlock(ctx, why) {
  if (!why || !why.lines || !why.lines.length) return null;
  return ctx.components.details({
    summary: ctx.t("web.ui.card.why"),
    open: false,
    children: [lineList(ctx, why.lines, "scr-lines scr-why")],
  });
}

export function billBlock(ctx, bill) {
  if (!bill) return null;
  const totals = (bill.totals || []).map((row) =>
    el(
      ctx,
      "div",
      { class: "scr-kv" },
      el(ctx, "span", { class: "scr-kv-label" }, row.label),
      el(ctx, "span", { class: "scr-kv-value scr-num" }, row.value),
      row.source ? el(ctx, "span", { class: "scr-judge-tag" }, row.source) : null,
    ),
  );
  const anomalies = (bill.anomalies || []).map((item) =>
    el(
      ctx,
      "div",
      { class: "scr-anomaly" },
      el(
        ctx,
        "div",
        { class: "scr-anomaly-head" },
        el(ctx, "span", { class: "scr-anomaly-title" }, item.title),
        el(ctx, "span", { class: "scr-num" }, item.amount),
      ),
      el(ctx, "div", { class: "scr-muted" }, item.date),
      el(ctx, "div", {}, item.reasons),
    ),
  );
  return el(
    ctx,
    "div",
    { class: "scr-bill", "data-testid": "bill" },
    totals.length ? el(ctx, "div", { class: "scr-totals" }, totals) : null,
    bill.categories && bill.categories.length ? ctx.components.bars(bill.categories) : null,
    anomalies.length
      ? el(
          ctx,
          "section",
          { class: "scr-anomalies", "aria-label": ctx.t("web.ui.bill.anomalies") },
          el(ctx, "h4", { class: "scr-h4" }, ctx.t("web.ui.bill.anomalies")),
          anomalies,
        )
      : null,
    para(ctx, bill.empty_text, "scr-empty"),
  );
}

export function subscriptionsBlock(ctx, subs) {
  if (!subs) return null;
  return el(
    ctx,
    "div",
    { class: "scr-subs", "data-testid": "subscriptions" },
    para(ctx, subs.snapshot_text, "scr-muted"),
    lineList(ctx, subs.renewals, "scr-lines scr-renewals"),
    subs.rows && subs.rows.length
      ? el(
          ctx,
          "ul",
          { class: "scr-rows" },
          subs.rows.map((row) =>
            el(
              ctx,
              "li",
              {},
              el(ctx, "span", {}, row.text),
              row.cells ? el(ctx, "span", { class: "scr-judge-tag" }, row.cells.join(" | ")) : null,
            ),
          ),
        )
      : null,
    para(ctx, subs.empty_text, "scr-empty"),
    para(ctx, subs.footer, "scr-foot"),
  );
}

function transactionsBlock(ctx, table) {
  if (!table || !table.rows) return null;
  return el(
    ctx,
    "div",
    { class: "scr-table-wrap" },
    el(
      ctx,
      "table",
      { class: "scr-table" },
      el(ctx, "thead", {}, el(ctx, "tr", {}, (table.columns || []).map((c) => el(ctx, "th", {}, c)))),
      el(
        ctx,
        "tbody",
        {},
        table.rows.map((row) => el(ctx, "tr", {}, row.map((c) => el(ctx, "td", {}, c)))),
      ),
    ),
  );
}

export function resultBlock(ctx, vm, opts = {}) {
  if (!vm) return null;
  // A settled bank receipt leads with the amount it records, under a check mark; the
  // banner's words then sit under the amount instead of in a box of their own.
  const hero = Boolean(vm.amount_text && vm.banner && vm.banner.tone === "success");
  const banner = vm.banner
    ? el(
        ctx,
        "div",
        {
          class: hero ? "scr-receipt-status" : `scr-banner scr-tone-${vm.banner.tone}`,
          "data-testid": "result-banner",
          "data-tone": vm.banner.tone,
          role: vm.banner.tone === "error" ? "alert" : "status",
        },
        vm.banner.text,
      )
    : null;
  const nodes = (vm.nodes || []).map((node) =>
    el(ctx, "li", { class: "scr-result-node" }, resultBlock(ctx, node, { nested: true })),
  );
  const headline = hero
    ? el(
        ctx,
        "div",
        { class: "scr-receipt-hero", "data-testid": "receipt-hero" },
        el(ctx, "span", { class: "scr-receipt-check", "aria-hidden": "true" }, ctx.icons.named("check", 22)),
        el(
          ctx,
          "div",
          { class: "scr-receipt-main" },
          el(ctx, "span", { class: "scr-receipt-amount scr-num" }, vm.amount_text),
          banner,
        ),
      )
    : null;
  return el(
    ctx,
    "div",
    { class: opts.nested ? "scr-result scr-result-nested" : "scr-result" },
    headline,
    hero ? null : banner,
    para(ctx, vm.receipt_line, "scr-receipt-line scr-num", "receipt-line"),
    lineList(ctx, vm.facts_lines, "scr-lines scr-facts"),
    lineList(ctx, vm.message_lines, "scr-lines"),
    nodes.length ? el(ctx, "ol", { class: "scr-result-nodes" }, nodes) : null,
    billBlock(ctx, vm.bill),
    subscriptionsBlock(ctx, vm.subscriptions),
    transactionsBlock(ctx, vm.transactions),
    lineList(ctx, vm.stop_receipt_lines, "scr-lines scr-stop-receipt", "stop-receipt"),
    whyBlock(ctx, vm.why),
    vm.rule_ids && vm.rule_ids.length
      ? el(
          ctx,
          "div",
          { class: "scr-rule-ids" },
          vm.rule_ids.map((id) => el(ctx, "span", { class: "scr-judge-tag" }, id)),
        )
      : null,
    jsonBlock(ctx, ctx.t("web.ui.judge.json_receipt"), vm.receipt),
    jsonBlock(ctx, ctx.t("web.ui.judge.json_audit"), vm.audit_checkpoint),
  );
}

export function failureBlock(ctx, vm) {
  if (!vm) return null;
  return el(
    ctx,
    "div",
    { class: `scr-failure scr-failure-${vm.kind}`, "data-testid": "failure-block", "data-kind": vm.kind, role: "alert" },
    lineList(ctx, vm.lines, "scr-lines"),
    para(ctx, vm.cancel_hint, "scr-hint"),
  );
}

// Tab pages fetch their payload (a pure GET) when visible and stale.
const loading = new Set();

export function ensureTab(ctx, state, name) {
  const payload = ctx.store.tabs ? ctx.store.tabs[name] : null;
  const stale = !payload || payload.revision < state.revision;
  const key = `${state.generation}:${state.revision}:${name}`;
  if (stale && !loading.has(key)) {
    loading.add(key);
    // A failed load is not retried for the same revision, so an error
    // toast cannot drive a request loop.
    Promise.resolve(ctx.actions.loadTab(name)).then(
      () => loading.delete(key),
      () => {},
    );
  }
  return payload || null;
}

export function isActiveTab(ctx, name) {
  return Boolean(ctx.store.ui && ctx.store.ui.tab === name);
}

export function section(ctx, title, testid, ...children) {
  const attrs = { class: "scr-section", "aria-label": title };
  if (testid) attrs["data-testid"] = testid;
  return el(ctx, "section", attrs, el(ctx, "h3", { class: "scr-h3" }, title), ...children);
}
