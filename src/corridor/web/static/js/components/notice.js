import { h } from "../dom.js";
import { named } from "../icons.js";

const KIND_TONE = {
  suitability: "info",
  fraud: "warning",
  recipient_context: "info",
  irreversible: "warning",
  red: "danger",
  schedule_scope: "info",
};
const LEVEL_TONE = { info: "info", success: "success", warning: "warning", error: "danger" };
const TONE_ICON = { info: "info", success: "check", warning: "warning", danger: "warning", gate: "shield" };

// Page notes are drawn outside the card frame and say they are not part of what is signed.
export function notice(vm, opts) {
  const o = opts || {};
  if (vm && vm.kind) {
    const tone = KIND_TONE[vm.kind] || "info";
    const inner = vm.kind === "suitability" ? "note-suitability" : vm.kind === "fraud" ? "note-fraud" : null;
    return h(
      "section",
      { class: ["note", "note-" + tone], "data-testid": o.testid || "card-note", "data-kind": vm.kind },
      h("div", { class: "note-icon", "aria-hidden": "true" }, named(TONE_ICON[tone], 18)),
      h(
        "div",
        { class: "note-body", "data-testid": inner },
        vm.title ? h("p", { class: "note-title" }, vm.title) : null,
        (vm.lines || []).map((line) => h("p", { class: "note-line" }, line)),
        vm.footer ? h("p", { class: "note-footer" }, vm.footer) : null,
        o.notSignedLabel ? h("p", { class: "not-signed" }, o.notSignedLabel) : null,
      ),
    );
  }
  const tone = LEVEL_TONE[vm && vm.level] || o.tone || "info";
  const lines = Array.isArray(vm && vm.lines) ? vm.lines : [vm && vm.text];
  return h(
    "div",
    {
      class: ["note", "note-" + tone, o.compact && "note-compact"],
      "data-testid": o.testid,
      role: tone === "danger" && o.alert ? "alert" : null,
    },
    h("div", { class: "note-icon", "aria-hidden": "true" }, named(TONE_ICON[tone], 18)),
    h(
      "div",
      { class: "note-body" },
      vm && vm.title ? h("p", { class: "note-title" }, vm.title) : null,
      lines.filter(Boolean).map((line) => h("p", { class: "note-line" }, line)),
      o.notSignedLabel ? h("p", { class: "not-signed" }, o.notSignedLabel) : null,
    ),
  );
}
