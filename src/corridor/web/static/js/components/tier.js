import { h } from "../dom.js";
import { outcome, tier } from "../icons.js";

const CODES = new Set(["green", "yellow", "red", "black"]);

// Tier is shape + text + colour, never colour alone.
export function tierBadge(vm, opts) {
  if (!vm) return null;
  const code = CODES.has(vm.code) ? vm.code : "black";
  // A finished action keeps its tier name only, drawn quietly: nothing is asked any more.
  const settled = Boolean(opts && opts.settled);
  return h(
    "span",
    {
      class: ["tier", "tier-" + code, opts && opts.large && "tier-lg", settled && "tier-settled"],
      "data-tier": code,
      "data-settled": settled ? "true" : null,
      "data-testid": opts && opts.testid,
    },
    tier(code, opts && opts.large ? 16 : 13),
    h("span", { class: "tier-label" }, settled ? opts.label || vm.label : vm.label),
  );
}

const OUTCOMES = { Accept: "accept", Escalate: "escalate", Reject: "reject" };

export function outcomeBadge(name, label) {
  const tone = OUTCOMES[name] || "none";
  return h(
    "span",
    { class: ["outcome", "outcome-" + tone], "data-outcome": name || "none" },
    outcome(name, 14),
    h("span", null, label || name || "—"),
  );
}
