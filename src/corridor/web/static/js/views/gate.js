// The two self-check questions. Level, decision, pause copy and the R7
// preset all come from GateVM; this module only draws them and forwards
// answers. The box sits outside the approval card.

import { button, el, lineList, lockable, para, setFlags } from "./result.js";

function question(ctx, gate, rvm, q) {
  const disabled = !gate.interactive || gate.locked;
  const options = q.options.map((opt) => {
    const input = el(ctx, "input", {
      type: "radio",
      name: `gate-${rvm.rkey}-${q.name}`,
      value: opt.value,
      "data-testid": `gate-q-${q.name}-${opt.value}`,
      on: {
        change: () => ctx.actions.gateAnswer(rvm.rkey, gate.version, { [q.name]: opt.value }),
      },
    });
    setFlags(input, { checked: q.answer === opt.value });
    lockable(input, disabled);
    return el(ctx, "label", { class: "scr-radio scr-radio-lg" }, input, el(ctx, "span", {}, opt.label));
  });
  return el(
    ctx,
    "fieldset",
    { class: "scr-gate-q", "data-question": q.name },
    el(ctx, "legend", { class: "scr-gate-legend" }, q.label),
    options,
  );
}

// On an earlier reply the questions stay in the document (read-only) but
// are folded away; the way back is the prominent control.
function questions(ctx, gate, rvm) {
  const body = [
    el(ctx, "h4", { class: "scr-h4" }, gate.questions_title),
    gate.questions.map((q) => question(ctx, gate, rvm, q)),
  ];
  if (gate.interactive) return body;
  return ctx.components.details({ summary: gate.questions_title, open: false, children: body.slice(1) });
}

function pauseBox(ctx, gate, rvm) {
  const p = gate.pause;
  if (!p) return null;
  return el(
    ctx,
    "div",
    { class: "scr-gate-pause", "data-testid": "gate-pause", role: "status" },
    el(ctx, "h4", { class: "scr-h4" }, p.title),
    para(ctx, p.copy),
    p.self_report && p.self_report !== gate.self_report_note ? para(ctx, p.self_report, "scr-hint") : null,
    para(ctx, p.cancel_hint, "scr-hint"),
    gate.interactive
      ? button(ctx, {
          label: p.retry_label,
          kind: "secondary",
          testid: "gate-retry",
          onClick: () => ctx.actions.gateRetry(rvm.rkey, gate.version),
        })
      : null,
  );
}

export function renderGate(ctx, gate, rvm) {
  if (!gate) return null;
  const notSigned = rvm.cards && rvm.cards.length ? rvm.cards[0].not_signed_label : null;
  const judge = gate.judge
    ? el(
        ctx,
        "div",
        { class: "scr-judge-block" },
        para(ctx, gate.judge.note, "scr-judge-line"),
        para(ctx, gate.judge.record, "scr-judge-line scr-mono"),
        para(ctx, gate.judge.preset_note, "scr-judge-line"),
      )
    : null;
  return el(
    ctx,
    "section",
    {
      class: "scr-gate",
      "data-testid": "gate",
      "data-decision": gate.decision,
      "data-preset": String(Boolean(gate.preset)),
      "data-interactive": String(Boolean(gate.interactive)),
      "aria-label": gate.title,
    },
    el(
      ctx,
      "div",
      { class: "scr-gate-head" },
      el(ctx, "h3", { class: "scr-gate-title" }, gate.title),
      notSigned ? el(ctx, "span", { class: "scr-not-signed" }, notSigned) : null,
    ),
    para(ctx, gate.subtitle, "scr-muted"),
    gate.facts && gate.facts.length
      ? el(
          ctx,
          "div",
          { class: "scr-gate-facts" },
          el(ctx, "h4", { class: "scr-h4" }, gate.facts_title),
          lineList(ctx, gate.facts),
        )
      : null,
    para(ctx, gate.cues, "scr-gate-cues"),
    questions(ctx, gate, rvm),
    gate.notice ? el(ctx, "p", { class: "scr-gate-notice", role: "alert" }, gate.notice) : null,
    pauseBox(ctx, gate, rvm),
    para(ctx, gate.self_report_note, "scr-hint", "gate-self-report"),
    para(ctx, gate.footer, "scr-gate-footer"),
    judge,
    gate.interactive
      ? null
      : button(ctx, {
          label: ctx.t("web.ui.gate.back"),
          kind: "secondary",
          testid: "gate-back",
          onClick: () => ctx.actions.makeCurrent(rvm.rkey),
        }),
  );
}
