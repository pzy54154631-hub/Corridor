// Live-model panel (S7, contract 5.3): deployment, access code, switch, and the reason,
// boundary, cap and note lines, all localized by the server. The access code never leaves
// this panel's field: the field is cleared on submit, and the unlock action sends only a
// proof over a fresh single-use challenge. Switching takes effect at the server's next
// transaction boundary; the panel only shows what the server says.

let field = null;
let seen = null;

function submit(ctx) {
  const code = field.value;
  field.value = "";
  if (code) ctx.actions.liveUnlock(code);
}

// One access field for the whole page: a redraw moves it, so typing is never lost.
function codeField(ctx) {
  if (!field) {
    field = ctx.h("input", {
      id: "live-code-field",
      type: "password",
      class: "scr-composer-input",
      "data-testid": "live-code",
      autocomplete: "off",
      maxlength: "256",
      on: {
        keydown: (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            submit(ctx);
          }
        },
      },
    });
  }
  return field;
}

function line(h, text, testid, extra) {
  if (!text) return null;
  return h("p", { class: ["small", "pre-line", extra], "data-testid": testid || null }, text);
}

function badge(h, vm) {
  return h(
    "div",
    {},
    h(
      "p",
      { class: ["planner-badge", "badge-" + vm.badge.kind], "data-kind": vm.badge.kind },
      h("span", { class: "dot", "aria-hidden": "true" }),
      vm.badge.text,
    ),
  );
}

function warning(c, text) {
  return text ? c.notice({ level: "warning", text }, { compact: true }) : null;
}

// One group of the panel; empty groups are left out.
function group(h, ...children) {
  const shown = children.flat().filter(Boolean);
  return shown.length ? h("div", { class: "scr-card-unit" }, shown) : null;
}

function deploymentLabel(ctx, vm) {
  const key = "web.ui.live.deployment";
  const text = ctx.t(key);
  return text === key ? vm.title : text;
}

// With the switch off the panel still says what it is for: a scripted demo now, how a
// live model would be switched in, and that the checks stay the same either way.
function scriptedState(ctx, judge) {
  const { h, icons, t } = ctx;
  return h(
    "div",
    { class: "live-idle", "data-testid": "live-idle" },
    h("span", { class: "idle-icon", "aria-hidden": "true" }, icons.named("spark", 32)),
    h("h3", { class: "live-idle-title" }, t("web.ui.live.idle_title")),
    ["web.ui.live.idle_1", "web.ui.live.idle_2", "web.ui.live.idle_3"].map((key) =>
      h("p", { class: "live-idle-line" }, t(key))),
    judge ? h("p", { class: "live-idle-line" }, t("web.ui.judge.live_checks")) : null,
    h("p", { class: "live-idle-motto" }, t("web.ui.live.motto")),
  );
}

function panel(vm, ctx, state) {
  const { h, components: c, actions, store } = ctx;
  const notice = warning(c, vm.notice);
  if (!vm.enabled) {
    const judge = state && state.view === "judge";
    return [
      scriptedState(ctx, judge),
      group(
        h,
        h(
          "div",
          { "data-testid": "live-off" },
          line(h, vm.off_text),
          line(h, vm.reason_text, "live-reason"),
        ),
      ),
      group(h, badge(h, vm), notice),
      judge && state.evidence
        ? c.button({
          label: ctx.t("web.ui.judge.live_evidence"),
          kind: "secondary",
          testid: "live-to-evidence",
          onClick: () => store.set({ ui: { liveOpen: false, evidenceOpen: true, evidenceFocus: "planner" } }),
        })
        : null,
    ];
  }
  // As on the Streamlit page: the deployment changes only at the boundary, with the live
  // client off (the server answers 409 otherwise).
  const fixed = !vm.boundary || vm.badge.kind === "model";
  const select = h(
    "select",
    {
      id: "live-deployment-field",
      class: ["scr-composer-input", fixed && "muted"],
      "data-testid": "live-deployment",
      disabled: fixed,
      on: { change: (event) => actions.liveSelect(event.target.value) },
    },
    vm.deployments.map((item) =>
      h(
        "option",
        { value: item.id },
        item.credential_text ? item.label + " · " + item.credential_text : item.label,
      )),
  );
  select.value = vm.selected || "";
  const code = codeField(ctx);
  code.disabled = Boolean(vm.unlock.disabled);
  code.classList.toggle("muted", code.disabled);
  const on = Boolean(vm.toggle.on);
  return [
    group(h, line(h, vm.intro)),
    group(
      h,
      h("label", { class: "field-label", for: "live-deployment-field" }, deploymentLabel(ctx, vm)),
      select,
      line(h, vm.price, null, "muted"),
    ),
    group(
      h,
      h("label", { class: "field-label", for: "live-code-field" }, vm.unlock.label),
      h(
        "div",
        { class: "scr-composer-row" },
        code,
        c.button({
          label: vm.unlock.button,
          kind: "secondary",
          testid: "live-unlock",
          disabled: vm.unlock.disabled,
          onClick: () => submit(ctx),
        }),
      ),
      vm.unlock.result_text
        ? h(
          "p",
          { class: "small", role: "status", "data-testid": "live-result" },
          vm.unlock.result_text,
        )
        : null,
      warning(c, vm.revoked_text),
    ),
    group(
      h,
      h(
        "button",
        {
          type: "button",
          role: "switch",
          class: ["btn", "btn-md", on ? "btn-primary" : "btn-secondary"],
          "aria-checked": on ? "true" : "false",
          "data-testid": "live-toggle",
          "data-on": on ? "true" : "false",
          disabled: vm.toggle.disabled,
          on: { click: () => actions.liveToggle(!on) },
        },
        vm.toggle.label,
      ),
      line(h, vm.reason_text, "live-reason"),
      line(h, vm.boundary_text),
    ),
    group(h, line(h, vm.caps_text), line(h, vm.kernel_note), line(h, vm.no_secret)),
    group(h, badge(h, vm), line(h, vm.scope_note), notice),
    group(h, (vm.counters || []).map((text) => line(h, text, null, "muted"))),
  ];
}

export function mount(root, ctx) {
  root.setAttribute("data-live", "panel");
}

export function update(root, state, ctx) {
  const { h, store, actions, t } = ctx;
  const vm = store.live;
  const at = state.generation + ":" + state.revision;
  if (vm && seen && seen.vm === vm && seen.at !== at) {
    // A business request ran since the panel was read: read it again (a GET only).
    seen.at = at;
    actions.liveOpen();
  } else if (vm && (!seen || seen.vm !== vm)) {
    seen = { vm, at };
  }
  // A field no longer on the page belonged to a closed panel: nothing typed there is kept.
  if (field !== null && !field.isConnected) field.value = "";
  const focused = field !== null && document.activeElement === field;
  root.replaceChildren(
    h(
      "div",
      {
        class: ["live-sheet", "scr-cards"],
        "data-testid": "live-sheet",
        "data-enabled": vm && vm.enabled ? "true" : "false",
      },
      vm ? panel(vm, ctx, state) : h("p", { class: "muted" }, t("web.ui.loading")),
    ),
  );
  // The field moved into the redrawn sheet; give it back its focus once that is shown.
  if (focused) queueMicrotask(() => field.focus({ preventScroll: true }));
}
