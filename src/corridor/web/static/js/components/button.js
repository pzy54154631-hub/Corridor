import { h } from "../dom.js";
import { t } from "../i18n.js";
import { named } from "../icons.js";

// kind: primary | secondary | ghost | danger; size: md | lg | device.
export function button({ label, kind = "secondary", size = "md", testid, disabled, busy, onClick, icon, ariaLabel, attrs }) {
  const text = busy ? t("web.ui.device.busy") : label;
  return h(
    "button",
    {
      type: "button",
      class: ["btn", "btn-" + kind, "btn-" + size, busy && "is-busy"],
      "data-testid": testid,
      disabled: Boolean(disabled || busy),
      "aria-busy": busy ? "true" : null,
      "aria-label": ariaLabel || null,
      ...(attrs || {}),
      on: onClick ? { click: onClick } : {},
    },
    busy ? h("span", { class: "spinner", "aria-hidden": "true" }) : icon ? named(icon, 18) : null,
    text ? h("span", { class: "btn-label" }, text) : null,
  );
}
