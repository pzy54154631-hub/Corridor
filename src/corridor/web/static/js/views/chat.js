// Assistant tab: conversation log, shortcuts, gated-elsewhere list, task
// panel, operator snapshot and the composer. Blocks are keyed and only
// rebuilt when their view model changes, so focus, scroll and open
// <details> survive unrelated updates. The composer is never rebuilt.

import { operatorBlock } from "./plans.js";
import { openRogue } from "./rogue.js";
import { renderResponse } from "./response.js";
import { applyBusy, button, el, ensureTab, para, textTag } from "./result.js";

const COMPOSER_MAX_ROWS = 3;

function signature(ctx, state, value) {
  return `${state.generation}|${state.view}|${state.locale}|${textTag(ctx)}|${JSON.stringify(value)}`;
}

function shortcutButtons(ctx, chat, compact, onPick) {
  const regular = chat.shortcuts.filter((s) => s.key !== "scam_drill");
  const wealth = (ctx.store.state?.capabilities.tabs || []).includes("wealth")
    ? button(ctx, {
        label: ctx.t("web.ui.chat.try_wealth"),
        kind: "secondary",
        testid: "shortcut-wealth",
        onClick: () => {
          ctx.store.set({ ui: { tab: "wealth" } });
          ctx.actions.loadTab("wealth");
        },
      })
    : null;
  const drill = chat.shortcuts.find((s) => s.key === "scam_drill");
  const make = (s) =>
    button(ctx, {
      label: s.label,
      kind: "secondary",
      testid: `shortcut-${s.key}`,
      onClick: () => onPick(s.key),
    });
  return el(
    ctx,
    "div",
    { class: compact ? "scr-shortcuts scr-shortcuts-compact" : "scr-shortcuts" },
    regular.length ? el(ctx, "h3", { "data-testid": "try-these" }, ctx.t("web.ui.chat.try_these")) : null,
    regular.length ? el(ctx, "div", { class: "scr-shortcut-grid" }, regular.map(make), wealth) : null,
    drill
      ? el(
          ctx,
          "div",
          { class: "scr-shortcut-drill" },
          make(drill),
          button(ctx, { label: ctx.t("web.ui.rogue.title"), kind: "secondary",
            testid: "rogue-entry", onClick: () => openRogue(ctx) }),
          para(ctx, chat.drill_caption, "scr-hint", "drill-caption"),
        )
      : null,
  );
}

function turnNode(ctx, turn) {
  const role = turn.role;
  return el(
    ctx,
    "div",
    { class: `scr-turn scr-turn-${role}`, "data-testid": `turn-${role}`, "data-rkey": turn.rkey || "" },
    el(ctx, "div", { class: "scr-bubble" }, turn.text),
    para(ctx, turn.attachment_note, "scr-turn-note"),
    para(ctx, turn.remark_note, "scr-turn-note"),
  );
}

// Each response block goes after the first assistant turn that carries its
// rkey (or, failing that, the last turn that does), so later marker and
// receipt turns never move it.
function layout(chat) {
  const anchor = new Map();
  chat.turns.forEach((turn, i) => {
    if (!turn.rkey || !chat.responses[turn.rkey]) return;
    const seen = anchor.get(turn.rkey);
    if (!seen || (!seen.assistant && turn.role === "assistant")) {
      anchor.set(turn.rkey, { index: i, assistant: turn.role === "assistant" });
    } else if (!seen.assistant) {
      seen.index = i;
    }
  });
  // The anchoring assistant turn is drawn inside its reply, so the reply
  // element carries the assistant's own words (opening line included).
  const items = [];
  chat.turns.forEach((turn, i) => {
    const at = turn.rkey ? anchor.get(turn.rkey) : null;
    const anchored = at && at.index === i;
    if (!(anchored && at.assistant)) items.push({ key: `t:${turn.id}`, kind: "turn", turn });
    if (anchored) {
      items.push({
        key: `r:${turn.rkey}`,
        kind: "response",
        rvm: chat.responses[turn.rkey],
        lead: at.assistant ? turn : null,
      });
    }
  });
  return items;
}

