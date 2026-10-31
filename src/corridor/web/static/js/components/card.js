import { h } from "../dom.js";
import { t } from "../i18n.js";
import { tierBadge } from "./tier.js";
import { details } from "./details.js";

function owner(vm, opts) {
  if (opts && opts.owner) return opts.owner;
  return /^T/.test(String(vm.key || "")) ? "device" : "phone";
}

function technical(vm) {
  const blocks = [];
  for (const group of vm.structured || []) {
    blocks.push(
      h("div", { class: "card-structured" },
        h("p", { class: "card-structured-label" }, group.label),
        (group.lines || []).map((line) => h("p", { class: "mono-line" }, line))),
    );
  }
  if (vm.technical) {
    blocks.push(
      details({
        summary: t("web.ui.card.technical"),
        className: "card-tech",
        children: [h("pre", { class: "json" }, JSON.stringify(vm.technical, null, 2))],
      }),
    );
  }
  return blocks;
}

// The card frame holds only what the device shows for signing: title, purpose, amount, tier,
// reason, facts and "why". Notes are rendered by the caller outside this frame.
export function card(vm, opts) {
  const o = opts || {};
  const code = vm.tier && vm.tier.code ? vm.tier.code : "black";
  const body = h(
    "article",
    {
      class: ["approval-card", "edge-" + code, o.size === "device" && "approval-card-device"],
      "data-testid": o.testid || "card",
      "data-tier": code,
      "data-owner": owner(vm, o),
      "data-key": vm.key,
    },
    h("header", { class: "card-head" },
      h("p", { class: "card-title" }, vm.title),
      vm.position ? h("span", { class: "card-position" }, vm.position) : null),
    vm.purpose ? h("p", { class: "card-purpose" }, vm.purpose) : null,
    vm.amount_text ? h("p", { class: "card-amount num", "data-testid": "card-amount" }, vm.amount_text) : null,
    h("div", { class: "card-tier" }, tierBadge(vm.tier)),
    vm.tier_reason ? h("p", { class: "card-reason" }, vm.tier_reason) : null,
    (vm.facts || []).length
      ? h("dl", { class: "card-facts" },
        vm.facts.map((fact) =>
          h("div", { class: "card-fact", "data-testid": "card-fact", "data-key": fact.key },
            h("dt", null, fact.label),
            h("dd", null, fact.value))))
      : null,
    vm.why && (vm.why.lines || []).length
      ? details({
        summary: t("web.ui.card.why"),
        className: "card-why",
        children: vm.why.lines.map((line) => h("p", null, line)),
      })
      : null,
    o.plain ? null : technical(vm),
  );
  if (!o.compact) return body;
  return h(
    "details",
    { class: "card-compact" },
    h("summary", { class: "card-compact-summary" },
      tierBadge(vm.tier),
      h("span", { class: "card-compact-title" }, vm.purpose || vm.title),
      vm.amount_text ? h("span", { class: "num card-compact-amount" }, vm.amount_text) : null),
    body,
  );
}
