"""The public mechanism demonstration uses isolated, real kernel executions."""

from concurrent.futures import ThreadPoolExecutor

from corridor.web import rogue
from tests.web.conftest import serve, ws_of
from tests.web.test_public_mode import TIGHT, Clock, browser, config


def test_real_pairs_and_single_process_cache(monkeypatch):
    from corridor.agent.llm import LLMClient

    def forbidden(*args, **kwargs):
        raise AssertionError("The mechanism demonstration must never call a model")

    monkeypatch.setattr(LLMClient, "complete", forbidden)
    rogue._cached = None
    original = rogue.execute_case
    calls = []

    def counted(spec, **kwargs):
        calls.append((spec.attack, kwargs.get("benign", False)))
        return original(spec, **kwargs)

    monkeypatch.setattr(rogue, "execute_case", counted)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: rogue.report(), range(4)))
    assert len(calls) == 16
    assert results.count(results[0]) == 4
    result = results[0]
    assert len(result["rows"]) == 8
    assert len({r["family"] for r in result["rows"]}) == 8
    assert result["summary"] == {
        "attacks": 8,
        "blocked": 8,
        "violations": 0,
        "prohibited_effects": 0,
        "attack_dispatches": 0,
        "benign_completed": 8,
    }
    statuses = {row["attack"]: row["status"] for row in result["rows"]}
    assert statuses.pop("missing_mfa") == "Escalate"
    assert set(statuses.values()) <= {"Reject", "rejected"}
    for row in result["rows"]:
        assert row["expected_rule"] in row["rule_ids"]
        assert row["passed"] and row["benign_completed"]
        assert not row["oracle_violation"] and not row["oracle_indeterminate"]
        assert row["llm_calls"] == 0
    result["rows"].clear()
    assert len(rogue.report()["rows"]) == 8


def test_get_before_session_and_no_session_mutation(client, monkeypatch):
    assert client.get("/api/rogue").code == 200
    client.open()
    ws = ws_of(client.app, client)
    before = client.get("/api/state").body
    calls = ws.effects.count
    monkeypatch.setattr(rogue, "_cached", None)
    assert client.get("/api/rogue").code == 200
    assert client.get("/api/state").body == before
    assert ws.effects.count == calls
    assert ws.live is None


def test_public_rate_limit_before_any_session():
    clock = Clock()
    with serve({"public": config(rates=TIGHT), "clock": clock}) as app:
        client = browser(app)
        for _ in range(TIGHT["api"].burst):
            assert client.get("/api/rogue").code == 200
        response = client.get("/api/rogue")
        assert response.code == 503
        assert response.headers["Retry-After"] == "5"
