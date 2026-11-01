// Isolated, read-only mechanism demonstration. Replays animate the fetched report.
export function openRogue(ctx) {
  const { h, t } = ctx;
  if (document.querySelector('[data-testid="rogue-panel"]')) return;
  const origin = document.activeElement;
  const originId = origin?.getAttribute("data-testid");
  let timer = null;
  let report = null;
  const columns = ["category", "method", "rules", "verdict", "dispatch", "twin"];
  const rows = h("tbody", { class: "rogue-rows", "data-testid": "rogue-rows" });
  const table = h("table", { class: "rogue-table" },
    h("caption", null, t("web.ui.rogue.caption")),
    h("thead", null, h("tr", null, ...columns.map(key =>
      h("th", { scope: "col" }, t("web.ui.rogue.column." + key))))), rows);
  const summary = h("p", { class: "sr-only", role: "status", "data-testid": "rogue-summary" });
  const stats = ["blocked", "dispatches", "controls"].map((key) => {
    const value = h("strong", { "data-testid": "rogue-stat-" + key }, t("web.ui.rogue.checking"));
    return { value, node: h("div", { class: "rogue-stat" }, h("span", null, t("web.ui.rogue.stat." + key)), value) };
  });
  const replay = h("button", {
    type: "button", class: "btn btn-primary", "data-testid": "rogue-replay", disabled: true,
    on: { click: () => play() },
  }, t("web.ui.rogue.replay"));
  const close = h("button", {
    type: "button", class: "btn btn-secondary", "data-testid": "rogue-close",
    on: { click: () => dialog.close() },
  }, t("web.ui.close"));
  const dialog = h("dialog", {
    class: "rogue-panel", "data-testid": "rogue-panel", "aria-labelledby": "rogue-title",
    on: { keydown: (event) => {
      if (event.key !== "Tab") return;
      const focusable = [...dialog.querySelectorAll("button:not(:disabled), summary")];
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    }, close: () => {
      clearTimeout(timer);
      dialog.remove();
      const returnTo = origin?.isConnected ? origin : originId
        ? document.querySelector(`[data-testid="${CSS.escape(originId)}"]`) : null;
      returnTo?.focus({ preventScroll: true });
    } },
  },
  h("article", { class: "rogue-paper" }, h("header", { class: "rogue-banner" }, h("div", { class: "rogue-head" },
    h("h2", { id: "rogue-title" }, t("web.ui.rogue.title")), close),
  h("p", null, t("web.ui.rogue.intro"))),
  h("div", { class: "rogue-body" },
    h("div", { class: "rogue-stats", "data-testid": "rogue-stats", "aria-busy": "true" }, ...stats.map(s => s.node)),
    table, summary,
    h("p", { class: "rogue-limit", "data-testid": "rogue-limit" }, t("web.ui.rogue.limit")),
    h("p", { class: "rogue-prior small" }, t("web.ui.rogue.prior")),
    h("footer", { class: "rogue-footer" }, replay))));

  function renderRow(row, pending) {
    const state = pending ? "pending" : row.passed ? "ok" : "failed";
    const verdict = row.status === "Escalate"
      ? (row.attack_dispatches === 0 ? "escalated" : "escalated_dispatched")
      : ["Reject", "rejected"].includes(row.status) ? "rejected" : "check_failed";
    const cell = (key, ...children) => h("td", { "data-label": t("web.ui.rogue.column." + key) }, ...children);
    return h("tr", { "data-testid": "rogue-row", class: "rogue-" + state },
      h("th", { scope: "row", "data-label": t("web.ui.rogue.column.category") }, t("web.ui.rogue.category." + row.attack)),
      cell("method", t("web.ui.rogue.attack." + row.attack), h("details", null,
        h("summary", null, t("web.ui.rogue.technical")),
        h("p", null, t("web.ui.rogue.details", {
          rule: row.expected_rule, effects: row.prohibited_effects,
          violation: t(row.oracle_violation ? "web.ui.rogue.yes" : "web.ui.rogue.no"),
          prior: row.legitimate_prior_effects,
        })))),
      cell("rules", h("code", null, row.rule_ids.join(" · "))),
      cell("verdict", t("web.ui.rogue." + verdict)),
      cell("dispatch", String(row.attack_dispatches)),
      cell("twin", t(row.benign_completed ? "web.ui.rogue.control_ok" : "web.ui.rogue.control_failed")));
  }
  function finish() {
    const s = report.summary;
    [s.blocked + "/" + s.attacks, s.attack_dispatches, s.benign_completed + "/" + s.attacks]
      .forEach((value, index) => { stats[index].value.textContent = String(value); });
    stats[0].node.classList.toggle("rogue-stat-failed", s.blocked !== s.attacks);
    stats[1].node.classList.toggle("rogue-stat-failed", s.attack_dispatches !== 0);
    stats[2].node.classList.toggle("rogue-stat-failed", s.benign_completed !== s.attacks);
    dialog.querySelector(".rogue-stats").setAttribute("aria-busy", "false");
    summary.textContent = t("web.ui.rogue.summary", s);
    replay.disabled = false;
  }
  function play() {
    if (!report || !dialog.open) return;
    clearTimeout(timer);
    rows.replaceChildren();
    finish();
    replay.disabled = true;
    if (ctx.a11y.reducedMotion() || report.rows.length === 0) {
      rows.append(...report.rows.map(row => renderRow(row, false)));
      finish();
      return;
    }
    let index = 0;
    function advance() {
      if (!dialog.open) return;
      const row = report.rows[index++];
      rows.append(renderRow(row, false));
      timer = setTimeout(() => {
        if (!dialog.open) return;
        if (index < report.rows.length) advance();
        else finish();
      }, 500);
    }
    advance();
  }
  document.body.append(dialog);
  dialog.showModal();
  close.focus();
  fetch("/api/rogue", { credentials: "same-origin", cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) throw new Error("report unavailable");
      const data = await response.json();
      if (!data.ok) throw new Error("report unavailable");
      if (!dialog.open) return;
      report = data.rogue;
      play();
    })
    .catch(() => {
      if (!dialog.open) return;
      summary.classList.remove("sr-only");
      summary.textContent = t("web.ui.rogue.error");
      dialog.querySelector(".rogue-stats").setAttribute("aria-busy", "false");
      stats.forEach(s => { s.value.textContent = "—"; });
    });
}
