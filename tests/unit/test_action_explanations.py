"""Explanation content must describe actual evidence without inventing rule hits."""

from corridor.ui.explanations import explanation


def test_explanation_preserves_actual_rules_and_separates_sources():
    value = explanation(
        card={"tier": "red", "dependency_hash": "dependency"},
        rule_ids=["BRK-LOCK"],
        checkpoint={"seq": 4, "head": "audit"},
    )
    assert value["tier"] == "red"
    assert list(value["rules"]) == ["BRK-LOCK"]
    assert value["rules"]["BRK-LOCK"]
    assert value["audit_checkpoint"]["head"] == "audit"
    assert value["fact_sources"]["dependency_hash"] == "dependency"
    assert explanation(card={"tier": "green"})["rules"] == {}
