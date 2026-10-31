// Shell strings come from /api/i18n; business text arrives already localized in AppState.

import { store } from "./store.js";

export function t(key, vars) {
  const strings = store.i18n && store.i18n.strings;
  let text = strings && Object.prototype.hasOwnProperty.call(strings, key) ? strings[key] : key;
  if (vars) {
    text = text.replace(/\{([a-z_][a-z0-9_]*)\}/gi, (whole, name) =>
      Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole,
    );
  }
  return text;
}

export function matches(i18n, state) {
  return Boolean(i18n && state && i18n.locale === state.locale && i18n.view === state.view);
}
