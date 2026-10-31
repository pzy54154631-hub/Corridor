"""State-level equivalence of the web front end and the Streamlit page (design §3.5.1).

Every listed flow runs once per driver, each run in its own process
(``python -m tests.web.flow_runner``): Streamlit under AppTest against the web over HTTP,
or for a tab flow the reference script (``reference_flows.py``) against the web. At
every listed stage the 21 ``STATE_FIELDS`` of both runs must be equal, both runs must
meet the hand-written expectations of ``expectations.py``, and the simulated device must
never complete an SMS check itself. Both sides' JSON and the difference summary stay in
``.tmp/web-equivalence/<flow>/``.

The conclusion is limited to "in the N listed flows, at every listed stage, the 21 listed
state fields are equal"; it says nothing about entry points only the web has, which the
negative tests cover. The summary and the per-flow timings are printed after pytest's
own summary, ending with ``TOTAL state-level differences: <n>``.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from corridor.web import FEATURES
from tests.web.drivers import CallProbe
from tests.web.expectations import EXPECTED, check_flow, check_stage
from tests.web.flows import FLOWS, PROBES, selected
from tests.web.state_probe import STATE_FIELDS, compare_stages, normalized, observe_state

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".tmp" / "web-equivalence"
TIMEOUT = 600
REPORT = {"flows": {}, "printed": False}
# CONTRACT-CHANGES.md Q-1, ruled option A (P-DIFF-19): Streamlit writes the reply message
# and the receipt narratives of a button callback in the environment's language, the web in
# the session's. From the switch on, this flow compares everything but those rendered
# strings; "locale_round_trip" still compares them byte for byte.
LOCALIZED_AFTER_SWITCH = frozenset({"locale_switch_gate"})
# CONTRACT-CHANGES.md Q-2, ruled option B (P-DIFF-3): Streamlit's refused Confirm on a
# ticking that is not a prefix still counts one DomesticSession tool call, the web gives no
# token and makes none. From the named stage on, Streamlit's ``tool_calls`` must be exactly
# one more than the web's; every other field is compared as it is.
REFUSED_CONFIRM_FROM = {"birthday_prefix": "invalid"}


def _environment():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LLM_", "CORRIDOR_LIVE", "LIVE_"))
    }
    env.update({"CORRIDOR_LOCALE": "zh-CN", "PYTHONIOENCODING": "utf-8"})
    return env


def sides(flow):
    """Streamlit (for a tab flow, the reference script) against the web."""
    return (flow.left, "web")


def run_both(flow):
    """Start both drivers' processes for one flow and read their stage lists."""
    folder = OUT / flow.name
    folder.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    processes = {}
    for driver in sides(flow):
        out = folder / f"{driver}.json"
        out.unlink(missing_ok=True)
        processes[driver] = subprocess.Popen(
            [sys.executable, "-m", "tests.web.flow_runner", "--driver", driver]
            + ["--flow", flow.name, "--out", str(out)],
            cwd=ROOT,
            env=_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    runs = {}
    for driver, process in processes.items():
        try:
            output, _ = process.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        out = folder / f"{driver}.json"
        if out.exists():
            runs[driver] = json.loads(out.read_text(encoding="utf-8"))
        else:
            tail = output.decode("utf-8", "replace")[-2000:]
            runs[driver] = {"seconds": None, "stages": [{"stage": "error", "error": tail}]}
    runs["wall"] = round(time.perf_counter() - started, 2)
    return runs


@pytest.fixture(scope="module", autouse=True)
def summary(request):
    request.config.add_cleanup(_print_summary)


def _print_summary():
    if REPORT["printed"] or not REPORT["flows"]:
        return
    REPORT["printed"] = True
    total = calls = 0
    lines = ["", "web equivalence: Streamlit or reference script vs web, state fields only"]
    timings, pending = [], []
    for name, item in REPORT["flows"].items():
        lines.extend(item["lines"])
        for driver, failures in item["expectations"].items():
            lines.extend(f"   expectation ({driver}): {failure}" for failure in failures)
        timings.append(
            f"   {name}: "
            + ", ".join(f"{driver} {seconds} s" for driver, seconds in item["seconds"].items())
        )
        if not item["gating"]:
            met = not any(item["expectations"].values())
            pending.append(
                f"   {name}: {item['differences']} state differences, "
                f"{item['calls']} call-count differences, expectations met: {met}"
            )
            continue
        total += item["differences"]
        calls += item["calls"]
    gating = sum(1 for item in REPORT["flows"].values() if item["gating"])
    lines.append(
        f"flows: {gating} (core, and ext flows whose features are open or need none); "
        f"fields per stage: {len(STATE_FIELDS)}"
    )
    if pending:
        lines.append("not yet gating (ext not in FEATURES, or batch B; not part of the total):")
        lines.extend(pending)
    lines.append("per-flow time:")
    lines.extend(timings)
    lines.append(f"call-count differences (not part of the state total): {calls}")
    lines.append(f"TOTAL state-level differences: {total}")
    print("\n".join(lines), flush=True)


def _localized_from(flow):
    """The first stage index after the language switch (stage 0 is the login)."""
    if flow.name not in LOCALIZED_AFTER_SWITCH:
        return None
    return [step.op for step in flow.steps].index("locale") + 1


def _refused_confirm_counted(flow, left, right):
    """Streamlit's stages with its one refused Confirm taken out of ``tool_calls``, only
    where its count is exactly one more than the web's; any other count stays a difference."""
    names = [stage["stage"] for stage in left]
    start = REFUSED_CONFIRM_FROM.get(flow.name)
    if start not in names:
        return left
    adjusted = []
    for index, stage in enumerate(left):
        other = right[index] if index < len(right) else {}
        mine, theirs = stage.get("tool_calls"), other.get("tool_calls")
        counted = isinstance(mine, int) and isinstance(theirs, int) and mine == theirs + 1
        if index >= names.index(start) and counted:
            stage = {**stage, "tool_calls": theirs}
        adjusted.append(stage)
    return adjusted


def gating(flow):
    """Core flows, and ext flows whose features this build opens (or that need none)."""
    return flow.tier == "core" or (flow.tier == "ext" and flow.features <= FEATURES)


def _cases():
    """An ext flow runs with its features enabled in its own test app; until those
    features join ``FEATURES`` (and a batch-B flow until that batch lands) it is reported
    but expected to fail, which never counts as a pass."""
    cases = []
    for flow in selected(tiers=("core", "ext", "after_b")):
        reason = "ext item not in FEATURES" if flow.tier == "ext" else "waits for batch B"
        marks = () if gating(flow) else pytest.mark.xfail(reason=reason)
        cases.append(pytest.param(flow, id=flow.name, marks=marks))
    return cases


@pytest.mark.parametrize("flow", _cases())
def test_flow_is_equivalent_at_every_stage(flow):
    runs = run_both(flow)
    drivers = sides(flow)
    differences, calls, lines = compare_stages(
        flow.name,
        _refused_confirm_counted(flow, runs[flow.left]["stages"], runs["web"]["stages"]),
        runs["web"]["stages"],
        localized_from=_localized_from(flow),
        left_name=flow.left,
    )
    if flow.name in REFUSED_CONFIRM_FROM:
        lines.append(
            f"   from stage {REFUSED_CONFIRM_FROM[flow.name]}: tool_calls compared as "
            f"{flow.left} = web + 1 (the refused Confirm, P-DIFF-3)"
        )
    expectations = {driver: check_flow(flow.name, runs[driver]["stages"]) for driver in drivers}
    REPORT["flows"][flow.name] = {
        "differences": differences,
        "calls": calls,
        "lines": lines,
        "expectations": expectations,
        "seconds": {driver: runs[driver]["seconds"] for driver in drivers} | {"wall": runs["wall"]},
        "gating": gating(flow),
    }
    (OUT / flow.name / "diff.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert differences == 0, "\n".join(lines)
    assert calls == 0, "\n".join(lines)
    assert expectations == {driver: [] for driver in drivers}, expectations


# --- the harness itself: independent expectations and spy negatives -----------------------


def test_every_flow_has_expectations_for_its_named_stages():
    assert set(EXPECTED) == set(FLOWS)
    for name, flow in FLOWS.items():
        stages = {"login"} | {step.stage for step in flow.steps}
        assert set(EXPECTED[name]) <= stages, name
    assert set(PROBES) <= set(FLOWS)


def _controller_run(text="给妈妈转5000元"):
    from corridor.agent.domestic_session import DomesticSession
    from corridor.channels.im import IMAdapter
    from corridor.ui.controller import DemoController, demo_state
    from corridor.ui.wealth_controller import WealthController

    controller = DemoController(initial_state=demo_state(showcase=True))
    wealth = WealthController()
    controller.start_session()
    wealth.start_session()
    session = DomesticSession(controller, wealth)
    probe = CallProbe(controller)
    rid = session.receive(text)["response_id"]
    IMAdapter(session).click("confirm", rid)
    return controller, session, probe, rid


def _stage(name, controller, session, rid, probe):
    observed = normalized(observe_state(controller, session, rid, None))
    return {"stage": name, **observed, "calls": probe.snapshot()}


def test_the_probe_flags_an_sms_check_completed_by_the_simulated_device():
    controller, session, probe, rid = _controller_run()
    pending = session.response(rid)["payload"]["pending_id"]
    controller.request_mfa(pending)
    assert controller.complete_mfa(pending)["status"] == "mfa_verified"
    calls = probe.snapshot()
    assert calls["device.complete_mfa"] == 1
    assert calls["controller.complete_mfa(auto)"] == 1
    failures = check_flow(
        "level0_large_known", [_stage("verified", controller, session, rid, probe)]
    )
    assert any("the simulated device completed MFA" in line for line in failures), failures


def test_the_probe_does_not_flag_the_verify_step_after_a_typed_code():
    from corridor.channels.im import IMAdapter

    controller, session, probe, rid = _controller_run()
    pending = session.response(rid)["payload"]["pending_id"]
    controller.request_mfa(pending)
    code = controller.gateway.mfa.test_code(controller._pending[pending]["challenge_id"])
    assert controller.complete_mfa(pending, code=code)["status"] == "mfa_verified"
    IMAdapter(session).click("verify_mfa", rid)
    calls = probe.snapshot()
    assert calls.get("device.complete_mfa", 0) == 0
    assert calls.get("controller.complete_mfa(auto)", 0) == 0
    assert calls["controller.complete_mfa(no_code)"] == 1
    assert session.response(rid)["status"] == "mfa_verified"
    stage = _stage("verified", controller, session, rid, probe)
    assert check_stage(EXPECTED["level0_large_known"]["verified"], stage) == []


def test_the_comparison_counts_each_changed_field_and_a_missing_stage():
    controller, session, probe, rid = _controller_run("给妈妈转500元")
    stage = _stage("confirmed", controller, session, rid, probe)
    same, calls, _ = compare_stages("self", [stage], [json.loads(json.dumps(stage))])
    assert (same, calls) == (0, 0)
    changed = {**stage, "execute_count": stage["execute_count"] + 1, "clock": "changed"}
    differences, _, lines = compare_stages("self", [stage], [changed])
    assert differences == 2 and any("STATE DIFF in execute_count" in line for line in lines)
    missing, _, _ = compare_stages("self", [stage, stage], [stage])
    assert missing == 1
    error = {"stage": "error", "error": "DriverError: no confirm gesture"}
    both_failed, _, lines = compare_stages("self", [error], [error])
    assert both_failed == 1 and "RUN ERROR" in lines[-1]
    extra_call = {**stage, "calls": {**stage["calls"], "gateway.dispatch": 9}}
    _, call_differences, _ = compare_stages("self", [stage], [extra_call])
    assert call_differences == 1


def test_an_expectation_fails_when_a_paused_check_let_the_payment_through():
    controller, session, probe, rid = _controller_run("给妈妈转500元")
    settled = _stage("paused", controller, session, rid, probe)
    failures = check_stage(EXPECTED["level2_pause_retry"]["paused"], settled)
    assert any(line.startswith("executed") for line in failures), failures
    assert any(line.startswith("events_exact") for line in failures), failures


def test_only_rendered_narrative_strings_can_be_left_out_of_the_comparison():
    controller, session, probe, rid = _controller_run("给妈妈转500元")
    stage = _stage("confirmed", controller, session, rid, probe)
    english = json.loads(json.dumps(stage))
    english["response"]["message"] = "Status comes from the gateway."
    (english_result,) = english["results"].values()
    for item in english_result["facts"]["facts"]:
        item["text"], item["display_value"] = "Transfer settled.", "Transfer settled"
    assert compare_stages("self", [stage], [english])[0] == 2
    assert compare_stages("self", [stage], [english], localized_from=0)[0] == 0
    (fact, *_) = english_result["facts"]["facts"]
    fact[next(key for key in fact if key not in ("text", "display_value"))] = "changed"
    assert compare_stages("self", [stage], [english], localized_from=0)[0] == 1


def test_only_the_one_refused_confirm_is_taken_out_of_the_tool_calls():
    def stages(*counts):
        names = ("login", "plan", "invalid", "subset")
        return [{"stage": name, "tool_calls": count} for name, count in zip(names, counts)]

    def counts(flow, left, right):
        return [stage["tool_calls"] for stage in _refused_confirm_counted(flow, left, right)]

    prefix, subset = FLOWS["birthday_prefix"], FLOWS["birthday_subset"]
    streamlit = stages(0, 1, 2, 3)
    assert counts(prefix, streamlit, stages(0, 1, 1, 2)) == [0, 1, 1, 2]
    assert counts(prefix, streamlit, stages(0, 1, 0, 1)) == [0, 1, 2, 3]
    assert counts(prefix, streamlit, stages(0, 0, 1, 2)) == [0, 1, 1, 2]
    assert compare_stages("self", stages(0, 1, 2, 3), stages(0, 0, 2, 3))[0] == 1
    assert counts(subset, streamlit, stages(0, 1, 1, 2)) == [0, 1, 2, 3]
