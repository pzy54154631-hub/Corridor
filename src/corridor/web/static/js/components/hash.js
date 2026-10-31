import { h } from "../dom.js";
import { t } from "../i18n.js";
import { named } from "../icons.js";

async function copy(text, target) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const range = document.createRange();
    range.selectNodeContents(target);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return false;
  }
}

// First 16 hex digits with algorithm and truncation stated; full value on demand.
export function hash(vm, opts) {
  const o = opts || {};
  if (!vm) {
    return h("div", { class: "hash hash-none", "data-testid": o.testid, "data-status": "not_yet" },
      o.label ? h("p", { class: "hash-label" }, o.label) : null,
      o.emptyText ? h("p", { class: "hash-empty" }, o.emptyText) : null);
  }
  const full = h("code", { class: "hash-full mono" }, vm.full);
  const state = h("span", { class: "hash-copied", role: "status" });
  return h(
    "div",
    { class: ["hash", o.status && "hash-" + o.status], "data-testid": o.testid, "data-status": o.status || null },
    o.label ? h("p", { class: "hash-label" }, o.label) : null,
    h("p", { class: "hash-row" },
      h("span", { class: "sr-only" }, t("web.ui.hash.sr")),
      h("code", { class: "hash-short mono" }, vm.short),
      o.showAlgorithm === false ? null : h("span", { class: "hash-algo" }, vm.algorithm)),
    h("details", { class: "hash-more" },
      h("summary", { "data-testid": "hash-expand" }, t("web.ui.hash.expand")),
      full,
      h("button", {
        type: "button",
        class: "btn btn-ghost btn-sm",
        "aria-label": t("web.ui.hash.copy"),
        on: {
          click: async () => {
            const ok = await copy(vm.full, full);
            state.textContent = ok ? t("web.ui.hash.copied") : "";
          },
        },
      }, named("copy", 16), h("span", null, t("web.ui.hash.copy"))),
      state),
  );
}
