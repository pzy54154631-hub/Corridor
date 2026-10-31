import { h } from "../dom.js";

// options: [{value, label, testid?}]
export function segmented({ options, value, onChange, testid, label, disabled }) {
  return h(
    "div",
    { class: "segmented", role: "group", "aria-label": label || null, "data-testid": testid },
    options.map((option) =>
      h("button", {
        type: "button",
        class: ["segment", option.value === value && "is-on"],
        "aria-pressed": option.value === value ? "true" : "false",
        "data-testid": option.testid,
        "data-value": option.value,
        disabled: Boolean(disabled),
        on: {
          click: () => {
            if (option.value !== value && onChange) onChange(option.value);
          },
        },
      }, option.label)),
  );
}
