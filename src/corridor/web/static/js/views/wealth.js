// Wealth tab (ext 4): single-product comparison and purchase, holdings
// and redemption, and the isolated suitability refusal demo. Suitability
// and refusals are decided by the server; a refusal never has a gesture.

import { cardWithNotes } from "./response.js";
import {
  applyBusy,
  button,
  el,
  ensureTab,
  isActiveTab,
  jsonBlock,
  lineList,
  lockable,
  para,
  resultBlock,
  section,
  setFlags,
  textTag,
} from "./result.js";

// Rebuild hook set by update(); segmented buttons show the picked value.
let refresh = () => {};

// Form drafts only: the objective and product picked before pressing a
// button. The server's objective value is the default.
const draft = { generation: null, objective: null, product: null };

function resetDraft(state, vm) {
  if (draft.generation !== state.generation) {
    draft.generation = state.generation;
    draft.objective = null;
    draft.product = null;
  }
  if (!draft.objective) draft.objective = vm.objective.value;
  const candidates = vm.comparison ? vm.comparison.candidates : [];
  if (draft.product && !candidates.some((c) => c.key === draft.product)) draft.product = null;
}

function radioGroup(ctx, name, options, current, testid, onPick) {
  return options.map((opt, i) => {
    const input = el(ctx, "input", {
      type: "radio",
      name,
      value: opt.value,
      "data-testid": testid(opt, i),
      on: { change: () => onPick(opt.value) },
    });
    setFlags(input, { checked: current === opt.value });
    lockable(input, false);
    return el(ctx, "label", { class: "scr-radio scr-radio-lg" }, input, el(ctx, "span", {}, opt.label));
  });
}

export function refusalBlock(ctx, vm) {
  if (!vm) return null;
  return el(
    ctx,
    "div",
    { class: "scr-refusal", "data-testid": "refusal-card", role: "status" },
    el(ctx, "h4", { class: "scr-h4" }, vm.title),
    para(ctx, vm.lead),
    cardWithNotes(ctx, vm.card),
    lineList(ctx, vm.reasons, "scr-lines scr-refusal-reasons"),
    para(ctx, vm.dispatches_text, "scr-hint"),
    para(ctx, vm.isolation, "scr-hint"),
    vm.outcome ? el(ctx, "span", { class: "scr-judge-tag" }, vm.outcome) : null,
    vm.rule_ids ? el(ctx, "div", { class: "scr-rule-ids" }, vm.rule_ids.map((id) => el(ctx, "span", { class: "scr-judge-tag" }, id))) : null,
  );
}

function comparison(ctx, vm) {
  const cmp = vm.comparison;
  const objective = ctx.components.segmented({
    options: vm.objective.options.map((opt) => ({ ...opt, testid: `wealth-objective-${opt.value}` })),
    value: draft.objective,
    label: ctx.t("web.ui.wealth.compare"),
    onChange: (value) => {
      draft.objective = value;
      if (ctx.store.state) refresh();
    },
  });
  const compare = button(ctx, {
    label: ctx.t("web.ui.wealth.compare"),
    kind: "primary",
    testid: "wealth-compare",
    onClick: () => ctx.actions.wealthCompare(draft.objective),
  });
  if (!cmp) return el(ctx, "div", { class: "scr-section" }, objective, compare);
  const rows = cmp.rows.map((row) =>
    el(
      ctx,
      "div",
      { class: `scr-product${row.chosen ? " scr-product-chosen" : ""}`, "data-product": row.key },
      el(
        ctx,
        "div",
        { class: "scr-product-head" },
        el(ctx, "span", { class: "scr-product-name" }, row.name),
        row.chosen ? el(ctx, "span", { class: "scr-chip" }, ctx.t("web.ui.wealth.chosen")) : null,
      ),
      row.cells.map((cell) =>
        el(
          ctx,
          "div",
          { class: "scr-kv" },
          el(ctx, "span", { class: "scr-kv-label" }, cell.label),
          el(ctx, "span", { class: "scr-kv-value scr-num" }, cell.value),
        ),
      ),
    ),
  );
  const exclusions = cmp.exclusions.length
    ? ctx.components.details({
        summary: ctx.t("web.ui.wealth.exclusions"),
        open: false,
        children: [
          el(
            ctx,
            "ul",
            { class: "scr-rows" },
            cmp.exclusions.map((x) => el(ctx, "li", {}, el(ctx, "strong", {}, x.name), " ", x.reasons)),
          ),
        ],
      })
    : null;
  const candidates = el(
    ctx,
    "fieldset",
    { class: "scr-candidates" },
    el(ctx, "legend", { class: "scr-gate-legend" }, ctx.t("web.ui.wealth.other")),
    radioGroup(
      ctx,
      "wealth-product",
      cmp.candidates.map((c) => ({ value: c.key, label: c.label })),
      draft.product,
      (opt, i) => `wealth-product-${i}`,
      (value) => {
        draft.product = value;
      },
    ),
  );
  return el(
    ctx,
    "div",
    { class: "scr-section" },
    objective,
    compare,
    para(ctx, cmp.banner, "scr-banner scr-tone-info"),
    rows,
    exclusions,
    cmp.bic ? jsonBlock(ctx, ctx.t("web.ui.judge.json"), cmp.bic) : null,
    candidates,
    button(ctx, {
      label: ctx.t("web.ui.wealth.purchase"),
      kind: "primary",
      testid: "wealth-purchase",
      onClick: () => ctx.actions.wealthPurchase(draft.product, cmp.candidates_version),
    }),
  );
}

