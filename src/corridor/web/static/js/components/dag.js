import { h, svg } from "../dom.js";

const W = 168;
const H = 54;
const GAP_X = 44;
const GAP_Y = 18;

function clip(text, max) {
  const chars = [...String(text || "")];
  return chars.length > max ? chars.slice(0, max - 1).join("") + "…" : chars.join("");
}

// Task graph drawn with SVG; every label goes in through text nodes.
export function dag(vm, opts) {
  const nodes = (vm && vm.nodes) || [];
  if (!nodes.length) return h("div", { class: "dag-empty" });
  const layers = new Map();
  for (const node of nodes) {
    if (!layers.has(node.layer)) layers.set(node.layer, []);
    layers.get(node.layer).push(node);
  }
  const order = [...layers.keys()].sort((a, b) => a - b);
  const place = new Map();
  let rows = 0;
  order.forEach((layer, column) => {
    const items = layers.get(layer).sort((a, b) => a.order - b.order);
    items.forEach((node, row) => place.set(node.key, { x: 8 + column * (W + GAP_X), y: 8 + row * (H + GAP_Y) }));
    rows = Math.max(rows, items.length);
  });
  const width = 16 + order.length * W + (order.length - 1) * GAP_X;
  const height = 16 + rows * H + (rows - 1) * GAP_Y;
  const edges = ((vm && vm.edges) || []).map(([from, to]) => {
    const a = place.get(from);
    const b = place.get(to);
    if (!a || !b) return null;
    const x1 = a.x + W;
    const y1 = a.y + H / 2;
    const x2 = b.x;
    const y2 = b.y + H / 2;
    const mid = (x1 + x2) / 2;
    return svg("path", { class: "dag-edge", d: `M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`, fill: "none" });
  });
  const boxes = nodes.map((node) => {
    const p = place.get(node.key);
    return svg("g", { class: ["dag-node", "tone-" + node.tone], transform: `translate(${p.x} ${p.y})`, "data-key": node.key, "data-status": node.status },
      svg("rect", { width: W, height: H, rx: 12 }),
      svg("circle", { class: "dag-dot", cx: 16, cy: 18, r: 4 }),
      svg("text", { class: "dag-label", x: 28, y: 22 }, clip(node.label, 11)),
      svg("text", { class: "dag-status", x: 16, y: 42 }, clip(node.status_text, 14)));
  });
  return h(
    "figure",
    { class: "dag", "data-testid": opts && opts.testid },
    svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": opts && opts.label ? opts.label : null, preserveAspectRatio: "xMidYMin meet" },
      edges, boxes),
    h("ul", { class: "sr-only" }, nodes.map((node) => h("li", null, node.label + " · " + node.status_text))),
    vm.blocked_text ? h("figcaption", { class: "dag-blocked" }, vm.blocked_text) : null,
  );
}
