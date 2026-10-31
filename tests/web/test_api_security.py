"""Transport, origin, body and pairing negatives (design §6.5 items 10, 11, 14, 24–26).

Every refused request must leave the session untouched: no service call, no refresh, no
live switch and no new revision.
"""

import http.client
import json
import re
import subprocess
import sys
import threading

import pytest

from corridor.web.security import CSP
from tests.web.conftest import SMS_CODE, WebClient, serve, ws_of

MESSAGE = {"text": "hi", "external_files": 0, "client_msg_id": "m-000001"}
SIDE_EFFECTS = {"send_message", "device_mfa_verify", "device_confirm", "refresh", "live.sync"}


@pytest.fixture
def web_settings(doubles):
    return doubles.settings()


@pytest.fixture
def opened(web_app, client):
    client.open(view="customer")
    return client


def untouched(doubles, ws, revision):
    return not SIDE_EFFECTS & set(doubles.names(ws)[2:]) and ws.revision == revision


def error_of(client, response):
    return client.json(response)["error"]["code"]


# --- item 10 and 24: who may send a request -------------------------------------------------


def test_a_missing_tab_or_cookie_is_no_session(web_app, opened, doubles):
    ws = ws_of(web_app, opened)
    for headers in ({"X-Corridor-Tab": None}, {"Cookie": None}, {"Cookie": "corridor_web=x"}):
        response = opened.post("/api/chat/message", MESSAGE, headers=headers)
        assert response.code == 401 and error_of(opened, response) == "no_session", headers
        assert "state" not in opened.json(response)
    assert untouched(doubles, ws, 0)


def test_a_tab_only_works_with_the_cookie_that_opened_it(web_app, opened, other_browser, doubles):
    other_browser.open()
    ws = ws_of(web_app, opened)
    stolen = {"X-Corridor-Tab": opened.tab, "X-Corridor-Generation": opened.generation}
    response = other_browser.post("/api/chat/message", MESSAGE, headers=stolen)
    assert response.code == 401
    mine = {"X-Corridor-Tab": other_browser.tab, "X-Corridor-Generation": opened.generation}
    assert opened.post("/api/chat/message", MESSAGE, headers=mine).code == 401
    assert untouched(doubles, ws, 0)


@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:{port}", "127.0.0.1:8501", "127.0.0.2:{port}", "0.0.0.0:{port}"],
)
def test_hosts_outside_the_loopback_whitelist_are_refused(web_app, opened, doubles, host):
    ws = ws_of(web_app, opened)
    host = host.format(port=opened.port)
    headers = {"Host": host, "Origin": "http://" + host}
    for response in (
        opened.post("/api/chat/message", MESSAGE, headers=headers),
        opened.get("/api/state", headers={"Host": host}),
        opened.get(f"/api/phone/{ws.pair}", headers={"Host": host}),
        opened.get("/", headers={"Host": host}),
    ):
        assert response.code == 403 and error_of(opened, response) == "forbidden_origin"
    assert untouched(doubles, ws, 0)


def test_every_loopback_spelling_of_the_host_is_served(web_app, opened):
    for name in ("localhost", "[::1]"):
        host = f"{name}:{opened.port}"
        response = opened.post(
            "/api/chat/stop", {}, headers={"Host": host, "Origin": "http://" + host}
        )
        assert response.code == 200, name


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "",
        "http://127.0.0.1:8501",
        "http://localhost:{port}",
        "https://127.0.0.1:{port}",
        "http://127.0.0.1:{port}/",
        "http://evil.example",
        ["http://127.0.0.1:{port}", "http://127.0.0.1:{port}"],
    ],
)
def test_posts_need_exactly_one_same_origin_header(web_app, opened, doubles, origin):
    ws = ws_of(web_app, opened)
    if isinstance(origin, list):
        origin = [item.format(port=opened.port) for item in origin]
    elif origin is not None:
        origin = origin.format(port=opened.port)
    response = opened.post("/api/chat/message", MESSAGE, headers={"Origin": origin})
    assert response.code == 403 and error_of(opened, response) == "forbidden_origin"
    assert untouched(doubles, ws, 0)


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "text/plain;charset=UTF-8",
        "application/x-www-form-urlencoded",
        "application/json; charset=latin-1",
        "application/json, text/plain",
        ["application/json", "application/json"],
    ],
)
def test_posts_must_be_json(web_app, opened, doubles, content_type):
    ws = ws_of(web_app, opened)
    response = opened.post("/api/chat/message", MESSAGE, headers={"Content-Type": content_type})
    assert response.code == 403
    assert untouched(doubles, ws, 0)


