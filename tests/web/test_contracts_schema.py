"""B2: every response body and fixture conforms to web-contract/1 (design §6.1).

The validator in ``corridor.web.contracts`` is checked both ways: the E2E fixtures and
every body the real handlers return across the core flows (both views, both locales)
pass, and crafted bodies with an unknown key, a float, a judge-only key in the customer
view, a malformed opaque key or a broken cross-field rule are each reported.
"""

import copy
import json
from pathlib import Path

import pytest

import corridor.web as web_package
from corridor.web import contracts
from tests.unit.test_ui_scam_friction import SAFE
from tests.unit.test_ui_scam_r7 import DIRECTED_NEW
from tests.web.test_customer_trim import EXT, Customer

pytestmark = pytest.mark.usefixtures("st_sentinel")
FIXTURES = sorted((Path(__file__).parents[1] / "web_e2e" / "fixtures").glob("*.json"))
YELLOW = "给妈妈转500元"
RED = "给妈妈转5000元"
FRAUD = "工作人员说立即给新联系人转480元，不然账户冻结"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", FIXTURES, ids=[path.name for path in FIXTURES])
def test_every_e2e_fixture_conforms(path):
    contracts.validate_response(load(path))


@pytest.fixture
def page(client, monkeypatch):
    monkeypatch.setattr(web_package, "FEATURES", web_package.FEATURES | EXT | {"live"})
    return Customer(client)


@pytest.mark.parametrize("view", ["customer", "judge"])
def test_every_handler_response_of_the_core_flows_conforms(page, client, view):
    page.keep(client.post("/api/session", {"view": view}), view=view)
    page.get("/api/state", view=view)
    page.get("/api/i18n", view=view)
    page.post("/api/session/login")
    page.send(YELLOW, "shape-0001")
    page.gesture()
    rkey = page.send(RED, "shape-0002")
    for _ in range(4):
        page.gesture()
    page.send(FRAUD, "shape-0003")
    page.answer(SAFE)
    page.send(DIRECTED_NEW, "shape-0004")
    page.post("/api/chat/handoff")
    for name in ("operator", "accounts", "plans", "wealth"):
        page.get(f"/api/tabs/{name}", view=view)
    page.get(f"/api/evidence/responses/{rkey}", view=view)
    page.get("/api/live", view=view)
    pair = page.state()["session"]["phone_url"].split("/phone/", 1)[1].split("?", 1)[0]
    page.get(f"/api/phone/{pair}?lang=zh-CN&view={view}", view=view)
    page.post("/api/session/locale", {"locale": "en"})
    page.state()
    page.post("/api/stop")
    found = [
        f"{url}: {problem}"
        for url, body, seen in page.bodies
        for problem in contracts.problems(body, view=view if seen == "customer" else seen)
    ]
    assert not found, found[:20]
    assert len(page.bodies) > 25


def red_state():
    return load(next(path for path in FIXTURES if path.name == "customer-red-code.json"))


def problems(body, **options):
    return contracts.problems(body, **options)


def test_an_unknown_key_is_reported():
    body = red_state()
    body["state"]["chat"]["extra"] = 1
    assert any("extra: not in the contract" in problem for problem in problems(body))


def test_a_float_anywhere_is_reported():
    body = red_state()
    body["state"]["revision"] = 7.0
    body["revision"] = 7.0
    assert any("float" in problem for problem in problems(body))


def test_a_judge_only_key_in_the_customer_view_is_reported():
    body = red_state()
    body["state"]["shell"]["judge_strip"] = "x"
    found = problems(body)
    assert any("judge_strip: judge-only key in the customer view" in item for item in found)
    body["state"]["view"] = "judge"
    assert not problems(body)


def test_a_missing_key_a_bad_literal_and_a_malformed_key_are_reported():
    body = red_state()
    del body["state"]["device"]["sms"]
    body["state"]["chat"]["responses"]["R2"]["tier"]["glyph"] = "star"
    body["state"]["device"]["tasks"][0]["gesture"]["card_version"] = "XYZ"
    found = " ".join(problems(body))
    assert "sms: missing" in found and "glyph" in found and "card_version" in found


def test_cross_field_rules_are_reported():
    body = red_state()
    broken = copy.deepcopy(body)
    broken["revision"] = 8
    assert any("differs from state.revision" in item for item in problems(broken))
    broken = copy.deepcopy(body)
    broken["state"]["device"]["tasks"][0]["step"] = "not_current"
    assert any("outside the whitelist" in item for item in problems(broken))
    broken = copy.deepcopy(body)
    broken["state"]["chat"]["turns"] = []
    assert any("not a reply of the listed turns" in item for item in problems(broken))


def test_a_hash_short_form_must_be_the_first_sixteen_digits():
    body = load(next(path for path in FIXTURES if path.name == "judge-red-code.json"))
    body["state"]["evidence"]["audit"]["head"]["short"] = "0" * 16
    assert any("first 16 digits" in item for item in problems(body))


def test_the_validator_raises_with_every_problem_listed():
    body = red_state()
    body["state"]["chat"]["extra"] = 1
    with pytest.raises(contracts.ContractError) as raised:
        contracts.validate_response(body)
    assert raised.value.problems and contracts.validate is contracts.validate_response
    assert contracts.check_response is contracts.validate_response


def test_live_select_and_toggle_bodies_need_no_result():
    # Contract §3.7: only the unlock answer carries a result beside the state and the panel.
    body = {**red_state(), "live": {}}
    assert contracts._shape(body) is contracts.OkStateLive
    assert contracts._shape({**body, "result": "accepted"}) is contracts.OkLiveResult
    assert not any(item.startswith("body.result") for item in problems(body))


def test_phone_and_tab_bodies_need_their_view_for_judge_keys():
    phone = {
        "ok": True,
        "version": "0" * 16,
        "title": "t",
        "status_bar": "s",
        "corner": "c",
        "messages": [],
        "empty": None,
        "unpaired": "u",
        "footer": "f",
        "disclosure": "d",
    }
    assert not problems(phone, view="judge")
    assert any("disclosure" in item for item in problems(phone, view="customer"))
