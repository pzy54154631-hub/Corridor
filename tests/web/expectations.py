"""Hand-written expectations for every equivalence flow (design §3.5.1, X16).

They come from the S6 truth tables and the existing unit tests (for example
``test_three_runs_off_proceed_and_pause``: a pause leaves only ``login`` and
``user_utterance`` in the audit chain, no budget, no challenge, no dispatch), never from
either driver's output, so two drivers that make the same mistake still fail here.

Keys of one stage expectation:
  status, tier       the observed reply's envelope status and tier
  executed           bank execute count
  challenges         number of ``mfa_challenge`` audit events
  pending            number of pending actions; ``verified`` their verified flags
  results            number of stored results
  budget_used        approvals consumed from the budget
  safety             gateway safety state
  card               whether the reply carries a card
  local_status       the local selection projection's status (None: no projection)
  events             audit event kinds that must be present
  absent             audit event kinds that must be absent
  events_exact       the whole audit event sequence
  calls              the S6 spy counts so far (``{}``: no MFA, approval, signature,
                     dispatch or workflow confirmation call has run at all)
"""

PAUSED_EVENTS = ("login", "user_utterance")
SETTLED_EVENTS = ("approval_requested", "approval_consumed", "dispatch_intent")


def untouched(**extra):
    """A reply with a card and nothing else: the state of a paused or unanswered check,
    with every spied side-effect call still at zero (the S6 three-run pause path)."""
    return {
        "executed": 0,
        "challenges": 0,
        "pending": 0,
        "results": 0,
        "budget_used": 0,
        "events_exact": PAUSED_EVENTS,
        "calls": {},
        **extra,
    }


def red(*, budget=True):
    """Stage expectations of the red path, after the recipient check (if any) passed.

    ``budget`` is False for an action previewed by the controller itself (a card
    operation), whose approval request is not tied to the first confirmation.
    """
    used = {"budget_used": 1} if budget else {}
    return {
        "confirmed": {
            "status": "waiting_mfa",
            "tier": "red",
            "executed": 0,
            "challenges": 0,
            "pending": 1,
            "verified": [False],
            "absent": ("mfa_challenge", "dispatch_intent"),
            **used,
        },
        "challenge": {
            "status": "waiting_mfa",
            "executed": 0,
            "challenges": 1,
            "verified": [False],
            "absent": ("mfa_verified", "dispatch_intent"),
        },
        "verified": {
            "status": "mfa_verified",
            "executed": 0,
            "challenges": 1,
            "verified": [True],
            "events": ("mfa_attempt", "mfa_verified"),
            "absent": ("dispatch_intent",),
        },
        "final": {
            "status": "settled",
            "tier": "red",
            "executed": 1,
            "challenges": 1,
            "results": 1,
            "safety": "Normal",
            "events": SETTLED_EVENTS,
            **used,
        },
    }


def yellow_settled(**extra):
    return {
        "status": "settled",
        "tier": "yellow",
        "executed": 1,
        "challenges": 0,
        "results": 1,
        "budget_used": 1,
        "safety": "Normal",
        "events": SETTLED_EVENTS,
        "absent": ("mfa_challenge",),
        **extra,
    }


def card(tier, **extra):
    return untouched(status="awaiting_input", tier=tier, card=True, **extra)


def no_card(**extra):
    return untouched(card=False, **extra)


def held():
    """The phrase gate holds the words before any planner call (test_ui_scam_r7), so not
    even the utterance reaches the audit chain."""
    return no_card(status="clarify", events_exact=("login",))


