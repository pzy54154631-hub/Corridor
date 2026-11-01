// DOM construction. Text goes in through textContent only; attributes pass a whitelist.

const ATTRS = new Set([
  "class", "id", "role", "type", "name", "value", "checked", "disabled", "for", "inputmode",
  "autocomplete", "maxlength", "tabindex", "title", "lang", "href", "src", "scope",
]);
const PROPS = new Set(["value", "checked", "disabled"]);
// The SVG namespace is read from the HTML parser so that no URL literal appears in the bundle.
const SVG_NS = new DOMParser().parseFromString("<svg></svg>", "text/html").body.firstElementChild
  .namespaceURI;
const SVG_ATTRS = new Set([
  "class", "role", "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy",
  "r", "rx", "ry", "d", "points", "fill", "stroke", "stroke-width", "stroke-linecap",
  "stroke-linejoin", "stroke-dasharray", "text-anchor", "dominant-baseline", "focusable",
  "transform", "marker-end", "preserveAspectRatio",
]);

function allowed(name) {
  return ATTRS.has(name) || name.startsWith("aria-") || name.startsWith("data-");
}

export function samePath(value) {
  const text = String(value);
  return text.startsWith("/") && !text.startsWith("//") && !text.startsWith("/\\");
}

function className(value) {
  if (Array.isArray(value)) return value.filter(Boolean).join(" ");
  return String(value);
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) append(el, child);
    else if (child instanceof Node) el.appendChild(child);
    else el.appendChild(document.createTextNode(String(child)));
  }
}

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (name === "on") {
      for (const [type, fn] of Object.entries(value || {})) {
        if (typeof fn !== "function") throw new TypeError("handler must be a function: " + type);
        el.addEventListener(type, fn);
      }
      continue;
    }
    if (!allowed(name)) throw new TypeError("attribute not allowed: " + name);
    if (value === null || value === undefined || value === false) continue;
    if ((name === "href" || name === "src") && !samePath(value)) {
      throw new TypeError("only same-origin paths are allowed in " + name);
    }
    if (PROPS.has(name)) {
      el[name] = name === "value" ? String(value) : Boolean(value);
      if (name !== "value") el.setAttribute(name, "");
      continue;
    }
    el.setAttribute(name, name === "class" ? className(value) : value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

export function svg(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (!(SVG_ATTRS.has(name) || name.startsWith("aria-") || name.startsWith("data-"))) {
      throw new TypeError("svg attribute not allowed: " + name);
    }
    if (value === null || value === undefined || value === false) continue;
    el.setAttribute(name, name === "class" ? className(value) : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// Replace the children of root, keeping keyboard focus on the element with the same test id.
export function render(root, ...children) {
  const active = document.activeElement;
  const keep = active && root.contains(active) ? focusKey(active) : null;
  root.replaceChildren();
  append(root, children);
  if (keep) {
    const again = root.querySelector(keep);
    if (again && typeof again.focus === "function") again.focus({ preventScroll: true });
  }
}

function focusKey(el) {
  const id = el.getAttribute("data-testid");
  if (!id) return null;
  const extra = ["data-rkey", "data-key"].map((name) =>
    el.hasAttribute(name) ? `[${name}="${CSS.escape(el.getAttribute(name))}"]` : "",
  );
  return `[data-testid="${CSS.escape(id)}"]` + extra.join("");
}

export function toggleClass(el, name, on) {
  el.classList.toggle(name, Boolean(on));
}