def test_a_cross_site_form_post_is_refused(web_app, opened, doubles):
    ws = ws_of(web_app, opened)
    form = b"\r\n".join([
        b"--x",
        b'Content-Disposition: form-data; name="text"',
        b"",
        b"hi",
        b"--x--",
        b"",
    ])
    headers = {"Content-Type": "multipart/form-data; boundary=x", "Origin": "http://evil.example"}
    assert opened.post("/api/chat/message", raw=form, headers=headers).code == 403
    same_origin = {"Content-Type": "multipart/form-data; boundary=x"}
    assert opened.post("/api/chat/message", raw=form, headers=same_origin).code == 403
    # A form body tornado cannot even parse is refused before the handler runs.
    broken = opened.post("/api/chat/message", raw=b"{}", headers=same_origin)
    assert broken.code == 400 and error_of(opened, broken) == "bad_json"
    assert untouched(doubles, ws, 0)


@pytest.mark.parametrize(
    "content_type", ["application/json; charset=utf-8", "Application/JSON;charset=UTF-8"]
)
def test_json_with_a_utf8_charset_is_accepted(opened, content_type):
    assert opened.post("/api/chat/stop", {}, headers={"Content-Type": content_type}).code == 200


def test_opening_a_session_needs_the_custom_header_too(web_app, doubles):
    page = WebClient(web_app)
    for headers in (
        {"X-Corridor-Web": None},
        {"X-Corridor-Web": "0"},
        {"X-Corridor-Web": ["1", "1"]},
        {"Origin": None},
        {"Origin": "null"},
        {"Content-Type": "text/plain"},
    ):
        response = page.post("/api/session", {}, headers=headers)
        assert response.code == 403, headers
        assert not response.headers.get_list("Set-Cookie")
    assert doubles.sessions == []


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Corridor-Tab": "short"},
        {"X-Corridor-Generation": None},
        {"X-Corridor-Generation": "not-a-generation!"},
        {"X-Corridor-Tab": ["{tab}", "{tab}"]},
        {"Cookie": ["corridor_web={browser}", "corridor_web={browser}"]},
        {"Cookie": "corridor_web={browser}; corridor_web={browser}"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_malformed_or_repeated_headers_are_refused(web_app, opened, doubles, headers):
    ws = ws_of(web_app, opened)
    values = {"tab": opened.tab, "browser": opened.browser, "port": opened.port}

    def fill(value):
        if isinstance(value, list):
            return [fill(item) for item in value]
        return value.format(**values) if isinstance(value, str) else value

    headers = {name: fill(value) for name, value in headers.items()}
    assert opened.post("/api/chat/message", MESSAGE, headers=headers).code == 403
    get = opened.get("/api/state", headers=headers)
    assert get.code == 403 and "state" not in opened.json(get)
    assert untouched(doubles, ws, 0)


def test_a_repeated_host_is_refused_before_any_handler(web_app, opened, doubles):
    ws = ws_of(web_app, opened)
    host = [opened.host, opened.host]

    def refused(call):
        # Tornado's HTTP layer answers a repeated Host itself with a bare 400 and hangs up;
        # with an unread body the hang-up may reset the connection before the 400 arrives.
        try:
            return call().code == 400
        except ConnectionError:
            return True

    assert refused(lambda: opened.post("/api/chat/message", MESSAGE, headers={"Host": host}))
    assert refused(lambda: opened.get("/api/state", headers={"Host": host}))
    assert untouched(doubles, ws, 0)


def test_reads_refuse_a_foreign_origin_but_need_none(web_app, opened):
    assert opened.get("/api/state").code == 200
    assert opened.get("/api/state", headers={"Origin": opened.origin}).code == 200
    assert opened.get("/api/state", headers={"Sec-Fetch-Site": "same-origin"}).code == 200
    assert opened.get("/api/state", headers={"Origin": "http://evil.example"}).code == 403


def test_options_and_other_methods_get_405_without_cors(web_app, opened):
    for method in ("OPTIONS", "PUT", "DELETE", "PATCH"):
        for path in ("/api/session", "/api/chat/message", "/api/state", "/"):
            response = opened.request(
                method,
                path,
                b"{}",
                [
                    ("Host", opened.host),
                    ("Origin", "http://evil.example"),
                    ("Access-Control-Request-Method", "POST"),
                ],
            )
            assert response.code == 405, (method, path)
            assert error_of(opened, response) == "method_not_allowed"
            assert not [name for name in response.headers if name.lower().startswith("access-")]
    assert opened.get("/api/chat/message").code == 405
    assert opened.post("/api/state", {}).code == 405


# --- item 11: bodies ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"", "bad_json"),
        (b"[]", "bad_json"),
        (b'"text"', "bad_json"),
        (b"{", "bad_json"),
        (b'{"text": "hi", "external_files": 0.0, "client_msg_id": "m-000001"}', "bad_json"),
        (b'{"text": "hi", "external_files": 1e0, "client_msg_id": "m-000001"}', "bad_json"),
        (b'{"text": "hi", "external_files": NaN, "client_msg_id": "m-000001"}', "bad_json"),
        (b'{"text": "hi", "external_files": Infinity, "client_msg_id": "m-000001"}', "bad_json"),
        (
            b'{"text": "a", "text": "b", "external_files": 0, "client_msg_id": "m-000001"}',
            "bad_json",
        ),
        (b'{"text": "\\ud800", "external_files": 0, "client_msg_id": "m-000001"}', "bad_json"),
        (b'{"text": "\xff", "external_files": 0, "client_msg_id": "m-000001"}', "bad_json"),
        (
            b'\xef\xbb\xbf{"text": "hi", "external_files": 0, "client_msg_id": "m-000001"}',
            "bad_json",
        ),
        (b'{"text": "hi", "external_files": true, "client_msg_id": "m-000001"}', "bad_json"),
        (b'{"text": "hi", "external_files": 2, "client_msg_id": "m-000001"}', "bad_json"),
        (b'{"text": "hi", "external_files": "1", "client_msg_id": "m-000001"}', "bad_json"),
        (b'{"text": 7, "external_files": 0, "client_msg_id": "m-000001"}', "bad_json"),
        (b'{"text": "hi", "external_files": 0, "client_msg_id": "short"}', "bad_json"),
        (b'{"text": "hi", "external_files": 0}', "bad_json"),
        (
            b'{"text": "hi", "external_files": 0, "client_msg_id": "m-000001", "x": 1}',
            "unknown_field",
        ),
    ],
)
def test_bodies_are_one_strict_json_object(web_app, opened, doubles, raw, code):
    ws = ws_of(web_app, opened)
    response = opened.post("/api/chat/message", raw=raw)
    assert response.code == 400 and error_of(opened, response) == code
    assert "state" not in opened.json(response)
    assert untouched(doubles, ws, 0)