EXPECTED = {
    "level0_yellow_known": {
        "login": {"executed": 0, "events_exact": ("login",), "safety": "Normal"},
        "card": card("yellow"),
        "confirmed": yellow_settled(),
    },
    "level0_large_known": {"card": card("red"), **red()},
    "level0_urgency_only": {"card": card("yellow"), "confirmed": yellow_settled()},
    # A green query is executed automatically with its own bank receipt (tier_reason:
    # "green-tier automatic execution"); it moves no funds but is a second execution.
    "level0_then_query": {
        "confirmed": yellow_settled(),
        "query": {"status": "settled", "tier": "green", "executed": 2, "budget_used": 1},
    },
    "level1_new_no_cue": {"card": card("red"), "answered": card("red"), **red()},
    "level1_known_authority": {
        "card": card("yellow"),
        "answered": card("yellow"),
        "confirmed": yellow_settled(),
    },
    "level2_fraud_new": {"card": card("red"), "answered": card("red"), **red()},
    "level2_pause_retry": {
        "card": card("red"),
        "paused": card("red"),
        "retried": card("red"),
        "answered": card("red"),
        **red(),
    },
    "directed_preset": {
        "card": card("red"),
        "retried": card("red"),
        "answered": card("red"),
        **red(),
    },
    "opening_copy": {"card": card("red"), "answered": card("red"), **red()},
    "phrase_gate_held": {"held": held()},
    "drill_phrase_gate": {"held": held()},
    "clarify_lin_b": {
        "clarify": no_card(status="clarify", local_status=None),
        "selected": untouched(local_status="awaiting_input"),
        "answered": untouched(local_status="awaiting_input"),
        "confirmed": {
            "status": "clarify",
            "local_status": "settled",
            "executed": 1,
            "challenges": 0,
            "results": 1,
            "budget_used": 1,
            "events": SETTLED_EVENTS,
        },
    },
    "clarify_cancel": {
        "clarify": no_card(status="clarify", local_status=None),
        "selected": untouched(local_status="awaiting_input"),
        "cancelled": {
            "status": "withdrawn",
            "local_status": None,
            "executed": 0,
            "challenges": 0,
            "pending": 0,
            "results": 0,
            "budget_used": 0,
            "calls": {},
        },
    },
    "cancel_mid_card": {
        "card": card("yellow"),
        "cancelled": {
            "status": "withdrawn",
            "executed": 0,
            "pending": 0,
            "results": 0,
            "budget_used": 0,
            "calls": {},
        },
    },
    "handoff_chat": {
        "card": card("yellow"),
        "handoff": {"executed": 0, "challenges": 0, "results": 0, "budget_used": 0, "calls": {}},
    },
    "handoff_panel": {
        "card": card("yellow"),
        "handoff": {"executed": 0, "challenges": 0, "results": 0, "budget_used": 0, "calls": {}},
    },
    "chat_stop": {
        "card": card("yellow"),
        "stopped": {"executed": 0, "pending": 0, "results": 0, "budget_used": 0, "calls": {}},
    },
    # Stop all after the first confirmation of a red card: the reply is withdrawn and the
    # approval it already used is not dispatched; no SMS challenge was ever issued.
    "stop_all_mid_card": {
        "card": card("red"),
        "confirmed": red()["confirmed"],
        "stopped": {
            "status": "withdrawn",
            "executed": 0,
            "challenges": 0,
            "results": 0,
            "absent": ("mfa_challenge", "dispatch_intent"),
        },
    },
    "mfa_lockout": {
        "challenge": {"status": "waiting_mfa", "challenges": 1, "safety": "Normal"},
        "wrong_1": {"status": "waiting_mfa", "verified": [False], "safety": "Watch"},
        "wrong_2": {"status": "waiting_mfa", "verified": [False], "safety": "Watch"},
        "wrong_3": {
            "status": "waiting_mfa",
            "executed": 0,
            "challenges": 1,
            "results": 0,
            "safety": "Locked",
            "events": ("mfa_attempt", "plan_voided"),
            "absent": ("mfa_verified", "dispatch_intent"),
        },
    },
    "locale_switch_gate": {
        "answered": card("red"),
        "locale_en": card("red"),
        **red(),
    },
    "locale_round_trip": {
        "answered": card("red"),
        "locale_en": card("red"),
        "locale_zh": card("red"),
        **red(),
    },
    # INV-2.9: only the exact displayed set signs; a true prefix re-previews unsigned; any
    # other ticking is refused with no state change and no workflow call.
    "birthday_prefix": {
        "plan": {"status": "draft", "tier": "yellow", "executed": 0, "results": 0, "calls": {}},
        "invalid": {"status": "draft", "executed": 0, "results": 0, "calls": {}},
        "subset": {
            "status": "redisplay",
            "executed": 0,
            "results": 0,
            "budget_used": 0,
            "calls": {},
            "absent": ("approval_consumed", "dispatch_intent"),
        },
        "confirmed": {
            "status": "settled",
            "results": 1,
            "challenges": 0,
            "events": ("approval_consumed", "dispatch_intent"),
        },
    },
    "birthday_subset": {
        "plan": {"status": "draft", "tier": "yellow", "executed": 0, "results": 0, "calls": {}},
        "subset": {"status": "redisplay", "executed": 0, "results": 0, "calls": {}},
        "confirmed": {"status": "settled", "results": 1, "challenges": 0},
    },
    "birthday_panel_compensate": {
        "confirmed": {"status": "settled", "results": 1, "challenges": 0},
        "compensation": {"pending": 1, "results": 1, "challenges": 0},
        "compensated": {"results": 2, "challenges": 0},
    },
    "birthday_then_refund": {
        "plan": {"status": "draft", "tier": "yellow", "executed": 0, "calls": {}},
        "confirmed": {"status": "settled", "results": 1, "challenges": 0},
        "refund": {"card": True, "challenges": 0},
        "refunded": {"status": "settled", "challenges": 0, "results": 2},
    },
    # Each yellow approval takes one unit of the approval budget; green reads take none.
    "subscriptions_chat": {
        "query": {"status": "awaiting_approval", "tier": "yellow", "card": True, "calls": {}},
        "listed": {"status": "settled", "challenges": 0, "budget_used": 1},
        "choose": {
            "status": "clarify",
            "card": False,
            "challenges": 0,
            "budget_used": 1,
            "results": 1,
        },
        "cancel_card": {"status": "awaiting_approval", "tier": "yellow", "card": True},
        "cancelled": {"status": "settled", "challenges": 0, "budget_used": 2},
    },
    "bill_report": {
        "bill": {"status": "settled", "tier": "green", "budget_used": 0, "challenges": 0},
        "anomaly": {"status": "settled", "tier": "green", "budget_used": 0, "challenges": 0},
    },
    # A purchase card is red: SMS first, then one approval dispatches it (INV-2.6).
    "wealth_purchase": {
        "compared": {"pending": 0, "challenges": 0, "results": 0},
        "purchase_card": {"pending": 1, "challenges": 0, "absent": ("dispatch_intent",)},
        "challenge": {"pending": 1, "challenges": 1, "verified": [False]},
        "verified": {"challenges": 1, "verified": [True], "absent": ("dispatch_intent",)},
        "final": {"results": 1, "challenges": 1, "events": SETTLED_EVENTS[1:]},
    },
    # O-S12: after Stop all a new purchase card can still be confirmed.
    "stop_then_purchase": {
        "purchase_card": {"pending": 1, "challenges": 0},
        "stopped": {"pending": 0, "results": 0, "absent": ("dispatch_intent",)},
        "purchase_again": {"pending": 1, "challenges": 0, "absent": ("dispatch_intent",)},
        "final": {"results": 1, "challenges": 1, "events": SETTLED_EVENTS[1:]},
    },
    "r4_preset": {"preset": {"executed": 0, "pending": 0, "results": 0, "calls": {}}},
    "subscriptions_tab": {
        "query_card": {"pending": 1, "challenges": 0, "results": 0, "calls": {}},
        "listed": {"results": 1, "challenges": 0, "budget_used": 1},
        "cancel_card": {"results": 1, "challenges": 0},
        "cancelled": {"results": 2, "challenges": 0, "budget_used": 2},
    },
    # A scheduled authorization ends "controlled" (created), not settled; red, so one SMS.
    "scheduled_monthly": {
        "card": {"status": "awaiting_approval", "tier": "red", "card": True, "executed": 0},
        "confirmed": {"status": "waiting_mfa", "challenges": 0},
        "challenge": {"status": "waiting_mfa", "challenges": 1},
        "verified": {"status": "mfa_verified", "challenges": 1},
        "final": {"status": "controlled", "challenges": 1, "verified": [True]},
    },
    "card_lost": {
        "card": {"tier": "red", "card": True, "executed": 0, "challenges": 0, "pending": 1},
        **red(budget=False),
    },
}


