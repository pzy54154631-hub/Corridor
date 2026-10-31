// Model suggestions under a clarification. Each is a sentence the server wrote from a
// fixed template, shown as a question; choosing one sends that sentence as a new message
// through the normal flow (card, device approval). The server decides what is offered,
// whether it is still current and when it expired; nothing here builds or edits text.

import { button, el, lineList, para } from "./result.js";

export function suggestionBlock(ctx, rvm) {
  const vm = rvm.suggestion_select;
  if (!vm) return null;
  const rows = (vm.choices || []).map((choice, i) =>
    el(
      ctx,
      "div",
      { class: "scr-suggest-row", "data-state": choice.state },
      button(ctx, {
        label: choice.label,
        kind: "secondary",
        testid: `suggestion-choice-${i}`,
        disabled: vm.locked || !rvm.current || choice.state !== "offered",
        onClick: () => ctx.actions.suggestionChoice(rvm.rkey, choice.key, vm.list_version),
      }),
      para(ctx, choice.state_text, "scr-hint", `suggestion-state-${i}`),
    ),
  );
  return el(
    ctx,
    "section",
    { class: "scr-birthday scr-suggest", "data-testid": "suggestion-select" },
    el(ctx, "div", { class: "scr-choice-row" }, rows),
    para(ctx, vm.note, "scr-hint", "suggestion-note"),
    lineList(ctx, vm.evidence_lines, "scr-lines scr-judge-block", "suggestion-evidence"),
  );
}