def test_bodies_above_64_kib_are_refused_whole(web_app, opened, doubles):
    ws = ws_of(web_app, opened)
    text = "x" * (64 * 1024)
    response = opened.post("/api/chat/message", {**MESSAGE, "text": text})
    assert response.code == 413 and error_of(opened, response) == "too_large"
    assert untouched(doubles, ws, 0)


def test_card_fields_in_a_confirmation_are_unknown_fields(web_app, opened, doubles):
    ws = ws_of(web_app, opened)
    gesture = {"gesture": "g" * 22, "card_version": "0" * 32}
    for extra in ("card", "amount", "contact_ref", "action", "nodes", "code"):
        response = opened.post("/api/device/tasks/TR1/confirm", {**gesture, extra: "x"})
        assert response.code == 400 and error_of(opened, response) == "unknown_field", extra
    assert untouched(doubles, ws, 0)


@pytest.mark.parametrize("text", ["", "   \n\t", "x" * 4001])
def test_text_length_is_checked_before_anything_runs(web_app, opened, doubles, text):
    ws = ws_of(web_app, opened)
    response = opened.post("/api/chat/message", {**MESSAGE, "text": text})
    data = opened.json(response)
    assert response.code == 422 and data["error"]["code"] == "invalid_input"
    assert data["state"]["revision"] == data["revision"] == 1
    assert not SIDE_EFFECTS & set(doubles.names(ws)[2:])