function taskPanel(ctx, state, vm, open) {
  if (!vm) return null;
  const controls = [
    button(ctx, { label: vm.controls.stop, kind: "danger", testid: "task-stop", onClick: () => ctx.actions.chatStop() }),
    vm.controls.compensate && state.capabilities.compensate
      ? button(ctx, {
          label: vm.controls.compensate,
          kind: "secondary",
          testid: "task-compensate",
          onClick: () => ctx.actions.compensate(vm.controls.compensate_version),
        })
      : null,
    button(ctx, { label: vm.controls.handoff, kind: "secondary", testid: "handoff", onClick: () => ctx.actions.handoff() }),
  ];
  const controlRow = el(ctx, "div", { class: "scr-choice-row scr-task-controls" }, controls);
  const compensationHint = vm.controls.compensate && state.capabilities.compensate
    ? para(ctx, vm.controls.compensate_hint, "scr-hint")
    : null;
  const inDetails = state.view === "customer" && vm.controls_in_details === true;
  return el(
    ctx,
    "section",
    { class: "scr-task-panel", "data-testid": "task-panel", "aria-label": ctx.t("web.ui.task.title") },
    ctx.components.details({
      summary: `${ctx.t("web.ui.task.title")} · ${vm.route_label}`,
      open: Boolean(open),
      children: [
        vm.tier ? ctx.components.tierBadge(vm.tier) : null,
        para(ctx, vm.copy),
        para(ctx, vm.requested_hint, "scr-hint"),
        ctx.components.dag(vm.dag),
        para(ctx, vm.dag.blocked_text, "scr-hint"),
        inDetails ? controlRow : null,
        inDetails ? compensationHint : null,
      ].filter(Boolean),
    }),
    inDetails ? null : controlRow,
    inDetails ? null : compensationHint,
  );
}

function gatedElsewhere(ctx, items) {
  if (!items || !items.length) return null;
  return el(
    ctx,
    "div",
    { class: "scr-gated-elsewhere", "data-testid": "gated-elsewhere", role: "status" },
    items.map((item) =>
      button(ctx, {
        label: item.label,
        kind: "secondary",
        testid: `gated-back-${item.rkey}`,
        onClick: () => ctx.actions.makeCurrent(item.rkey),
      }),
    ),
  );
}

function buildComposer(ctx, refs) {
  const input = el(ctx, "textarea", { class: "scr-composer-input", "data-testid": "composer-input" });
  input.rows = 1;
  const attach = el(ctx, "input", { type: "checkbox", "data-testid": "composer-attach" });
  const attachLabel = el(ctx, "span", {});
  const attachNote = el(ctx, "p", { class: "scr-hint scr-attach-note" });
  const contacts = el(ctx, "p", { class: "scr-hint scr-composer-contacts", "data-testid": "composer-contacts" });
  const send = sendButton(ctx, refs);
  const remember = () => {
    const ui = ctx.store.ui;
    if (ui && ui.draft) {
      ui.draft.text = input.value;
      ui.draft.attach = attach.checked;
    }
  };
  input.addEventListener("input", () => {
    remember();
    grow(input);
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
      ev.preventDefault();
      submit(ctx, refs);
    }
  });
  attach.addEventListener("change", () => {
    remember();
    attachNote.hidden = !attach.checked;
  });
  attachNote.hidden = true;
  const node = el(
    ctx,
    "div",
    { class: "scr-composer" },
    el(ctx, "label", { class: "scr-check scr-attach" }, attach, attachLabel),
    attachNote,
    contacts,
    el(ctx, "div", { class: "scr-composer-row" }, input, send),
  );
  Object.assign(refs, { composer: node, input, attach, attachLabel, attachNote, contacts, send });
  const draft = ctx.store.ui && ctx.store.ui.draft;
  if (draft) {
    input.value = draft.text || "";
    attach.checked = Boolean(draft.attach);
    attachNote.hidden = !attach.checked;
  }
  return node;
}

function sendButton(ctx, refs) {
  const label = ctx.t("web.ui.composer.send");
  const node = ctx.components.button({ label, kind: "primary", testid: "composer-send", onClick: () => submit(ctx, refs) });
  node.dataset.lock = "1";
  node.dataset.off = "0";
  refs.sendLabel = label;
  return node;
}

function grow(input) {
  input.rows = Math.min(COMPOSER_MAX_ROWS, Math.max(1, input.value.split("\n").length));
}

async function submit(ctx, refs) {
  const text = refs.input.value.trim();
  const state = ctx.store.state;
  if (!text || !state || state.chat.composer.disabled || refs.sending) return;
  refs.sending = true;
  refs.pinned = null;
  refs.pending = text;
  refs.render();
  let ok = false;
  try {
    ok = await ctx.actions.send(text, refs.attach.checked ? 1 : 0);
  } catch {
    // The shell already reported the error; keep the draft.
  } finally {
    refs.sending = false;
    refs.pending = null;
  }
  if (ok) {
    refs.input.value = "";
    refs.attach.checked = false;
    refs.attachNote.hidden = true;
    grow(refs.input);
  }
  refs.render();
}

