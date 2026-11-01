// Login screen (W1). Shown only while AppState carries `login`.

import { applyBusy, button, el, para } from "./result.js";
import { openRogue } from "./rogue.js";

export function mount(root, ctx) {
  const body = el(ctx, "div", { class: "scr-login" });
  root.append(body);
  root.__login = { body, sig: null };
  ctx.store.subscribe(() => {
    const state = ctx.store.state;
    if (state && !state.login && root.__login.sig !== null) {
      body.replaceChildren();
      body.hidden = true;
      root.__login.sig = null;
    }
  });
}

// Three plain facts about how the demo bank works, under the headline.
const FACTS = [
  ["cards", "web.ui.login.fact_review.title", "web.ui.login.fact_review.text"],
  ["shield", "web.ui.login.fact_tiers.title", "web.ui.login.fact_tiers.text"],
  ["check", "web.ui.login.fact_receipt.title", "web.ui.login.fact_receipt.text"],
];

function facts(ctx) {
  return el(
    ctx,
    "ul",
    { class: "scr-login-facts", "data-testid": "login-facts" },
    FACTS.map(([icon, title, text]) =>
      el(
        ctx,
        "li",
        { class: "scr-login-fact" },
        el(ctx, "span", { class: "scr-login-fact-icon", "aria-hidden": "true" }, ctx.icons.named(icon, 18)),
        el(
          ctx,
          "span",
          {},
          el(ctx, "span", { class: "scr-login-fact-title" }, ctx.t(title)),
          el(ctx, "span", { class: "scr-login-fact-text" }, ctx.t(text)),
        ),
      ),
    ),
  );
}

export function update(root, state, ctx) {
  const refs = root.__login;
  if (!refs) return;
  const vm = state.login;
  refs.body.hidden = !vm;
  if (!vm) {
    refs.body.replaceChildren();
    refs.sig = null;
    return;
  }
  const sig = JSON.stringify([state.generation, vm, state.session.bank_name,
    state.session.assistant_name, ctx.store.i18n?.locale, ctx.store.i18n?.view]);
  if (sig !== refs.sig) {
    refs.sig = sig;
    refs.body.replaceChildren(
      el(
        ctx,
        "div",
        { class: "scr-login-hero" },
        el(ctx, "div", { class: "scr-login-mark" }, state.session.bank_name),
        el(ctx, "h2", { class: "scr-login-title" }, vm.title),
        para(ctx, ctx.t("web.ui.login.intro"), "scr-login-intro"),
        button(ctx, {
          label: ctx.t("web.ui.rogue.title"), kind: "secondary", size: "lg",
          testid: "rogue-entry", onClick: () => openRogue(ctx),
        }),
        facts(ctx),
      ),
      el(
        ctx,
        "div",
        { class: "scr-login-actions" },
        button(ctx, {
          label: vm.button,
          kind: "primary",
          size: "lg",
          testid: "login-enter",
          onClick: () => ctx.actions.login(),
        }),
        para(ctx, vm.caption, "scr-hint scr-login-caption"),
      ),
    );
  }
  applyBusy(refs.body, ctx.store.ui && ctx.store.ui.inFlight);
}