def test_four_thousand_characters_are_accepted(opened):
    assert opened.post("/api/chat/message", {**MESSAGE, "text": "x" * 4000}).code == 200


@pytest.mark.parametrize(
    "code", [None, "", "12345", "1234567", "12a456", "１２３４５６", 123456, ["123456"]]
)
def test_a_code_that_is_not_six_digits_never_reaches_the_service(web_app, opened, doubles, code):
    ws = ws_of(web_app, opened)
    body = {"gesture": "g" * 22, "card_version": "0" * 32}
    if code is not None:
        body["code"] = code
    for request in (body, {**body, "code": None}):
        response = opened.post("/api/device/tasks/TR1/mfa/verify", request)
        assert response.code == 422 and error_of(opened, response) == "invalid_input"
    assert "device_mfa_verify" not in doubles.names(ws)


def test_a_six_digit_code_reaches_the_service_as_a_string(web_app, opened, doubles):
    body = {"gesture": "g" * 22, "card_version": "0" * 32, "code": "012345"}
    response = opened.post("/api/device/tasks/TR1/mfa/verify", body)
    assert response.code == 200
    (record,) = [item for item in doubles.records if item.name == "device_mfa_verify"]
    assert record.args == ("TR1", "g" * 22, "0" * 32, "012345")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/device/tasks/TR1/confirm", {"gesture": "g" * 21, "card_version": "0" * 32}),
        ("/api/device/tasks/TR1/confirm", {"gesture": "g" * 22, "card_version": "0" * 15}),
        ("/api/device/tasks/TR1/confirm", {"gesture": "g" * 22, "card_version": "A" * 32}),
        ("/api/chat/responses/R1/gate/answer", {"gate_version": "v" * 16, "answers": {"x": "yes"}}),
        (
            "/api/chat/responses/R1/gate/answer",
            {"gate_version": "v" * 16, "answers": {"verified": "y"}},
        ),
        ("/api/chat/responses/R1/gate/answer", {"gate_version": "v" * 15, "answers": {}}),
        ("/api/chat/responses/R1/recipient", {"choice": "c01"}),
        ("/api/session/view", {"view": "auditor"}),
        ("/api/session/locale", {"locale": "fr"}),
        ("/api/chat/shortcut", {"shortcut": "lottery", "client_msg_id": "m-000001"}),
    ],
)
def test_formats_are_checked_before_the_session_is_touched(web_app, opened, doubles, path, body):
    ws = ws_of(web_app, opened)
    response = opened.post(path, body)
    assert response.code == 400 and error_of(opened, response) in ("bad_json", "unknown_field")
    assert untouched(doubles, ws, 0)


def test_an_empty_answer_set_is_invalid_input(web_app, opened, doubles):
    response = opened.post(
        "/api/chat/responses/R1/gate/answer", {"gate_version": "v" * 16, "answers": {}}
    )
    assert response.code == 422 and error_of(opened, response) == "invalid_input"
    assert "gate_answer" not in doubles.names()


# --- item 14: nothing of an exception leaves the server -------------------------------------


