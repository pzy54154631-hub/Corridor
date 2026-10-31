"""Logs name routes and statuses, never identifiers, bodies or exception text (design §2.3)."""

import logging
import time

import pytest

from tests.web.conftest import ws_of

LOGGERS = ("tornado.access", "tornado.application", "tornado.general")
BODY = "SENTINEL-BODY-0xBEEF"
QUERY = "QUERY-SECRET-0xCAFE"
FAILURE = "secret-message C:\\private\\path"


@pytest.fixture
def web_settings(doubles):
    return doubles.settings()


@pytest.fixture
def logged(caplog):
    for name in LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)
    return caplog


def lines(caplog, *expected):
    """The captured lines, once every expected line is in.

    The server writes an access line after the answer is on the wire, so a test can read
    the answer before the line exists.
    """
    deadline = time.monotonic() + 10
    while True:
        captured = [
            (record.name, record.levelno, record.getMessage(), record.exc_info)
            for record in list(caplog.records)
            if record.name in LOGGERS
        ]
        if set(expected) <= {message for _, _, message, _ in captured}:
            return captured
        assert time.monotonic() < deadline, (expected, captured)
        time.sleep(0.01)


def test_logs_hold_route_templates_and_statuses_only(web_app, client, doubles, logged):
    client.open()
    ws = ws_of(web_app, client)
    client.post(
        "/api/chat/message", {"text": BODY, "external_files": 0, "client_msg_id": "m-000001"}
    )
    client.get(f"/api/phone/{ws.pair}?lang=en&view=judge")
    client.get(f"/phone/{ws.pair}")
    client.get(f"/api/nothing?token={QUERY}")
    client.post("/api/chat/message", raw=b'{"text": "' + BODY.encode() + b'"')
    client.post(
        "/api/chat/message",
        raw=BODY.encode(),
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )
    doubles.raises["chat_stop"] = RuntimeError(FAILURE)
    assert client.post("/api/chat/stop", {}).code == 500

    captured = lines(logged, "POST /api/chat/stop 500", "POST /api/chat/stop failed: RuntimeError")
    text = "\n".join(message for _, _, message, _ in captured)
    for secret in (ws.pair, client.tab, client.browser, client.generation, BODY, QUERY, FAILURE):
        assert secret not in text
    assert "secret" not in text and "Traceback" not in text
    assert all(exc_info is None for *_, exc_info in captured)
    access = [message for name, _, message, _ in captured if name == "tornado.access"]
    assert "POST /api/chat/message 200" in access
    assert "GET /api/phone/{pair} 200" in access
    assert any(message.startswith("GET /phone/{pair} ") for message in access)
    assert "GET /api/* 404" in access
    assert "POST /api/chat/message 400" in access
    assert "POST /api/chat/stop 500" in access
    failures = [message for name, _, message, _ in captured if name == "tornado.application"]
    assert failures == ["POST /api/chat/stop failed: RuntimeError"]


def test_access_levels_follow_the_status(web_app, client, logged):
    client.open()
    client.get("/api/state")
    client.get("/api/state", headers={"X-Corridor-Tab": None})
    captured = lines(logged, "GET /api/state 200", "GET /api/state 401")
    levels = {message: level for name, level, message, _ in captured if name == "tornado.access"}
    assert levels["GET /api/state 200"] == logging.INFO
    assert levels["GET /api/state 401"] == logging.WARNING


def test_unknown_methods_are_logged_generically(web_app, client, logged):
    response = client.request("PROPFIND", f"/api/x?{QUERY}", None, [("Host", client.host)])
    assert response.code == 405
    text = "\n".join(message for _, _, message, _ in lines(logged, "OTHER /api/* 405"))
    assert QUERY not in text and "PROPFIND" not in text


def test_the_pair_registry_and_store_do_not_print_identifiers(web_app, client):
    client.open()
    store = web_app.settings["store"]
    ws = ws_of(web_app, client)
    assert ws.pair not in repr(store.pairs) and client.tab not in repr(store)
    assert repr(store.pairs) == "PairRegistry(<1 allowed>)"
