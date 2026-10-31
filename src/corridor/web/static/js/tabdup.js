// Duplicate-tab detection. "Duplicate tab" copies sessionStorage, so two tabs would share one
// server session; the later page gives up its tab and asks the server for a new one.

const CHANNEL = "corridor-web-tab";
const instance = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
let channel = null;

function open() {
  if (channel) return channel;
  if (typeof BroadcastChannel !== "function") return null;
  try {
    channel = new BroadcastChannel(CHANNEL);
  } catch {
    channel = null;
  }
  return channel;
}

// Answer other pages that announce the tab this page holds.
export function watch(currentTab) {
  const ch = open();
  if (!ch) return;
  ch.addEventListener("message", (event) => {
    const msg = event.data || {};
    if (msg.type === "hello" && msg.id !== instance && msg.tab && msg.tab === currentTab()) {
      ch.postMessage({ type: "taken", tab: msg.tab, id: instance });
    }
  });
}

// Resolves true when another live page already holds this tab.
export function probe(tab, wait = 200) {
  const ch = open();
  if (!ch || !tab) return Promise.resolve(false);
  return new Promise((resolve) => {
    let taken = false;
    const onMessage = (event) => {
      const msg = event.data || {};
      if (msg.type === "taken" && msg.tab === tab && msg.id !== instance) taken = true;
    };
    ch.addEventListener("message", onMessage);
    ch.postMessage({ type: "hello", tab, id: instance });
    setTimeout(() => {
      ch.removeEventListener("message", onMessage);
      resolve(taken);
    }, wait);
  });
}
