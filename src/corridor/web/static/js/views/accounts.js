// Accounts tab (ext 2/3): balances, cards, recent receipts, the
// subscription fallback flow and the latest bill analysis.

import {
  applyBusy,
  billBlock,
  button,
  el,
  ensureTab,
  isActiveTab,
  lockable,
  para,
  section,
  setFlags,
  subscriptionsBlock,
  textTag,
} from "./result.js";

// The radio picked before "prepare cancellation" is a form draft only.
let cancelPick = null;

function balances(ctx, vm) {
  return section(
    ctx,
    ctx.t("web.ui.accounts.balances"),
    "accounts-balances",
    vm.balances.map((row) =>
      el(
        ctx,
        "div",
        { class: "scr-kv" },
        el(ctx, "span", { class: "scr-kv-label" }, row.label),
        el(ctx, "span", { class: "scr-kv-value scr-num" }, row.amount),
      ),
    ),
  );
}

function cards(ctx, vm) {
  if (!vm.cards.length) return null;
  return section(
    ctx,
    ctx.t("web.ui.accounts.cards"),
    null,
    vm.cards.map((row) =>
      el(
        ctx,
        "div",
        { class: "scr-kv" },
        el(ctx, "span", { class: "scr-kv-label" }, row.label),
        el(ctx, "span", { class: "scr-kv-value" }, row.status_text),
      ),
    ),
  );
}

function receipts(ctx, vm) {
  if (!vm.receipts.length) return null;
  return section(
    ctx,
    ctx.t("web.ui.accounts.receipts"),
    null,
    el(
      ctx,
      "ul",
      { class: "scr-rows" },
      vm.receipts.map((r) =>
        el(
          ctx,
          "li",
          { class: "scr-receipt-row" },
          el(ctx, "span", { class: "scr-receipt-title" }, r.title),
          el(ctx, "span", { class: "scr-num" }, r.amount),
          el(ctx, "span", { class: "scr-muted scr-num" }, r.time),
          el(ctx, "span", { class: "scr-status" }, r.status),
        ),
      ),
    ),
  );
}

function cardControls(ctx, vm) {
  const form = vm.card_controls;
  if (!form?.choices.length) return null;
  const select = (options, id) => lockable(el(ctx, "select", { "data-testid": id },
    options.map(item => el(ctx, "option", { value: item.value || item.key }, item.label))), false);
  const card = select(form.choices, "card-choice");
  const kind = select(form.operations, "card-operation");
  const limit = lockable(el(ctx, "input", { type: "text", inputmode: "decimal", "data-testid": "card-limit" }), false);
  const limitRow = el(ctx, "label", { class: "scr-field" }, ctx.t("web.ui.cards.limit"), limit);
  const checks = form.categories.map(item => {
    const input = lockable(el(ctx, "input", { type: "checkbox", value: item.value, "data-testid": `card-restrict-${item.value}` }), false);
    return { input, node: el(ctx, "label", { class: "scr-check" }, input, item.label) };
  });
  const restrictions = el(ctx, "fieldset", {}, el(ctx, "legend", {}, ctx.t("web.ui.cards.restrictions")), checks.map(c => c.node));
  const update = () => {
    limitRow.hidden = !kind.value.endsWith("card_limit");
    restrictions.hidden = !["restrict_card", "relax_card_restriction"].includes(kind.value);
  };
  const selected = () => {
    const row = form.choices.find(c => c.key === card.value);
    checks.forEach(c => { c.input.checked = row.restrictions.includes(c.input.value); });
    update();
  };
  card.addEventListener("change", selected);
  kind.addEventListener("change", update);
  selected();
  return section(ctx, ctx.t("web.ui.cards.controls"), "card-controls",
    el(ctx, "label", { class: "scr-field" }, ctx.t("web.ui.cards.choose"), card),
    el(ctx, "label", { class: "scr-field" }, ctx.t("web.ui.cards.operation"), kind),
    limitRow, restrictions, para(ctx, ctx.t("web.ui.cards.note"), "scr-hint"),
    button(ctx, { label: ctx.t("web.ui.cards.review"), testid: "card-review", kind: "primary", onClick: () => {
      const body = { choice: card.value, kind: kind.value, list_version: form.list_version };
      if (!limitRow.hidden) body.limit = limit.value;
      if (!restrictions.hidden) body.restrictions = checks.filter(c => c.input.checked).map(c => c.input.value);
      ctx.actions.cardPreview(body);
    } }));
}

