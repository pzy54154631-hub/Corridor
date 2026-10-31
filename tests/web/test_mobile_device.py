"""Mobile SMS disclosure uses existing task state; no code enters the main view."""

import json
from pathlib import Path

import pytest

from tests.web.test_viewmodels import RED, YELLOW, Web


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_sms_disclosure_inputs_follow_the_pending_verification(locale):
    web = Web(locale=locale).login()
    web.send(YELLOW)
    assert web.task()["gesture"]["op"] == "confirm"
    web.gesture()
    web.send(RED)
    web.gesture()
    assert web.task()["step"] == "mfa_request"
    web.gesture()
    task = web.task()
    assert task["step"] == "mfa_code"
    assert task["gesture"]["op"] == "mfa_verify"
    assert web.code(task["key"]) not in json.dumps(web.state(), ensure_ascii=False)
    web.gesture()
    assert web.task()["step"] == "final_confirm"
    catalog = json.loads(
        (Path(__file__).resolve().parents[2] / "src/corridor/web/messages-web-ui.json").read_text(
            encoding="utf-8"
        )
    )
    for key in ("web.ui.sms.expand", "web.ui.sms.collapse"):
        assert catalog[key][locale]
