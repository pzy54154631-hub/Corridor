// Screen reader announcements and focus containment for sheets and drawers.

let region = null;
const traps = [];

function liveRegion() {
  if (!region || !region.isConnected) {
    region = document.createElement("div");
    region.className = "sr-only";
    region.setAttribute("aria-live", "polite");
    region.setAttribute("role", "status");
    region.setAttribute("data-testid", "a11y-announce");
    document.body.appendChild(region);
  }
  return region;
}

export function announce(text) {
  if (!text) return;
  const el = liveRegion();
  el.textContent = "";
  // A fresh text node after clearing makes repeated messages audible.
  setTimeout(() => {
    el.textContent = String(text);
  }, 30);
}

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]), summary';

function onKey(event) {
  const trap = traps[traps.length - 1];
  if (!trap) return;
  if (event.key === "Escape" && trap.onEscape) {
    event.preventDefault();
    trap.onEscape();
    return;
  }
  if (event.key !== "Tab") return;
  const items = [...trap.el.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null);
  if (!items.length) {
    event.preventDefault();
    trap.el.focus();
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && (document.activeElement === first || !trap.el.contains(document.activeElement))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

export function trapFocus(el, opts) {
  if (traps.some((trap) => trap.el === el)) return;
  if (!traps.length) document.addEventListener("keydown", onKey, true);
  traps.push({ el, back: document.activeElement, onEscape: opts && opts.onEscape });
  const target = (opts && opts.initial) || el.querySelector("[data-autofocus]") || el;
  if (!target.hasAttribute("tabindex") && target === el) el.setAttribute("tabindex", "-1");
  target.focus({ preventScroll: false });
}

export function release(el) {
  const index = el ? traps.findIndex((trap) => trap.el === el) : traps.length - 1;
  if (index < 0) return;
  const [trap] = traps.splice(index, 1);
  if (!traps.length) document.removeEventListener("keydown", onKey, true);
  if (trap.back && trap.back.isConnected && typeof trap.back.focus === "function") trap.back.focus();
}

export function trapped(el) {
  return traps.some((trap) => trap.el === el);
}

export function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