function subscriptions(ctx, state, vm) {
  const subs = vm.subscriptions;
  const query = vm.sub_query;
  if (!subs && !query) return null;
  const choices = subs ? subs.cancel_choices : [];
  if (cancelPick && !choices.some((c) => c.key === cancelPick)) cancelPick = null;
  let prepare = null;
  const radios = choices.map((choice, i) => {
    const input = el(ctx, "input", {
      type: "radio",
      name: "subs-cancel",
      value: choice.key,
      "data-testid": `subs-cancel-choice-${i}`,
      on: {
        change: () => {
          cancelPick = choice.key;
          if (prepare) {
            prepare.dataset.off = "0";
            prepare.disabled = Boolean(ctx.store.ui && ctx.store.ui.inFlight);
          }
        },
      },
    });
    setFlags(input, { checked: cancelPick === choice.key });
    lockable(input, false);
    return el(ctx, "label", { class: "scr-radio scr-radio-lg" }, input, el(ctx, "span", {}, choice.label));
  });
  if (choices.length) {
    prepare = button(ctx, {
      label: ctx.t("web.ui.subs.cancel"),
      kind: "secondary",
      testid: "subs-cancel",
      disabled: !cancelPick,
      onClick: () => {
        if (cancelPick) ctx.actions.subsCancel(cancelPick, subs.cancel_version);
      },
    });
  }
  return section(
    ctx,
    ctx.t("web.ui.accounts.subscriptions"),
    null,
    query
      ? el(
          ctx,
          "div",
          { class: "scr-choice-row" },
          button(ctx, {
            label: query.label,
            kind: "secondary",
            testid: "subs-query",
            disabled: Boolean(query.task),
            onClick: () => ctx.actions.subsQuery(),
          }),
          query.task ? para(ctx, ctx.t("web.ui.device.look"), "scr-hint") : null,
        )
      : null,
    subscriptionsBlock(ctx, subs),
    radios.length ? el(ctx, "fieldset", { class: "scr-subs-cancel" }, radios, prepare) : null,
  );
}

export function mount(root, ctx) {
  const body = el(ctx, "div", { class: "scr-tabpage scr-accounts" });
  root.append(body);
  root.__accounts = { body, sig: null };
  // Hidden screens are not updated by the shell; drop this tab's content as
  // soon as another tab is chosen so no duplicate controls stay around.
  ctx.store.subscribe(() => {
    if (!isActiveTab(ctx, "accounts") && root.__accounts.sig !== null) {
      body.replaceChildren();
      root.__accounts.sig = null;
    }
  });
}

export function update(root, state, ctx) {
  const refs = root.__accounts;
  if (!refs) return;
  if (!isActiveTab(ctx, "accounts") || !state.capabilities.tabs.includes("accounts")) {
    refs.body.replaceChildren();
    refs.sig = null;
    return;
  }
  const payload = ensureTab(ctx, state, "accounts");
  const sig = JSON.stringify([textTag(ctx), state.generation, state.locale, state.view, payload]);
  if (sig !== refs.sig) {
    refs.sig = sig;
    if (!payload) {
      refs.body.replaceChildren(el(ctx, "div", { class: "scr-skeleton", role: "status" }, ctx.t("web.ui.loading")));
    } else {
      const vm = payload.vm;
      refs.body.replaceChildren(
        ...[
          balances(ctx, vm),
          cards(ctx, vm),
          cardControls(ctx, vm),
          receipts(ctx, vm),
          subscriptions(ctx, state, vm),
          vm.billing ? section(ctx, ctx.t("web.ui.accounts.billing"), null, billBlock(ctx, vm.billing)) : null,
          para(ctx, vm.empty_text, "scr-empty"),
        ].filter(Boolean),
      );
    }
  }
  applyBusy(refs.body, ctx.store.ui && ctx.store.ui.inFlight);
}
