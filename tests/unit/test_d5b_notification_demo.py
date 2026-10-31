"""The presentation uses actual isolated executions and the complete unlock protocol."""

from fractions import Fraction

from corridor.crypto.jcs import H
from corridor.gateway.audit import AuditLog
from corridor.ui.notification_demo import (
    advance_notification_cooldown,
    create_notification_demo,
    recover_notification_transfer,
    unlock_notification_demo,
)


def test_hijacked_proposal_moves_baseline_money_but_gateway_rejects(tmp_path):
    demo = create_notification_demo(tmp_path)
    result = demo["result"]
    baseline, corridor = result["baseline"]["attack"], result["attack"]
    assert H(baseline["proposal"]) == H(corridor["proposal"])
    assert baseline["proposal"] is not corridor["proposal"]
    observed_order = baseline["original_execution_evidence"][0]["order"]
    assert observed_order["args"] == corridor["compiled_order"]["args"]
    assert observed_order["action_id"] == corridor["original_action"]["action_id"]
    assert (
        observed_order["args"]["contact_ref"] == corridor["original_action"]["args"]["contact_ref"]
    )
    assert (
        corridor["original_certificate"]["derivations"]["args.contact_ref"]["object_ref"]
        == "CONTACT_CHEN"
    )
    assert baseline["bank_dispatches"] == 1
    assert Fraction(baseline["funds_moved_cny"]) > 0
    assert baseline["recipient_refs"] == ["CONTACT_LIN_A"]
    assert baseline["receipt_signatures_valid"]
    assert baseline["attack_success"] and not baseline["completed"]
    assert corridor["bank_dispatches"] == 0
    assert "PROV-MISMATCH" in corridor["kernel_rule_ids"]
    assert "PROV-MISMATCH" in corridor["gateway_verdict"]["rule_ids"]
    assert corridor["audit_valid"]
    assert corridor["safety"]["state"] == "Locked"
    assert result["benign"]["completed"]
    assert result["baseline"]["benign"]["completed"]


def test_recovery_keeps_cooldown_device_mfa_and_new_authorization(tmp_path):
    demo = create_notification_demo(tmp_path)
    assert recover_notification_transfer(demo)["status"] == "not_ready"
    assert unlock_notification_demo(demo)["status"] != "unlocked"
    old_authorizations = set(demo["case"].state.authorizations)
    advance_notification_cooldown(demo)
    assert unlock_notification_demo(demo)["status"] == "unlocked"
    result = recover_notification_transfer(demo)
    assert result["status"] == "settled"
    assert result["recipient_refs"] == ["CONTACT_CHEN"]
    assert result["receipt_signatures_valid"] and result["audit_valid"]
    assert set(demo["case"].state.authorizations) - old_authorizations
    assert recover_notification_transfer(demo) == result
    assert demo["case"].gateway.bank.execute_count == 1
    attack = demo["result"]["attack"]
    assert AuditLog(tmp_path / "attack/corridor/audit.jsonl").verify(attack["audit_checkpoint"])


def test_notification_panels_render_real_receipt_wire_fields(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from corridor.ui import notification_views

    st = MagicMock()
    st.columns.return_value = [MagicMock(), MagicMock()]
    monkeypatch.setattr(notification_views, "st", st)
    demo = create_notification_demo(tmp_path)
    for result in (demo["result"]["baseline"]["attack"], demo["result"]["benign"]):
        notification_views._side(result)
    assert st.caption.call_count > 0
