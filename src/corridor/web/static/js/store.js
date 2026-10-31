// Single page store. Views read from it; only api.js and actions.js write to it.

const listeners = new Set();
let scheduled = false;

export const store = {
  state: null,
  i18n: null,
  tabs: {},
  live: null,
  lab: null,
  labPhone: null,
  evidence: {},
  ui: {
    tab: "assistant",
    deviceOpen: false,
    evidenceOpen: false,
    liveOpen: false,
    labOpen: false,
    labPhoneOpen: false,
    labPhoneBusy: false,
    inFlight: false,
    draft: { text: "", attach: false },
  },
  subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  set(patch) {
    for (const [key, value] of Object.entries(patch || {})) {
      if (key === "ui") store.ui = { ...store.ui, ...value };
      else if (key === "tabs" || key === "evidence") {
        store[key] = value === null ? {} : { ...store[key], ...value };
      }
      else store[key] = value;
    }
    notify();
  },
};

function notify() {
  if (scheduled) return;
  scheduled = true;
  queueMicrotask(() => {
    scheduled = false;
    for (const fn of [...listeners]) fn(store);
  });
}