async function runShortcut(ctx, refs, key) {
  refs.sending = true;
  refs.pinned = null;
  refs.render();
  try {
    await ctx.actions.shortcut(key);
  } catch {
    // reported by the shell
  } finally {
    refs.sending = false;
    refs.render();
  }
}

export function mount(root, ctx) {
  const refs = { blocks: new Map(), seen: new Set(), phases: new Map(), operatorOpen: false, sending: false };
  const greeting = el(ctx, "div", { class: "scr-greeting" });
  const log = el(ctx, "div", {
    class: "scr-chat-log",
    "data-testid": "chat-log",
    role: "log",
    "aria-live": "polite",
  });
  const thinking = el(ctx, "div", { class: "scr-thinking", role: "status" });
  const operator = el(ctx, "div", { class: "scr-operator-slot" });
  const tail = el(ctx, "div", { class: "scr-chat-tail" });
  const scroller = el(ctx, "div", { class: "scr-chat-scroll" }, log, thinking, operator, tail);
  const chips = el(ctx, "div", { class: "scr-chips" });
  const wrap = el(ctx, "div", { class: "scr-chat" }, scroller, chips);
  refs.wrap = wrap;
  refs.scroller = scroller;
  // The reader taking over the scroll releases the pinned reply.
  for (const name of ["wheel", "touchstart", "keydown"]) {
    scroller.addEventListener(name, () => {
      refs.pinned = null;
    }, { passive: true });
  }
  refs.greeting = greeting;
  refs.log = log;
  refs.thinking = thinking;
  refs.operator = operator;
  refs.tail = tail;
  refs.chips = chips;
  refs.render = () => {
    if (ctx.store.state) update(root, ctx.store.state, ctx);
  };
  wrap.append(buildComposer(ctx, refs));
  root.append(wrap);
  root.__chat = refs;
  // The operator snapshot also lives on the plans tab; close it here when
  // the assistant tab is left so only one copy is in the document.
  ctx.store.subscribe(() => {
    if (refs.operatorOpen && ctx.store.ui && ctx.store.ui.tab !== "assistant") {
      refs.operatorOpen = false;
      refs.operatorSig = null;
      refs.operator.replaceChildren();
    }
  });
}

function renderOperator(ctx, state, refs) {
  const payload = refs.operatorOpen ? ensureTab(ctx, state, "operator") : null;
  const sig = signature(ctx, state, [refs.operatorOpen, payload, state.capabilities]);
  if (sig === refs.operatorSig) return;
  refs.operatorSig = sig;
  refs.operator.replaceChildren();
  if (!refs.operatorOpen) return;
  if (!payload) {
    refs.operator.append(el(ctx, "p", { class: "scr-muted" }, ctx.t("web.ui.loading")));
    return;
  }
  refs.operator.append(operatorBlock(ctx, state, payload.vm));
}