@pytest.mark.parametrize(
    "exc", [RuntimeError("secret-path C:\\x"), TypeError("secret"), KeyError("secret")]
)
def test_unexpected_exceptions_answer_a_generic_500(web_app, opened, doubles, exc):
    ws = ws_of(web_app, opened)
    doubles.raises["send_message"] = exc
    response = opened.post("/api/chat/message", MESSAGE)
    text = response.body.decode("utf-8")
    assert response.code == 500 and error_of(opened, response) == "outcome_unknown"
    assert "secret" not in text and "Traceback" not in text and "C:\\" not in text
    # R1-E6: the session is distrusted and refreshed once; nothing else runs again.
    assert ws.untrusted is True
    assert opened.json(response)["state"]["revision"] == ws.revision
    del doubles.raises["send_message"]
    before = len(doubles.names(ws))
    refused = opened.post("/api/chat/message", {**MESSAGE, "client_msg_id": "m-000002"})
    assert refused.code == 409 and error_of(opened, refused) == "not_applicable"
    assert "send_message" not in doubles.names(ws)[before:]
    assert opened.post("/api/chat/stop", {}).code == 200
    assert opened.post("/api/session/reset", {}).code == 200


def test_expected_failures_after_the_call_keep_state_and_refresh(web_app, opened, doubles):
    from corridor.web.errors import WebOperationFailed, WebOutcomeUnknown

    ws = ws_of(web_app, opened)
    for exc, status, code, distrusted in (
        (WebOperationFailed(), 422, "operation_failed", False),
        (WebOutcomeUnknown(), 500, "outcome_unknown", True),
    ):
        doubles.raises["device_confirm"] = exc
        before = len(doubles.names(ws))
        response = opened.post(
            "/api/device/tasks/TR1/confirm", {"gesture": "g" * 22, "card_version": "0" * 32}
        )
        data = opened.json(response)
        assert response.code == status and data["error"]["code"] == code
        assert data["state"]["revision"] == data["revision"] == ws.revision
        # R2-E3: an unknown result (even one raised as a WebError) also distrusts the
        # session, which refreshes it once more with every token and gate record gone.
        assert getattr(ws, "untrusted", False) is distrusted
        assert doubles.names(ws)[before:] == [
            "live.sync",
            "device_confirm",
            "live.sync",
            "refresh",
            *(["refresh"] if distrusted else []),
            "app_state",
        ]


def test_floats_and_mismatched_states_never_leave_the_server(web_app, opened, doubles):
    original = doubles.vm.app.app_state
    doubles.vm.app.app_state = lambda ws: {**original(ws), "ratio": 0.5}
    response = opened.get("/api/state")
    assert response.code == 500 and error_of(opened, response) == "internal"
    doubles.vm.app.app_state = lambda ws: {**original(ws), "revision": ws.revision + 1}
    assert opened.post("/api/chat/stop", {}).code == 500


# --- items 25 and 26: pairings and generations ----------------------------------------------


def phone(client, pair, query=""):
    response = client.get(f"/api/phone/{pair}{query}", headers={"Cookie": None})
    return response, client.json(response)


def test_a_reset_revokes_the_old_pairing(web_app, opened):
    old = ws_of(web_app, opened).pair
    response, data = phone(opened, old)
    assert response.code == 200 and SMS_CODE in json.dumps(data)
    opened.post("/api/session/reset", {})
    new = ws_of(web_app, opened).pair
    response, data = phone(opened, old)
    assert response.code == 200 and data["unpaired"] and SMS_CODE not in json.dumps(data)
    assert SMS_CODE in json.dumps(phone(opened, new)[1])
    assert "generation" not in data and "revision" not in data


def test_eviction_revokes_the_pairing(web_settings, doubles):
    with serve({**web_settings, "max_sessions": 1}) as app:
        first, second = WebClient(app), WebClient(app)
        first.open()
        pair = ws_of(app, first).pair
        second.open()
        response, data = phone(second, pair)
        assert data["unpaired"] and SMS_CODE not in json.dumps(data)


def test_an_unknown_pair_looks_exactly_like_a_revoked_one(web_app, opened):
    _, unknown = phone(opened, "Z" * 22)
    old = ws_of(web_app, opened).pair
    opened.post("/api/session/reset", {})
    _, revoked = phone(opened, old)
    assert unknown == revoked


