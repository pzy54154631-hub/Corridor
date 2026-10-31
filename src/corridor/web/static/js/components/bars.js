import { h } from "../dom.js";

function ratio(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  return Math.min(1, Math.max(0, n));
}

// Horizontal bars; widths follow the server-supplied decimal ratio string.
export function bars(categories, opts) {
  return h(
    "ul",
    { class: "bars", "data-testid": opts && opts.testid },
    (categories || []).map((item) => {
      const fill = h("span", { class: "bar-fill" });
      fill.style.setProperty("--ratio", String(ratio(item.ratio)));
      return h("li", { class: "bar" },
        h("span", { class: "bar-label" }, item.label),
        h("span", { class: "bar-track", "aria-hidden": "true" }, fill),
        h("span", { class: "bar-value num" }, item.value));
    }),
  );
}
