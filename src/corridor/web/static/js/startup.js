// Session startup alone: no catalog or server state is required to explain a failure.
import { h, render } from "./dom.js";
import { t } from "./i18n.js";
import { store } from "./store.js";

// Embedded from messages-web-ui.json for startup before /api/i18n is available.
const FALLBACK = {
  "web.ui.boot.retry": {
    "en": "We are experiencing high traffic. Please wait. This page will retry automatically in {seconds} seconds.",
    "zh-CN": "\u5f53\u524d\u8bbf\u95ee\u8f83\u591a\uff0c\u8bf7\u7a0d\u5019\u3002\u9875\u9762\u5c06\u5728 {seconds} \u79d2\u540e\u81ea\u52a8\u91cd\u8bd5\u3002"
  },
  "web.ui.boot.retry_now": {
    "en": "Retry now",
    "zh-CN": "\u7acb\u5373\u91cd\u8bd5"
  }
};

function locale() {
  return store.state?.locale || (navigator.language.toLowerCase().startsWith("en") ? "en" : "zh-CN");
}

function text(key, seconds) {
  if (store.i18n?.locale === locale() && store.i18n.strings?.[key]) return t(key, { seconds });
  return FALLBACK[key][locale()].replace("{seconds}", String(seconds));
}

export function waitToRetry(root, retryAfter, failures) {
  // The service sends delta-seconds. Invalid/missing values use five seconds; never spin
  // on a zero delay. Exponential backoff caps at 60s, but never shortens a server delay.
  const parsed = /^\d+$/.test(retryAfter || "") ? Number(retryAfter) : NaN;
  const base = Number.isSafeInteger(parsed) ? Math.max(1, parsed) : 5;
  const seconds = Math.max(base, Math.min(60, base * 2 ** Math.min(failures - 1, 6)));
  // Allow one immediate manual retry. Repeated failures also throttle the button.
  const manualDelay = failures === 1 ? 0 : Math.min(seconds, 5 * 2 ** Math.min(failures - 2, 4), 60);
  return new Promise((resolve) => {
    const start = performance.now();
    let finished = false;
    let timer;
    const finish = () => {
      if (finished) return;
      finished = true;
      clearInterval(timer);
      retry.disabled = true;
      resolve();
    };
    const message = h("p", { role: "status", "data-testid": "boot-retry-message" });
    const retry = h("button", {
      type: "button", class: "btn btn-primary btn-md", "data-testid": "boot-retry-now",
      on: { click: () => { if (!retry.disabled) finish(); } },
    }, text("web.ui.boot.retry_now"));
    const update = () => {
      const elapsed = (performance.now() - start) / 1000;
      const left = Math.max(0, Math.ceil(seconds - elapsed));
      message.textContent = text("web.ui.boot.retry", left);
      retry.disabled = elapsed < manualDelay;
      if (!left) finish();
    };
    document.documentElement.lang = locale();
    render(root, h("main", { class: "boot", "data-testid": "boot-retry" },
      h("section", { class: "boot-retry-panel" }, message, retry)));
    update();
    timer = setInterval(update, 250);
  });
}
