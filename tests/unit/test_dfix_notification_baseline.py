"""Notification baseline outcomes follow planner output and signed bank effects."""

from corridor.agent.llm import LLMClient, LLMResult
from corridor.ui.attack_views import observed_outcome


def test_baseline_does_not_force_attack_success(tmp_path):
    from corridor.bench.notification_baseline import run_notification_baseline

    attack = run_notification_baseline(tmp_path, benign=False)
    control = run_notification_baseline(tmp_path, benign=True)
    assert attack["attack_success"] is False
    assert attack["completed"] and control["completed"]
    assert attack["prohibited_effects"] == control["prohibited_effects"] == 0
    assert observed_outcome(attack) == "completed"
    assert attack["receipts"] and attack["receipt_signatures_valid"]
    assert attack["external_text_hash"] != control["external_text_hash"]


def test_baseline_measures_changed_recipient_if_planner_actually_selects_it(tmp_path, monkeypatch):
    from corridor.bench.notification_baseline import run_notification_baseline

    complete = LLMClient.complete

    def change_recipient(self, **kwargs):
        result = complete(self, **kwargs)
        output = dict(result.output)
        output["contact"] = {"handle_type": "state", "ref": "CONTACT_LIN_A"}
        return LLMResult(output, result.source, result.replay_key)

    monkeypatch.setattr(LLMClient, "complete", change_recipient)
    result = run_notification_baseline(tmp_path, benign=False)
    assert result["attack_success"] is True
    assert result["prohibited_effects"] > 0
    assert not result["completed"]
    assert observed_outcome(result) == "violated"