export function update(root, state, ctx) {
  const refs = root.__chat;
  if (!refs) return;
  const chat = state.chat;
  const loggedIn = state.session.logged_in && !state.login;
  refs.wrap.hidden = !loggedIn;
  if (!loggedIn || refs.langGeneration !== state.generation) {
    refs.langs = new Map();
    refs.switches = [];
    refs.switchNodes = new Map();
    refs.localeSeen = null;
    refs.langGeneration = state.generation;
  }
  if (!loggedIn) {
    refs.blocks.clear();
    refs.seen.clear();
    refs.phases.clear();
    refs.greetSig = null;
    refs.log.replaceChildren();
    return;
  }

  const empty = chat.turns.length === 0;
  const pick = (key) => runShortcut(ctx, refs, key);
  const greetSig = signature(ctx, state, [empty, chat.greeting, chat.shortcuts, chat.drill_caption, state.capabilities.tabs]);
  if (refs.greetSig !== greetSig) {
    refs.greetSig = greetSig;
    refs.greeting.replaceChildren();
    if (empty) {
      if (chat.greeting) {
        refs.greeting.append(
          el(ctx, "div", { class: "scr-turn scr-turn-assistant" }, el(ctx, "div", { class: "scr-bubble" }, chat.greeting)),
        );
      }
      refs.greeting.append(shortcutButtons(ctx, chat, false, pick));
    }
    refs.chips.replaceChildren();
    if (!empty && chat.shortcuts.length) {
      refs.chips.append(shortcutButtons(ctx, chat, true, pick));
    }
  }

  const onOperator = () => {
    refs.operatorOpen = true;
    refs.render();
    reveal(refs.scroller, refs.operator);
  };
  const items = layout(chat);
  // Written text is not translated again (P-DIFF-19): a switch of language is marked in the
  // log, and each block remembers the language it first appeared in.
  if (refs.localeSeen && refs.localeSeen !== state.locale && items.length) {
    refs.switches.push(items[items.length - 1].key);
  }
  refs.localeSeen = state.locale;
  const next = new Map();
  // The greeting lives inside the log so the log is never an empty box.
  const nodes = empty ? [refs.greeting] : [];
  let fresh = null;
  let moved = null;
  const lastBefore = refs.lastKey;
  for (const item of items) {
    if (!refs.langs.has(item.key)) refs.langs.set(item.key, state.locale);
    const born = refs.langs.get(item.key);
    const value =
      item.kind === "turn"
        ? item.turn
        : [item.lead, item.rvm, state.device.tasks.map((t) => [t.key, t.nodes_version, Boolean(t.gesture)])];
    const sig = signature(ctx, state, [value, state.capabilities, born]);
    let block = refs.blocks.get(item.key);
    if (!block || block.sig !== sig) {
      const node =
        item.kind === "turn"
          ? turnNode(ctx, item.turn)
          : renderResponse(ctx, state, item.rvm, { onOperator, lead: item.lead ? turnNode(ctx, item.lead) : null });
      if (born !== state.locale) bornTag(ctx, node, born);
      if (block) keepFocus(block.node, node);
      block = { sig, node };
    }
    next.set(item.key, block);
    nodes.push(block.node);
    refs.switches.forEach((key, index) => {
      if (key === item.key) nodes.push(switchNode(ctx, state, refs, index));
    });
    if (item.kind === "response") {
      const phase = phaseOf(item.rvm);
      if (!refs.seen.has(item.key)) {
        refs.seen.add(item.key);
        fresh = item;
      } else if (item.rvm.current && refs.phases.get(item.key) !== phase) {
        moved = item;
      }
      refs.phases.set(item.key, phase);
    }
  }
  refs.lastKey = items.length ? items[items.length - 1].key : null;
  const same = nodes.length === refs.log.children.length && nodes.every((n, i) => refs.log.children[i] === n);
  if (!same) refs.log.replaceChildren(...nodes);
  refs.blocks = next;

  const tailSig = signature(ctx, state, [chat.gated_elsewhere, chat.task_panel, state.capabilities]);
  if (tailSig !== refs.tailSig) {
    const open = refs.tail.querySelector("details[open]") !== null;
    refs.tailSig = tailSig;
    refs.tail.replaceChildren(
      ...[gatedElsewhere(ctx, chat.gated_elsewhere), taskPanel(ctx, state, chat.task_panel, open)].filter(Boolean),
    );
  }

  renderOperator(ctx, state, refs);

  const inFlight = Boolean(ctx.store.ui && ctx.store.ui.inFlight);
  refs.thinking.replaceChildren();
  if (refs.sending) {
    const live = state.shell.badge && state.shell.badge.kind === "model";
    // The text being sent is echoed faintly until the server's own turn
    // replaces it; nothing about it is decided here.
    if (refs.pending) {
      refs.thinking.append(
        el(ctx, "div", { class: "scr-turn scr-turn-user scr-pending" }, el(ctx, "div", { class: "scr-bubble" }, refs.pending)),
      );
    }
    refs.thinking.append(
      el(
        ctx,
        "div",
        { class: "scr-turn scr-turn-assistant", "data-testid": "chat-thinking" },
        el(ctx, "div", { class: "scr-bubble scr-typing" }, ctx.t(live ? "web.ui.chat.thinking_live" : "web.ui.chat.thinking")),
      ),
    );
  }

  const composer = chat.composer;
  if (refs.generation !== state.generation) {
    // A reset clears the draft in the store; follow it once per generation.
    refs.generation = state.generation;
    const draft = (ctx.store.ui && ctx.store.ui.draft) || { text: "", attach: false };
    refs.input.value = draft.text || "";
    refs.attach.checked = Boolean(draft.attach);
    refs.attachNote.hidden = !refs.attach.checked;
    grow(refs.input);
  }
  refs.input.placeholder = composer.placeholder;
  refs.input.setAttribute("aria-label", composer.placeholder);
  refs.input.maxLength = composer.max_chars;
  refs.input.disabled = Boolean(composer.disabled);
  if (refs.sendLabel !== ctx.t("web.ui.composer.send")) {
    const fresh = sendButton(ctx, refs);
    refs.send.replaceWith(fresh);
    refs.send = fresh;
  }
  refs.attachLabel.textContent = composer.attach_label;
  refs.attachNote.textContent = composer.attach_note;
  refs.contacts.textContent = composer.contacts_note;
  refs.send.dataset.off = composer.disabled ? "1" : "0";
  applyBusy(refs.wrap, inFlight || refs.sending);

  if (fresh) {
    const node = refs.blocks.get(fresh.key).node;
    reveal(refs.scroller, freshTarget(refs.scroller, node));
    // The new reply stays where it was put until the user scrolls or sends again; later
    // renders of the same exchange (busy flags, device updates) do not pull it away.
    refs.pinned = fresh.key;
    const line = fresh.rvm.copy_lines && fresh.rvm.copy_lines[0];
    if (line && ctx.a11y) ctx.a11y.announce(line);
  } else if (moved) {
    const node = refs.blocks.get(moved.key).node;
    reveal(refs.scroller, focusPoint(node) || node);
  } else if (refs.pinned) {
    // Hold the pinned reply in place.
  } else if (refs.lastKey && refs.lastKey !== lastBefore && lastBefore) {
    reveal(refs.scroller, refs.log.lastElementChild);
  } else if (refs.sending) {
    reveal(refs.scroller, refs.thinking);
  }
}

