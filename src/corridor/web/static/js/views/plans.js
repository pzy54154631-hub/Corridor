// Plans tab (ext 1): registered plans as task graphs, the birthday draft
// with server-registered node selection, and the operator section. The
// operator block is also used by the assistant tab after a handoff.

import { planSelect } from "./response.js";
import { applyBusy, button, el, ensureTab, isActiveTab, jsonBlock, lineList, para, resultBlock, section, textTag } from "./result.js";

export function operatorBlock(ctx, state, vm) {
  if (!vm) return null;
  const comp = vm.compensation;
  let compensation = null;
  if (comp) {
    compensation = el(
      ctx,
      "div",
      { class: "scr-operator-comp" },
      comp.task || comp.result || !state.capabilities.compensate
        ? null
        : button(ctx, {
            label: comp.label,
            kind: "secondary",
            testid: "operator-compensate",
            onClick: () => ctx.actions.compensate(comp.compensate_version),
          }),
      comp.task || comp.result ? null : para(ctx, comp.compensate_hint, "scr-hint"),
      comp.task ? para(ctx, ctx.t("web.ui.device.look"), "scr-hint") : null,
      resultBlock(ctx, comp.result),
    );
  }
  return el(
    ctx,
    "section",
    { class: "scr-operator", "aria-label": ctx.t("web.ui.plans.operator") },
    el(ctx, "h3", { class: "scr-h3" }, ctx.t("web.ui.plans.operator")),
    para(ctx, vm.intro, "scr-muted"),
    resultBlock(ctx, vm.result),
    el(
      ctx,
      "div",
      { class: "scr-operator-snapshot", "data-testid": "operator-snapshot" },
      lineList(ctx, vm.snapshot_lines),
      jsonBlock(ctx, ctx.t("web.ui.judge.json"), vm.snapshot),
    ),
    compensation,
    para(ctx, vm.note, "scr-hint"),
  );
}

function planRow(ctx, plan) {
  return el(
    ctx,
    "div",
    { class: "scr-plan", "data-plan": plan.key },
    el(ctx, "h4", { class: "scr-h4" }, plan.title),
    el(ctx, "div", { class: "scr-dag", "data-testid": "plans-dag" }, ctx.components.dag(plan.dag)),
    para(ctx, plan.blocked_text, "scr-hint"),
  );
}

export function mount(root, ctx) {
  const body = el(ctx, "div", { class: "scr-tabpage scr-plans" });
  root.append(body);
  root.__plans = { body, sig: null };
  // Hidden screens are not updated by the shell; drop this tab's content as
  // soon as another tab is chosen so no duplicate controls stay around.
  ctx.store.subscribe(() => {
    if (!isActiveTab(ctx, "plans") && root.__plans.sig !== null) {
      body.replaceChildren();
      root.__plans.sig = null;
    }
  });
}

export function update(root, state, ctx) {
  const refs = root.__plans;
  if (!refs) return;
  if (!isActiveTab(ctx, "plans") || !state.capabilities.tabs.includes("plans")) {
    refs.body.replaceChildren();
    refs.sig = null;
    return;
  }
  const payload = ensureTab(ctx, state, "plans");
  const operator = ensureTab(ctx, state, "operator");
  const sig = JSON.stringify([textTag(ctx), state.generation, state.locale, state.view, state.capabilities, payload, operator, state.device.tasks]);
  if (sig !== refs.sig) {
    refs.sig = sig;
    render(ctx, state, refs.body, payload, operator);
  }
  applyBusy(refs.body, ctx.store.ui && ctx.store.ui.inFlight);
}

function render(ctx, state, body, payload, operator) {
  if (!payload) {
    body.replaceChildren(el(ctx, "div", { class: "scr-skeleton", role: "status" }, ctx.t("web.ui.loading")));
    return;
  }
  const vm = payload.vm;
  body.replaceChildren(
    ...[
      vm.plans.length
        ? section(ctx, ctx.t("web.ui.plans.registered"), null, vm.plans.map((plan) => planRow(ctx, plan)))
        : para(ctx, vm.empty_text, "scr-empty"),
      vm.draft ? section(ctx, ctx.t("web.ui.plans.draft"), null, planSelect(ctx, state, vm.draft.select, null)) : null,
      operator ? operatorBlock(ctx, state, operator.vm) : null,
    ].filter(Boolean),
  );
}
