import { h } from "../dom.js";
import { named } from "../icons.js";

const shown = new Set();
const ICON = { info: "info", success: "check", warning: "warning", error: "error" };
let host = null;

function region() {
  if (!host || !host.isConnected) {
    host = h("div", { class: "toasts", "data-testid": "toast-region" });
    document.body.appendChild(host);
  }
  return host;
}

// Brief notice; ids are de-duplicated so a re-rendered state never repeats a toast.
export function toast(text, opts) {
  const o = opts || {};
  const level = ICON[o.level] ? o.level : "info";
  if (!text) return null;
  if (o.id) {
    if (shown.has(o.id)) return null;
    shown.add(o.id);
  }
  const el = h(
    "div",
    {
      class: ["toast", "toast-" + level],
      role: level === "error" ? "alert" : "status",
      "data-testid": "toast",
      "data-id": o.id || null,
      "data-level": level,
    },
    named(ICON[level], 18),
    h("p", null, text),
  );
  const box = region();
  box.appendChild(el);
  while (box.children.length > 3) box.firstElementChild.remove();
  setTimeout(() => {
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 320);
  }, o.duration || 5200);
  return el;
}
