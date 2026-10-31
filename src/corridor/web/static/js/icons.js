// Inline SVG icons. Shapes, not colour alone, carry tier and outcome meaning.

import { svg } from "./dom.js";

function frame(size, ...children) {
  return svg(
    "svg",
    {
      class: "icon",
      viewBox: "0 0 24 24",
      width: size,
      height: size,
      "aria-hidden": "true",
      focusable: "false",
    },
    ...children,
  );
}

const line = { fill: "none", stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" };

function stroke(size, ...paths) {
  return frame(size, ...paths.map((d) => svg("path", { d, ...line })));
}

const TIER = {
  green: () => svg("circle", { cx: 12, cy: 12, r: 7, fill: "currentColor" }),
  yellow: () => svg("path", { d: "M12 3.5 20.5 12 12 20.5 3.5 12Z", fill: "currentColor" }),
  red: () => svg("path", { d: "M12 3.8 21 19.6H3Z", fill: "currentColor", "stroke-linejoin": "round" }),
  black: () => svg("rect", { x: 5, y: 5, width: 14, height: 14, rx: 1.5, fill: "currentColor" }),
};

const GLYPH = { circle: "green", diamond: "yellow", triangle: "red", square: "black" };

export function tier(code, size = 14) {
  const draw = TIER[code] || TIER[GLYPH[code]] || TIER.black;
  return frame(size, draw());
}

export function outcome(name, size = 14) {
  if (name === "Accept") {
    return frame(size, svg("circle", { cx: 12, cy: 12, r: 9, fill: "currentColor" }),
      svg("path", { d: "m7.8 12.3 2.8 2.8 5.6-5.8", fill: "none", stroke: "#fff", "stroke-width": 2.2, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  }
  if (name === "Escalate") {
    return frame(size, svg("path", { d: "M12 2.5 21.5 12 12 21.5 2.5 12Z", fill: "currentColor" }),
      svg("path", { d: "M12 7.5v5.5M12 16.2v.1", fill: "none", stroke: "#fff", "stroke-width": 2.2, "stroke-linecap": "round" }));
  }
  if (name === "Reject") {
    return frame(size, svg("rect", { x: 3, y: 3, width: 18, height: 18, rx: 3, fill: "currentColor" }),
      svg("path", { d: "m8.5 8.5 7 7m0-7-7 7", fill: "none", stroke: "#fff", "stroke-width": 2.2, "stroke-linecap": "round" }));
  }
  return frame(size, svg("circle", { cx: 12, cy: 12, r: 8, ...line }));
}

const PATHS = {
  lock: ["M7 11V8a5 5 0 0 1 10 0v3", "M5.5 11h13v9.5h-13z", "M12 15v2.5"],
  shield: ["M12 3 19.5 6v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6Z", "m8.8 12 2.3 2.3 4.3-4.6"],
  device: ["M7 2.8h10a1.8 1.8 0 0 1 1.8 1.8v14.8a1.8 1.8 0 0 1-1.8 1.8H7a1.8 1.8 0 0 1-1.8-1.8V4.6A1.8 1.8 0 0 1 7 2.8Z", "M10 18h4"],
  sms: ["M4.5 5h15a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5H10l-4.5 3.5V17h-1A1.5 1.5 0 0 1 3 15.5v-9A1.5 1.5 0 0 1 4.5 5Z", "M7.5 9.5h9M7.5 12.5h5.5"],
  stop: ["M8 6h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Z"],
  reset: ["M4.5 12a7.5 7.5 0 1 0 2.2-5.3", "M4.5 4.5v4h4"],
  close: ["m6.5 6.5 11 11m0-11-11 11"],
  chevron: ["m9 6 6 6-6 6"],
  copy: ["M9 9h10v11H9z", "M5 15V4h10"],
  external: ["M14 4h6v6", "M20 4l-9 9", "M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"],
  send: ["M4 12 20 4l-4.5 16-4-6.5Z", "M11.5 13.5 20 4"],
  attach: ["M16.5 7.5 9 15a2.1 2.1 0 0 1-3-3l8-8a3.5 3.5 0 0 1 5 5l-8.2 8.2a5 5 0 0 1-7-7L11 3.5"],
  transfer: ["M4 8h14l-3.5-3.5", "M20 16H6l3.5 3.5"],
  cards: ["M3.5 6.5h17v11h-17z", "M3.5 10h17", "M7 14.5h3"],
  scam_drill: ["M12 3 19.5 6v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6Z", "M12 8v4.5", "M12 15.8v.1"],
  billing: ["M6 3.5h12v17l-2.5-1.5-2 1.5-1.5-1.5-1.5 1.5-2-1.5L6 20.5z", "M9 8h6M9 11.5h6M9 15h3.5"],
  subscriptions: ["M4.5 12a7.5 7.5 0 0 1 13-5.1", "M19.5 12a7.5 7.5 0 0 1-13 5.1", "M17.5 3.5v3.4h-3.4", "M6.5 20.5v-3.4h3.4"],
  birthday: ["M4.5 11h15v9.5h-15z", "M3.5 7.5h17V11h-17z", "M12 7.5v13", "M12 7.5c-2-3.5-5-3-4.5-1s4.5 1 4.5 1Zm0 0c2-3.5 5-3 4.5-1s-4.5 1-4.5 1Z"],
  wealth: ["M4 19.5h16", "M6.5 16v-4M11 16V8.5M15.5 16v-6M20 16V5.5"],
  assistant: ["M5 5.5h14a1.5 1.5 0 0 1 1.5 1.5v8.5A1.5 1.5 0 0 1 19 17h-7l-4.5 3.5V17H5a1.5 1.5 0 0 1-1.5-1.5V7A1.5 1.5 0 0 1 5 5.5Z", "M8.5 11.2h.1M12 11.2h.1M15.5 11.2h.1"],
  accounts: ["M3.5 9 12 4l8.5 5", "M5.5 10v8M10 10v8M14 10v8M18.5 10v8", "M3.5 20h17"],
  plans: ["M5 5h5v5H5zM14 14h5v5h-5z", "M10 7.5h4.5a2 2 0 0 1 2 2V14"],
  info: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M12 11v5.5", "M12 7.8v.1"],
  warning: ["M12 3.8 21 19.6H3Z", "M12 10v4.5", "M12 17.2v.1"],
  check: ["m5.5 12.5 4.2 4.2L18.5 8"],
  error: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "m9 9 6 6m0-6-6 6"],
  user: ["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M4.5 20.5a7.5 7.5 0 0 1 15 0"],
  evidence: ["M7 3.5h7.5L19 8v12.5H7z", "M14.5 3.5V8H19", "M10 12.5h6M10 16h6"],
  menu: ["M4.5 7h15M4.5 12h15M4.5 17h15"],
  globe: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M3.5 12h17", "M12 3c2.5 2.6 3.7 5.6 3.7 9s-1.2 6.4-3.7 9c-2.5-2.6-3.7-5.6-3.7-9S9.5 5.6 12 3Z"],
  headset: ["M4.5 14v-2a7.5 7.5 0 0 1 15 0v2", "M4.5 13.5h3v5h-3zM16.5 13.5h3v5h-3z", "M19.5 18.5c0 1.5-1.8 2.5-5 2.5"],
};

export function named(name, size = 18) {
  return stroke(size, ...(PATHS[name] || PATHS.info));
}

export const lock = (size) => named("lock", size);
export const shield = (size) => named("shield", size);

// Status bar glyphs for the simulated phone.
export function signal(size = 16) {
  return frame(size, ...[4, 8, 12, 16].map((x, i) =>
    svg("rect", { x: x - 1, y: 18 - (i + 1) * 3.2, width: 2.6, height: (i + 1) * 3.2, rx: 0.8, fill: "currentColor" })));
}

export function battery(size = 22) {
  return svg("svg", { class: "icon", viewBox: "0 0 28 14", width: size, height: size * 0.5, "aria-hidden": "true", focusable: "false" },
    svg("rect", { x: 1, y: 1.5, width: 22, height: 11, rx: 3, fill: "none", stroke: "currentColor", "stroke-width": 1.3 }),
    svg("rect", { x: 3, y: 3.5, width: 16, height: 7, rx: 1.6, fill: "currentColor" }),
    svg("rect", { x: 24.5, y: 5, width: 2, height: 4, rx: 1, fill: "currentColor" }));
}

// Abstract monogram for the fictional demo bank; it imitates no real institution.
export function mark(size = 28) {
  return svg("svg", { class: "icon mark", viewBox: "0 0 32 32", width: size, height: size, "aria-hidden": "true", focusable: "false" },
    svg("rect", { x: 1, y: 1, width: 30, height: 30, rx: 9, fill: "currentColor" }),
    svg("path", { d: "M9 12.5h11.5l-3-3M23 19.5H11.5l3 3", fill: "none", stroke: "#f3d39c", "stroke-width": 2.2, "stroke-linecap": "round", "stroke-linejoin": "round" }));
}
