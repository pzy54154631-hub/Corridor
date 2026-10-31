"""Presets remain isolated simulations with actual gateway receipts."""

from corridor.ui.domestic_demos import prepare_demo


def test_threshold_preset_has_real_prior_payment_and_ambiguous_contacts():
    demo = prepare_demo("daily_threshold")
    controller = demo["controller"]
    assert demo["setup_result"]["receipt"]["status"] == "settled"
    draft = controller.preview_transfer("给妈妈转800元", request_id="DEMO-MOM")
    assert draft["status"] == "clarify"
    assert len(draft["contacts"]) == 2
    assert not draft["card"]
    selected = controller.select_contact(draft["draft_id"], draft["contacts"][0])
    assert selected["card"]["tier"] == "red"
    assert selected["card"]["total_after"] == "1100/1"
    assert controller.snapshot()["dispatch_count"] == 1
    assert controller.gateway.verify_log()


def test_unknown_preset_is_real_response_loss_and_stop_never_resends():
    demo = prepare_demo("birthday_unknown")
    controller = demo["controller"]
    assert any(item["status"] == "unknown" for item in demo["advance_result"]["results"])
    dispatches = controller.snapshot()["dispatch_count"]
    controller.stop(demo["plan_id"])
    assert controller.snapshot()["dispatch_count"] == dispatches
    assert controller.gateway.verify_log()
    assert any(
        node["status"] == "succeeded"
        for plan in controller.snapshot()["plans"]
        for node in plan["nodes"].values()
    )