def _events(observation):
    return [row.get("event") for row in observation.get("audit") or []]


def check_stage(expected, observation):
    """The failed expectation keys of one stage, as readable lines (empty when met)."""
    response = observation.get("response") or {}
    events = _events(observation)
    actual = {
        "status": response.get("status"),
        "tier": response.get("tier"),
        "executed": observation.get("execute_count"),
        "challenges": events.count("mfa_challenge"),
        "pending": len(observation.get("pending") or {}),
        "verified": [item.get("verified") for item in (observation.get("pending") or {}).values()],
        "results": len(observation.get("results") or {}),
        "budget_used": (observation.get("budget") or {}).get("used"),
        "safety": observation.get("safety"),
        "card": bool(response.get("card")),
        "local_status": observation.get("local_status"),
        "events_exact": tuple(events),
        "calls": observation.get("calls") or {},
    }
    failures = []
    for key, want in expected.items():
        if key == "events":
            missing = [kind for kind in want if kind not in events]
            if missing:
                failures.append(f"events missing {missing}")
        elif key == "absent":
            present = [kind for kind in want if kind in events]
            if present:
                failures.append(f"events present {present}")
        elif actual[key] != want:
            failures.append(f"{key}: expected {want!r}, got {actual[key]!r}")
    return failures


def check_flow(name, stages):
    """Every unmet expectation of one driver's run of a flow."""
    expected = EXPECTED[name]
    observed = {stage["stage"]: stage for stage in stages}
    failures = []
    if "error" in observed:
        failures.append(f"run stopped: {observed['error'].get('error')}")
    for stage, wanted in expected.items():
        if stage not in observed:
            failures.append(f"stage {stage} missing")
            continue
        failures.extend(f"stage {stage}: {line}" for line in check_stage(wanted, observed[stage]))
    for stage in stages:
        calls = stage.get("calls") or {}
        if calls.get("device.complete_mfa") or calls.get("controller.complete_mfa(auto)"):
            failures.append(f"stage {stage['stage']}: the simulated device completed MFA {calls}")
    return failures
