import { h } from "../dom.js";
import { t } from "../i18n.js";
import { named } from "../icons.js";

let seq = 0;

// Bottom sheet, side drawer or centred dialog. The caller owns open/close and focus trapping.
export function sheet({ title, testid, side = "bottom", onClose, children, footer, className }) {
  seq += 1;
  const titleId = "sheet-title-" + seq;
  return h(
    "section",
    {
      class: ["sheet", "sheet-" + side, className],
      role: "dialog",
      "aria-modal": side === "dialog" ? "true" : "false",
      "aria-labelledby": titleId,
      "data-testid": testid,
      tabindex: "-1",
    },
    h("header", { class: "sheet-head" },
      side === "bottom" ? h("span", { class: "sheet-grip", "aria-hidden": "true" }) : null,
      h("h2", { class: "sheet-title", id: titleId, tabindex: "-1", "data-autofocus": "" }, title),
      onClose
        ? h("button", {
          type: "button",
          class: "icon-btn",
          "aria-label": t("web.ui.close"),
          "data-testid": testid ? testid + "-close" : null,
          on: { click: onClose },
        }, named("close", 18))
        : null),
    h("div", { class: "sheet-body" }, children),
    footer ? h("footer", { class: "sheet-foot" }, footer) : null,
  );
}

export function scrim({ onClose, className }) {
  return h("div", { class: ["scrim", className], "aria-hidden": "true", on: onClose ? { click: onClose } : {} });
}