def test_the_phone_renders_in_the_requested_locale_and_view(web_app, opened):
    pair = ws_of(web_app, opened).pair
    assert phone(opened, pair)[1]["rendered"] == {"locale": "zh-CN", "audience": "customer"}
    rendered = phone(opened, pair, "?lang=en&view=judge")[1]["rendered"]
    assert rendered == {"locale": "en", "audience": "judge"}
    for query in ("?lang=fr", "?view=auditor", "?lang=en&lang=zh-CN"):
        assert phone(opened, pair, query)[0].code == 404
    for bad in ("short", "has.dot" + "x" * 16, "x" * 65):
        assert opened.get(f"/api/phone/{bad}").code == 404
        assert opened.get(f"/phone/{bad}").code == 404


def test_phone_routes_are_no_store_and_no_referrer(web_app, opened):
    pair = ws_of(web_app, opened).pair
    for path in (f"/phone/{pair}", f"/api/phone/{pair}", "/phone/short"):
        response = opened.get(path, headers={"Cookie": None})
        assert response.headers["Cache-Control"] == "no-store", path
        assert response.headers["Referrer-Policy"] == "no-referrer", path


def test_in_flight_requests_finish_in_their_own_generation(web_app, opened, doubles):
    gate = doubles.block("send_message")
    old = opened.generation
    answers = {}

    def send():
        answers["message"] = opened.post("/api/chat/message", MESSAGE)

    def reset():
        answers["reset"] = opened.post("/api/session/reset", {})

    sender = threading.Thread(target=send)
    sender.start()
    assert doubles.wait_entered("send_message")
    resetter = threading.Thread(target=reset)
    resetter.start()
    resetter.join(0.3)
    assert resetter.is_alive()  # the reset waits for the call in progress
    gate.set()
    sender.join(10)
    resetter.join(10)
    message, reset_answer = (opened.json(answers[name]) for name in ("message", "reset"))
    assert answers["message"].code == 200 and message["generation"] == old
    assert answers["reset"].code == 200 and reset_answer["generation"] != old
    late = opened.post("/api/chat/stop", {}, headers={"X-Corridor-Generation": old})
    assert late.code == 409 and error_of(opened, late) == "stale_generation"


def test_requests_queued_behind_a_reset_answer_with_the_new_state(web_app, opened, doubles):
    gate = doubles.block("stop_all")
    old = opened.generation
    answers = {}

    def call(name, path):
        answers[name] = opened.post(path, {}, headers={"X-Corridor-Generation": old})

    first = threading.Thread(target=call, args=("stop", "/api/stop"))
    first.start()
    assert doubles.wait_entered("stop_all")
    threads = [
        threading.Thread(target=call, args=("reset", "/api/session/reset")),
        threading.Thread(target=call, args=("queued", "/api/chat/handoff")),
    ]
    for thread in threads:
        thread.start()
        thread.join(0.2)
    gate.set()
    for thread in (first, *threads):
        thread.join(10)
    queued = opened.json(answers["queued"])
    new = opened.json(answers["reset"])["generation"]
    if answers["queued"].code == 409:
        assert queued["error"]["code"] == "stale_generation"
        assert queued["generation"] == queued["state"]["generation"] == new
        assert "handoff" not in doubles.names(ws_of(web_app, opened))
    else:
        # The handoff got the old session's lock before the reset did.
        assert queued["generation"] == old


# --- headers on every answer ----------------------------------------------------------------


def test_every_answer_carries_the_security_headers(web_app, opened):
    pair = ws_of(web_app, opened).pair
    answers = [
        opened.get("/api/state"),
        opened.post("/api/chat/message", raw=b"{"),
        opened.get("/api/state", headers={"X-Corridor-Tab": None}),
        opened.get("/api/nothing"),
        opened.get("/"),
        opened.get(f"/api/phone/{pair}"),
        opened.get("/static/js/does-not-exist.js"),
        opened.request("OPTIONS", "/api/state", None, [("Host", opened.host)]),
    ]
    for response in answers:
        assert response.headers["Content-Security-Policy"] == CSP
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "Server" not in response.headers
        assert not [name for name in response.headers if name.lower().startswith("access-")]
    for response in answers[:4]:
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Content-Type"] == "application/json; charset=utf-8"