// A block first drawn in the other language keeps its written text; a small tag says so.
function bornTag(ctx, node, born) {
  node.classList.add("scr-born-other");
  node.prepend(
    el(ctx, "span", { class: "scr-born-tag", "data-testid": "born-tag", "data-born": born },
      ctx.t(born === "en" ? "web.ui.chat.born_en" : "web.ui.chat.born_zh")),
  );
}

function switchNode(ctx, state, refs, index) {
  if (!refs.switchNodes) refs.switchNodes = new Map();
  const sig = signature(ctx, state, ["switch", index]);
  const held = refs.switchNodes.get(index);
  if (held && held.sig === sig) return held.node;
  const node = el(ctx, "div", { class: "scr-turn scr-turn-system scr-locale-switch", "data-testid": "locale-switch" },
    el(ctx, "div", { class: "scr-bubble" }, ctx.t("web.ui.chat.locale_switched")));
  refs.switchNodes.set(index, { sig, node });
  return node;
}

// The user's words and the head of the new card (amount, tier) both in view; when they
// cannot both fit, the card's head wins.
function freshTarget(scroller, node) {
  const user = userTurnBefore(node);
  const card = node.querySelector('[data-testid="card"]');
  if (!user) return node;
  if (card) {
    const span = card.getBoundingClientRect().top + 120 - user.getBoundingClientRect().top;
    if (span > scroller.clientHeight) return card;
  }
  return user;
}

// What a reply is waiting for; a change moves the reader's eye to it.
function phaseOf(rvm) {
  return [
    Boolean(rvm.result),
    Boolean(rvm.failure),
    rvm.gate ? rvm.gate.decision : "",
    rvm.cards.length,
  ].join("|");
}

const FOCUS_ORDER = ["failure-block", "result-banner", "gate-pause", "gate", "card"];

function focusPoint(node) {
  for (const id of FOCUS_ORDER) {
    const target = node.querySelector(`[data-testid="${id}"]`);
    if (target) return target;
  }
  return null;
}

function userTurnBefore(node) {
  let prev = node.previousElementSibling;
  while (prev && prev.dataset.testid !== "turn-user") {
    if (prev.dataset.rkey !== node.dataset.rkey) return null;
    prev = prev.previousElementSibling;
  }
  return prev;
}

// Scroll the conversation (not the page) so that `node` starts in view.
function reveal(scroller, node) {
  if (!node) return;
  const top = node.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  scroller.scrollTo({ top: Math.max(0, top - 8), behavior: still ? "auto" : "smooth" });
}

function keepFocus(oldNode, newNode) {
  const active = document.activeElement;
  if (!active || !oldNode.contains(active)) return;
  const id = active.dataset && active.dataset.testid;
  if (!id) return;
  queueMicrotask(() => {
    const target = newNode.querySelector(`[data-testid="${id}"]`);
    if (target && !target.disabled) target.focus();
  });
}
