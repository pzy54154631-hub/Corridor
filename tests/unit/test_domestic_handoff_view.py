"""The operator view uses the latest shared-channel handoff without raw dialogue."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

from corridor.crypto.jcs import H
from corridor.ui import app


def test_operator_displays_latest_redacted_session_dag_and_wealth_snapshot(monkeypatch):
    controller = SimpleNamespace(snapshot=lambda: {"plans": []})
    plan = {"plan_id": "PLAN_BIRTHDAY", "intent_id": "INTENT_BIRTHDAY"}
    payload = {
        "session_snapshot": [
            {
                "utterance_hash": H("private"),
                "intent": "birthday",
                "status": "preview",
                "response_id": "R1",
                "text": "PRIVATE_INPUT",
            }
        ],
        "dag_snapshot": [
            {
                "plan": plan,
                "blocked": True,
                "ownership_epoch": 1,
                "nodes": {
                    "NODE": {"status": "handoff", "receipt_ref": None, "raw_text": "PRIVATE_INPUT"}
                },
                "audit_checkpoint": {"head": H("bank-audit"), "seq": 5},
            }
        ],
        "wealth": {
            "execution": {
                "allocation_ref": "ALLOC",
                "plan_hash": H("wealth-plan"),
                "biac_hash": H("biac"),
                "status": "cancelled",
                "control_status": "handoff",
                "items": [],
                "raw_text": "PRIVATE_INPUT",
            }
        },
        "raw_text": "PRIVATE_INPUT",
    }
    old = {"response_id": "OLD", "payload": {"session_snapshot": []}, "status": "handoff"}
    newest = {"response_id": "NEW", "payload": payload, "status": "handoff", "intent": "handoff"}
    session = SimpleNamespace(
        controller=controller,
        history=[
            {"response_id": "OLD", "intent": "handoff"},
            {"response_id": "NEW", "intent": "handoff"},
        ],
        response=lambda identifier: deepcopy({"OLD": old, "NEW": newest}[identifier]),
    )
    display = MagicMock()
    display.session_state = {"domestic_session": session}
    display.button.return_value = False
    monkeypatch.setattr(app, "st", display)
    monkeypatch.setattr(app, "render_plans", MagicMock())
    app.handoff(controller)
    shown = [call.args[0] for call in display.json.call_args_list]
    handoff = next(value for value in shown if value.get("response_id") == "NEW")
    assert handoff["session_snapshot"][0]["utterance_hash"] == H("private")
    assert handoff["dag_snapshot"][0]["nodes"]["NODE"]["status"] == "handoff"
    assert handoff["wealth"]["execution"]["control_status"] == "handoff"
    assert "PRIVATE_INPUT" not in str(display.mock_calls)
    assert payload["raw_text"] == "PRIVATE_INPUT"


def test_operator_does_not_show_another_controller_handoff(monkeypatch):
    controller = SimpleNamespace(snapshot=lambda: {"plans": []})
    session = SimpleNamespace(controller=object(), history=[{"intent": "handoff"}])
    display = MagicMock()
    display.session_state = {"domestic_session": session}
    display.button.return_value = False
    monkeypatch.setattr(app, "st", display)
    monkeypatch.setattr(app, "render_plans", MagicMock())
    app.handoff(controller)
    assert not display.json.called