def test_static_files_are_served_by_extension_with_a_javascript_type(web_app, opened):
    response = opened.get("/static/js/api.js")
    if response.code == 404:
        pytest.skip("static/js/api.js is not in this tree yet")
    assert response.headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert opened.get("/js/api.js").body == response.body
    assert opened.get("/static/js").code == 404
    assert opened.get("/static/../app.py").code == 404
    assert opened.get("/static/../messages-web-ui.json").code == 404
    assert opened.get("/static/%2e%2e/app.py").code == 404


# --- the real stack: codes stay on the phone, and the entry point stays on loopback --------

RED = "给妈妈转5000元"
CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")


@pytest.fixture
def real_app(st_sentinel):
    with serve({}) as app:
        yield app


def test_a_real_code_reaches_only_the_phone_and_expires_with_its_pairing(real_app):
    from corridor.ui.phone_outbox import OUTBOX

    page = WebClient(real_app)
    page.open(view="customer")
    bodies = [page.post("/api/session/login", {}).body]
    answer = page.post(
        "/api/chat/message", {"text": RED, "external_files": 0, "client_msg_id": "m-real-0002"}
    )
    bodies.append(answer.body)
    data = page.json(answer)
    for op, path in (("confirm", "confirm"), ("mfa_request", "mfa/request")):
        (task,) = [item for item in data["state"]["device"]["tasks"] if item["current"]]
        gesture = task["gesture"]
        assert gesture["op"] == op, task["step"]
        answer = page.post(
            f"/api/device/tasks/{task['key']}/{path}",
            {"gesture": gesture["token"], "card_version": gesture["card_version"]},
        )
        bodies.append(answer.body)
        data = page.json(answer)
    ws = ws_of(real_app, page)
    phone = page.json(page.get(f"/api/phone/{ws.pair}"))
    codes = set(CODE.findall(json.dumps(phone["messages"], ensure_ascii=False)))
    assert len(codes) == 1
    for path in ("/api/state", "/api/i18n", "/api/tabs/operator"):
        bodies.append(page.get(path).body)
    assert not [body for body in bodies for code in codes if code.encode() in body]
    old = ws.pair
    page.post("/api/session/reset", {})
    after = page.json(page.get(f"/api/phone/{old}"))
    assert after["unpaired"] and after["messages"] == []
    assert not codes & set(CODE.findall(json.dumps(after, ensure_ascii=False)))
    assert all(sms.status == "expired" and not sms.code for sms in OUTBOX.inbox(old))


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.com", ""])
def test_the_entry_point_refuses_any_other_host(host):
    from corridor.web.__main__ import main

    with pytest.raises(SystemExit) as refused:
        main(["--host", host])
    assert refused.value.code == 2


def test_bound_sockets_are_checked_to_be_loopback():
    from corridor.web.__main__ import loopback_only

    class Bound:
        def __init__(self, address):
            self.address = address

        def getsockname(self):
            return self.address

    assert loopback_only([Bound(("127.0.0.1", 1)), Bound(("::1", 1, 0, 0))])
    assert not loopback_only([Bound(("127.0.0.1", 1)), Bound(("192.168.1.2", 1))])
    assert not loopback_only([Bound(("0.0.0.0", 1))])
    assert not loopback_only([Bound(("::", 1, 0, 0))])
    assert not loopback_only([])


