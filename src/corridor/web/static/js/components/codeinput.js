import { h } from "../dom.js";

let seq = 0;

// Six-digit code field. Six digits submit the code only, 150 ms later; the final confirmation
// remains a separate gesture on the device.
export function codeInput({ label, testid, onComplete, errorText, hint, disabled }) {
  seq += 1;
  const id = "code-" + seq;
  const errorId = id + "-error";
  let timer = null;
  let sent = null;
  const error = h("p", { class: "field-error", id: errorId, role: "alert" }, errorText || "");
  const input = h("input", {
    id,
    class: "code-input mono",
    type: "text",
    inputmode: "numeric",
    autocomplete: "one-time-code",
    maxlength: "6",
    "aria-describedby": errorText ? errorId : null,
    "aria-invalid": errorText ? "true" : null,
    "data-testid": testid,
    disabled: Boolean(disabled),
    on: {
      input: () => {
        const digits = input.value.replace(/[^0-9]/g, "").slice(0, 6);
        if (digits !== input.value) input.value = digits;
        if (timer) clearTimeout(timer);
        if (digits.length === 6 && digits !== sent && onComplete) {
          timer = setTimeout(() => {
            timer = null;
            if (input.value === digits) {
              sent = digits;
              onComplete(digits);
            }
          }, 150);
        }
      },
    },
  });
  const el = h(
    "div",
    { class: "code-field" },
    h("label", { class: "field-label", for: id }, label),
    input,
    hint ? h("p", { class: "field-hint" }, hint) : null,
    error,
  );
  el.codeValue = () => input.value;
  el.input = input;
  el.clear = () => {
    input.value = "";
    sent = null;
  };
  el.setError = (text) => {
    error.textContent = text || "";
    if (text) {
      input.setAttribute("aria-describedby", errorId);
      input.setAttribute("aria-invalid", "true");
    } else {
      input.removeAttribute("aria-describedby");
      input.removeAttribute("aria-invalid");
    }
  };
  el.setDisabled = (value) => {
    input.disabled = Boolean(value);
  };
  return el;
}
