// Isolated, read-only mechanism demonstration. Replays animate the fetched report.
export function openRogue(ctx) {
  const { h, t } = ctx;
  if (document.querySelector('[data-testid="rogue-panel"]')) return;
  const origin = document.activeElement;
  let timer = null;
  let report = null;
  const rows = h("ol", { class: "rogue-rows", "data-testid": "rogue-rows" });
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
      if (origin?.isConnected) origin.focus();
    } },
  },
  h("header", { class: "rogue-banner" }, h("div", { class: "rogue-head" },
    h("h2", { id: "rogue-title" }, t("web.ui.rogue.title")), close),
  h("p", null, t("web.ui.rogue.intro"))),
  h("div", { class: "rogue-body" },
    h("div", { class: "rogue-stats", "data-testid": "rogue-stats", "aria-busy": "true" }, ...stats.map(s => s.node)),
    rows, summary,
    h("p", { class: "rogue-limit", "data-testid": "rogue-limit" }, t("web.ui.rogue.limit")),
    h("footer", { class: "rogue-footer" }, h("p", { class: "small" }, t("web.ui.rogue.prior")), replay)));

  function renderRow(row, pending) {
    const state = pending ? "pending" : row.passed ? "ok" : "failed";
    return h("li", { "data-testid": "rogue-row", class: "rogue-" + state },
      h("div", { class: "rogue-row-head" },
        h("div", { class: "rogue-intent" }, h("span", { class: "rogue-tag rogue-attack-tag" }, t("web.ui.rogue.intent")),
          h("strong", null, t("web.ui.rogue.attack." + row.attack))),
        h("div", { class: "rogue-verdict" },
          h("span", { class: "rogue-tag rogue-verdict-tag" }, t(pending ? "web.ui.rogue.attacking" : row.passed ? "web.ui.rogue.rejected" : "web.ui.rogue.check_failed")),
          pending ? null : h("code", null, row.rule_ids.join(" · ")))),
      pending ? null : h("p", { class: "rogue-result" }, t("web.ui.rogue.dispatches", { dispatches: row.attack_dispatches }), " · ",
        t(row.benign_completed ? "web.ui.rogue.control_ok" : "web.ui.rogue.control_failed")),
      pending ? null : h("details", null,
        h("summary", null, t("web.ui.rogue.technical")),
        h("p", null, t("web.ui.rogue.details", {
          rule: row.expected_rule, effects: row.prohibited_effects,
          violation: t(row.oracle_violation ? "web.ui.rogue.yes" : "web.ui.rogue.no"),
          prior: row.legitimate_prior_effects,
        }))));
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
    summary.textContent = t("web.ui.rogue.checking");
    replay.disabled = true;
    dialog.querySelector(".rogue-stats").setAttribute("aria-busy", "true");
    stats.forEach(s => {
      s.value.textContent = t("web.ui.rogue.checking");
      s.node.classList.remove("rogue-stat-failed");
    });
    if (ctx.a11y.reducedMotion() || report.rows.length === 0) {
      rows.append(...report.rows.map(row => renderRow(row, false)));
      finish();
      return;
    }
    let index = 0;
    function advance() {
      if (!dialog.open) return;
      const row = report.rows[index++];
      const pending = renderRow(row, true);
      rows.append(pending);
      timer = setTimeout(() => {
        if (!dialog.open) return;
        pending.replaceWith(renderRow(row, false));
        if (index < report.rows.length) advance();
        else finish();
      }, 400);
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