def test_the_entry_point_serves_on_loopback_and_prints_both_addresses():
    process = subprocess.Popen(
        [sys.executable, "-m", "corridor.web", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    lines = []

    def read_two():
        lines.extend(process.stdout.readline() for _ in range(2))

    reader = threading.Thread(target=read_two, daemon=True)
    reader.start()
    try:
        reader.join(60)
        first, second = (line.strip() for line in lines)
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", first) and second == first + "?view=judge"
        host = first.removeprefix("http://").rstrip("/")
        port = int(host.rsplit(":", 1)[1])
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", "/api/state", headers={"Host": host})
        answer = connection.getresponse()
        assert answer.status == 401 and json.loads(answer.read())["error"]["code"] == "no_session"
        connection.close()
    finally:
        process.terminate()
        process.wait(10)


# --- ext entry points: their bodies are as strict as the core ones -------------------------

EXT = frozenset({
    "plan_select",
    "compensate",
    "aa",
    "tab:accounts",
    "tab:wealth",
    "tab:plans",
    "shortcut:subscriptions",
    "shortcut:billing",
    "live",
})


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/api/device/tasks/TR1/nodes", {"card_version": "0" * 32, "nodes": ["n0", "n0"]}, 400),
        ("/api/device/tasks/TR1/nodes", {"card_version": "0" * 32, "nodes": ["n01"]}, 400),
        ("/api/device/tasks/TR1/nodes", {"card_version": "0" * 32, "nodes": "n0"}, 400),
        ("/api/device/tasks/TR1/nodes", {"card_version": "0" * 32, "nodes": ["n0"], "x": 1}, 400),
        ("/api/device/tasks/TR1/nodes", {"nodes": ["n0"]}, 400),
        ("/api/chat/responses/R1/birthday", {"choice": "c"}, 400),
        ("/api/chat/responses/R1/aa", {"choices": []}, 422),
        ("/api/chat/responses/R1/aa", {"choices": [f"c{i}" for i in range(21)]}, 422),
        ("/api/chat/responses/R1/aa", {"choices": ["c0", "c0"]}, 400),
        ("/api/chat/compensate", {"plan": "x"}, 400),
        ("/api/chat/compensate", {}, 400),
        ("/api/chat/compensate", {"plan_version": "0" * 16}, 400),
        ("/api/subscriptions/cancel", {"choice": 0}, 400),
        ("/api/subscriptions/cancel", {"choice": "c0"}, 400),
        ("/api/billing/analyze", {"period": "2026-9"}, 400),
        ("/api/billing/analyze", {"period": "２０２６-０９"}, 400),
        ("/api/wealth/compare", {"objective": "max_return"}, 400),
        ("/api/wealth/purchase", {}, 400),
        ("/api/wealth/purchase", {"choice": "x1"}, 400),
        ("/api/wealth/redeem", {"choice": None}, 400),
        ("/api/wealth/redeem", {"choice": "c0", "list_version": "Z" * 32}, 400),
        ("/api/presets/suitability-r4", {"risk": "R5"}, 400),
        ("/api/live/unlock", {"nonce": "0" * 32, "proof": "0" * 63}, 400),
        ("/api/live/unlock", {"nonce": "0" * 32, "proof": "0" * 64, "code": "x"}, 400),
        ("/api/live/select", {"deployment": "../x"}, 400),
        ("/api/live/toggle", {"on": 1}, 400),
    ],
)
def test_ext_bodies_are_checked_before_anything_runs(web_app, opened, doubles, path, body, status):
    web_app.settings["features"] = EXT
    ws = ws_of(web_app, opened)
    before = len(doubles.names(ws))
    response = opened.post(path, body)
    assert response.code == status, opened.json(response)
    assert ("state" in opened.json(response)) is (status == 422)
    called = set(doubles.names(ws)[before:]) - {"app_state"}
    assert not called, called


def test_ext_tab_answers_never_mix_revisions(web_app, opened, doubles):
    web_app.settings["features"] = EXT
    for path, body, name in (
        ("/api/subscriptions/query", {}, "accounts"),
        ("/api/billing/analyze", {}, "accounts"),
        ("/api/wealth/purchase", {"choice": None, "list_version": "0" * 32}, "wealth"),
        ("/api/presets/suitability-r4", {}, "wealth"),
    ):
        data = opened.json(opened.post(path, body))
        assert data["tab"]["name"] == name
        assert data["tab"]["revision"] == data["state"]["revision"] == data["revision"]
