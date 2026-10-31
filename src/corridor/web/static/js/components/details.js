import { h } from "../dom.js";
import { named } from "../icons.js";

// Disclosure built on <details>; data-open mirrors the open state for tests.
export function details({ summary, open, testid, children, onToggle, className, extra }) {
  const el = h(
    "details",
    { class: ["disclosure", className], "data-testid": testid, "data-open": open ? "true" : "false" },
    h("summary", { class: "disclosure-summary" },
      h("span", { class: "disclosure-chevron", "aria-hidden": "true" }, named("chevron", 16)),
      h("span", { class: "disclosure-title" }, summary),
      extra || null),
    h("div", { class: "disclosure-body" }, children),
  );
  el.open = Boolean(open);
  el.addEventListener("toggle", () => {
    el.setAttribute("data-open", el.open ? "true" : "false");
    if (onToggle) onToggle(el.open);
  });
  return el;
}