function pendingCard(ctx, item) {
  if (!item) return null;
  if (item.refusal) return refusalBlock(ctx, item.refusal);
  return el(
    ctx,
    "div",
    { class: "scr-section" },
    cardWithNotes(ctx, item.card),
    item.task ? para(ctx, ctx.t("web.ui.device.look"), "scr-device-look-text") : null,
  );
}

function positions(ctx, vm) {
  if (!vm.positions.length) return null;
  return section(
    ctx,
    ctx.t("web.ui.wealth.positions"),
    null,
    vm.positions.map((pos, i) =>
      el(
        ctx,
        "div",
        { class: "scr-position" },
        el(ctx, "div", { class: "scr-product-name" }, pos.label),
        para(ctx, pos.caption, "scr-muted"),
        para(ctx, pos.from_text, "scr-muted scr-num"),
        button(ctx, {
          label: ctx.t("web.ui.wealth.redeem"),
          kind: "secondary",
          testid: `wealth-redeem-${i}`,
          disabled: !pos.redeemable,
          onClick: () => ctx.actions.wealthRedeem(pos.key, vm.positions_version),
        }),
      ),
    ),
  );
}

function render(ctx, state, vm) {
  resetDraft(state, vm);
  return [
    el(
      ctx,
      "div",
      { class: "scr-section" },
      para(ctx, vm.intro),
      para(ctx, vm.scope_note, "scr-hint scr-scope", "wealth-scope-note"),
      vm.live_scope_note ? para(ctx, vm.live_scope_note, "scr-hint") : null,
    ),
    comparison(ctx, vm),
    pendingCard(ctx, vm.purchase),
    positions(ctx, vm),
    pendingCard(ctx, vm.redemption),
    vm.results.map((r) => resultBlock(ctx, r)),
    el(
      ctx,
      "div",
      { class: "scr-section scr-r4" },
      button(ctx, {
        label: ctx.t("web.ui.wealth.r4"),
        kind: "ghost",
        testid: "wealth-r4",
        onClick: () => ctx.actions.presetR4(),
      }),
      refusalBlock(ctx, vm.r4),
    ),
  ]
    .flat()
    .filter(Boolean);
}

export function mount(root, ctx) {
  const body = el(ctx, "div", { class: "scr-tabpage scr-wealth" });
  root.append(body);
  root.__wealth = { body, sig: null };
  // Hidden screens are not updated by the shell; drop this tab's content as
  // soon as another tab is chosen so no duplicate controls stay around.
  ctx.store.subscribe(() => {
    if (!isActiveTab(ctx, "wealth") && root.__wealth.sig !== null) {
      body.replaceChildren();
      root.__wealth.sig = null;
    }
  });
}

export function update(root, state, ctx) {
  const refs = root.__wealth;
  if (!refs) return;
  if (!isActiveTab(ctx, "wealth") || !state.capabilities.tabs.includes("wealth")) {
    refs.body.replaceChildren();
    refs.sig = null;
    return;
  }
  const payload = ensureTab(ctx, state, "wealth");
  refresh = () => {
    refs.sig = null;
    update(root, ctx.store.state, ctx);
  };
  const sig = JSON.stringify([textTag(ctx), state.generation, state.locale, state.view, payload]);
  if (sig !== refs.sig) {
    refs.sig = sig;
    if (!payload) {
      refs.body.replaceChildren(el(ctx, "div", { class: "scr-skeleton", role: "status" }, ctx.t("web.ui.loading")));
    } else {
      refs.body.replaceChildren(...render(ctx, state, payload.vm));
    }
  }
  applyBusy(refs.body, ctx.store.ui && ctx.store.ui.inFlight);
}
