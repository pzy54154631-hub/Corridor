"""Sessions, tabs, generations and the request pipeline (design §2.2, §2.3, §6.1).

These tests drive ``make_app`` with module doubles (``conftest.Doubles``), so they check
the shell alone; the controllers' own behaviour is covered by the service tests.
"""

import pytest

from tests.web.conftest import WebClient, serve, ws_of


@pytest.fixture
def web_settings(doubles):
    return doubles.settings()


def test_a_new_browser_gets_a_cookie_a_server_tab_and_generation_zero(web_app, client):
    data = client.open()
    assert data["ok"] is True and data["revision"] == 0
    assert len(data["tab"]) == 32 and len(data["generation"]) == 12
    assert data["state"]["generation"] == data["generation"]
    assert data["state"]["revision"] == data["revision"] == 0
    ws = ws_of(web_app, client)
    assert ws.tab_id == data["tab"] and ws.browser_id == client.browser
    cookie = client.post("/api/session/view", {"view": "judge"}).headers.get_list("Set-Cookie")
    assert cookie == []


def test_the_cookie_is_http_only_strict_and_session_scoped(client):
    response = client.post("/api/session", {})
    (cookie,) = response.headers.get_list("Set-Cookie")
    parts = [part.strip().lower() for part in cookie.split(";")]
    assert parts[0].startswith("corridor_web=")
    assert {"httponly", "samesite=strict", "path=/"} <= set(parts)
    assert not any(part.startswith(("expires", "max-age", "domain")) for part in parts)


def test_the_same_tab_reuses_its_session_and_another_tab_gets_its_own(web_app, client, doubles):
    first = client.open()
    ws = ws_of(web_app, client)
    again = client.open()
    assert again["tab"] == first["tab"] and ws_of(web_app, client) is ws
    assert again["generation"] == first["generation"]
    second = WebClient(web_app)
    second.browser = client.browser
    other = second.open()
    assert other["tab"] != first["tab"] and ws_of(web_app, second) is not ws
    assert len(doubles.sessions) == 2


def test_a_client_supplied_tab_is_never_adopted(web_app, client, doubles):
    client.open()
    forged = "A" * 32
    response = client.post("/api/session", {"tab": forged})
    data = client.json(response)
    assert response.code == 200 and data["tab"] != forged
    assert web_app.settings["store"].peek(client.browser, forged) is None
    # Another browser cannot take over a known tab either: tabs belong to their cookie.
    thief = WebClient(web_app)
    stolen = thief.post("/api/session", {"tab": client.tab})
    assert thief.json(stolen)["tab"] != client.tab


def test_view_and_locale_apply_only_when_a_session_is_created(web_app, client):
    data = client.open(view="judge", locale="en")
    assert (data["state"]["view"], data["state"]["locale"]) == ("judge", "en")
    again = client.open(view="customer", locale="zh-CN")
    assert (again["state"]["view"], again["state"]["locale"]) == ("judge", "en")


def test_without_a_view_the_default_comes_from_the_environment(web_app, monkeypatch):
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "customer")
    assert WebClient(web_app).open()["state"]["view"] == "customer"
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "judge")
    assert WebClient(web_app).open()["state"]["view"] == "judge"
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "auditor")
    assert WebClient(web_app).open()["state"]["view"] == "customer"


def test_switching_view_and_locale_sticks_and_renders_in_the_new_context(web_app, client, doubles):
    client.open(view="customer", locale="zh-CN")
    response = client.post("/api/session/locale", {"locale": "en"})
    state = client.json(response)["state"]
    assert state["locale"] == "en" and state["rendered"] == {"locale": "en", "audience": "customer"}
    response = client.post("/api/session/view", {"view": "judge"})
    state = client.json(response)["state"]
    assert state["rendered"] == {"locale": "en", "audience": "judge"}
    ws = ws_of(web_app, client)
    # The shell POSTs never touch the live switch (design §2.3).
    assert "live.sync" not in doubles.names(ws)
    assert doubles.names(ws).count("refresh") == 3
    assert client.json(client.get("/api/state"))["state"]["view"] == "judge"


def test_every_post_that_reaches_the_session_counts_once_and_gets_do_not(web_app, client):
    client.open()
    revisions = []
    for path, body in (
        ("/api/session/login", {}),
        ("/api/chat/stop", {}),
        ("/api/chat/responses/R404/current", {}),
    ):
        revisions.append(client.json(client.post(path, body))["revision"])
    for _ in range(3):
        revisions.append(client.json(client.get("/api/state"))["revision"])
    assert revisions == [1, 2, 3, 3, 3, 3]


def test_a_reset_replaces_controllers_pairing_and_generation(web_app, client, doubles):
    opened = client.open(view="judge", locale="en")
    old = ws_of(web_app, client)
    response = client.post("/api/session/reset", {})
    data = client.json(response)
    new = ws_of(web_app, client)
    assert response.code == 200 and new is not old
    assert data["generation"] != opened["generation"] and data["revision"] == 0
    assert (new.view, new.locale) == ("judge", "en")
    assert new.controller is not old.controller and new.pair != old.pair
    pairs = web_app.settings["store"].pairs
    assert pairs.allowed(new.pair) and not pairs.allowed(old.pair)
    # Contract §6.6: the old pairing is revoked, its codes expire, then the live session moves.
    assert [name for name in doubles.names(old) if name in ("live.on_reset", "retire_pair")] == [
        "retire_pair",
        "live.on_reset",
    ]
    assert "live.on_new" in doubles.names(new)
    assert client.tab == opened["tab"] and "tab" not in data


def test_requests_of_an_old_generation_get_409_with_the_new_state(web_app, client, doubles):
    old = client.open()["generation"]
    new = client.json(client.post("/api/session/reset", {}))["generation"]
    ws = ws_of(web_app, client)
    before = list(doubles.names(ws))
    stale = {"X-Corridor-Generation": old}
    for response in (
        client.post("/api/chat/stop", {}, headers=stale),
        client.get("/api/state", headers=stale),
    ):
        data = client.json(response)
        assert response.code == 409 and data["error"]["code"] == "stale_generation"
        assert data["generation"] == data["state"]["generation"] == new
        assert data["revision"] == data["state"]["revision"] == 0
    # Neither request reached the new session's service, refresh or live switch.
    assert [name for name in doubles.names(ws) if name != "app_state"] == [
        name for name in before if name != "app_state"
    ]


def test_gets_change_nothing(web_app, client, doubles):
    client.open(view="judge")
    client.post(
        "/api/chat/message", {"text": "hi", "external_files": 0, "client_msg_id": "m-000001"}
    )
    ws = ws_of(web_app, client)
    before = (list(doubles.names(ws)), ws.revision)
    for path in ("/api/state", "/api/i18n", "/api/tabs/operator", "/api/evidence/responses/R1"):
        for _ in range(3):
            response = client.get(path)
            assert response.code == 200, (path, client.json(response))
    names = [name for name in doubles.names(ws) if name not in ("app_state", "tab")]
    assert (names, ws.revision) == (
        [name for name in before[0] if name not in ("app_state", "tab")],
        before[1],
    )
    assert not {"refresh", "live.sync"} & set(doubles.names(ws)[len(before[0]) :])


def test_i18n_answers_in_the_session_locale_and_view(web_app, client):
    client.open(view="customer", locale="en")
    data = client.json(client.get("/api/i18n"))
    assert (data["locale"], data["view"]) == ("en", "customer")
    assert data["strings"] == {"web.ui.probe": "en/customer"}
    assert "state" not in data


def test_evidence_is_only_offered_in_the_judge_view(web_app, client):
    client.open(view="customer")
    client.post(
        "/api/chat/message", {"text": "hi", "external_files": 0, "client_msg_id": "m-000001"}
    )
    assert client.json(client.get("/api/evidence/responses/R1")) == {
        "ok": True,
        "generation": client.generation,
        "revision": 1,
        "available": False,
    }
    client.post("/api/session/view", {"view": "judge"})
    data = client.json(client.get("/api/evidence/responses/R1"))
    assert data["available"] is True and data["technical"]["envelope"] == {"rkey": "R1"}
    missing = client.get("/api/evidence/responses/R9")
    assert missing.code == 404 and client.json(missing)["state"]["revision"] == 2


def test_the_business_pipeline_runs_in_order_under_the_session_context(web_app, client, doubles):
    client.open(view="customer", locale="en")
    response = client.post(
        "/api/chat/message", {"text": "pay mum", "external_files": 1, "client_msg_id": "m-000001"}
    )
    data = client.json(response)
    assert response.code == 200 and data["rkey"] == "R1"
    ws = ws_of(web_app, client)
    names = doubles.names(ws)
    start = names.index("send_message") - 1
    assert names[start:] == ["live.sync", "send_message", "live.sync", "refresh", "app_state"]
    records = [item for item in doubles.records if item.ws is ws][start:]
    assert {(item.locale, item.audience) for item in records} == {("en", "customer")}
    assert all(item.thread.startswith("web-main") for item in records)
    assert records[1].args == ("pay mum", 1, "m-000001")


def test_results_carry_only_a_status(web_app, client):
    client.open()
    data = client.json(client.post("/api/session/login", {}))
    assert data["result"] == {"status": "settled"}
    assert "internal-id-never-shown" not in str(data)


def test_tab_posts_answer_state_and_tab_at_one_revision(web_app, client, doubles):
    app_settings = web_app.settings
    app_settings["features"] = frozenset({"tab:wealth"})
    client.open()
    data = client.json(client.post("/api/wealth/compare", {"objective": "total_fee"}))
    assert data["tab"]["name"] == "wealth"
    assert data["tab"]["revision"] == data["revision"] == data["state"]["revision"] == 1


def test_closed_features_answer_404_with_state_and_call_nothing(web_app, client, doubles):
    # A build that opens only the core shortcuts (the shipped FEATURES opens ext items).
    web_app.settings["features"] = frozenset({
        "shortcut:transfer",
        "shortcut:cards",
        "shortcut:scam_drill",
    })
    client.open()
    ws = ws_of(web_app, client)
    opened = len(doubles.names(ws))
    for path, body in (
        ("/api/chat/shortcut", {"shortcut": "billing", "client_msg_id": "m-000001"}),
        ("/api/chat/compensate", {"plan_version": "0" * 32}),
        ("/api/wealth/compare", {"objective": "net_return"}),
        ("/api/subscriptions/query", {}),
        ("/api/live/toggle", {"on": True}),
        ("/api/device/tasks/TR1/nodes", {"card_version": "0" * 32, "nodes": ["n0"]}),
        ("/api/chat/responses/R1/birthday", {"choice": "c0"}),
        ("/api/chat/responses/R1/aa", {"choices": ["c0"]}),
    ):
        response = client.post(path, body)
        data = client.json(response)
        assert response.code == 404 and data["error"]["code"] == "not_found", path
        assert data["state"]["generation"] == client.generation
    for path in ("/api/tabs/accounts", "/api/live"):
        assert client.get(path).code == 404
    assert client.get("/vendor/corridor-hmac.js").code == 404
    service = {"send_shortcut", "compensate_latest", "wealth_compare", "subscriptions_query"}
    called = set(doubles.names(ws)[opened:])
    assert not (service | {"live.toggle", "live.sync", "refresh"}) & called


def test_open_shortcuts_reach_the_service(web_app, client, doubles):
    client.open()
    response = client.post(
        "/api/chat/shortcut", {"shortcut": "transfer", "client_msg_id": "m-01234567"}
    )
    assert response.code == 200 and client.json(response)["rkey"] == "R1"
    assert ("transfer", "m-01234567") in [
        r.args for r in doubles.records if r.name == "send_shortcut"
    ]


def test_unknown_path_keys_answer_404(web_app, client, doubles):
    client.open()
    for path in (
        "/api/chat/responses/X1/current",
        "/api/chat/responses/R0/current",
        "/api/chat/responses/R1234567/current",
        "/api/device/tasks/TQ1/cancel",
        "/api/device/tasks/R1/cancel",
    ):
        response = client.post(path, {})
        assert response.code == 404, path
        assert client.json(response)["error"]["code"] == "not_found"
    assert client.get("/api/tabs/settings").code == 404
    assert client.get("/api/nothing-here").code == 404
    assert not {"make_current", "device_cancel"} & set(doubles.names())


def test_capacity_evicts_the_least_recently_used_idle_session(web_settings, doubles):
    with serve({**web_settings, "max_sessions": 2}) as app:
        first, second, third = WebClient(app), WebClient(app), WebClient(app)
        first.open()
        second.open()
        evicted = ws_of(app, first)
        first.get("/api/state")  # first is now the most recently used
        third.open()
        assert ws_of(app, second) is None and ws_of(app, first) is not None
        response = second.post("/api/chat/stop", {})
        assert response.code == 401 and second.json(response)["error"]["code"] == "no_session"
        assert evicted is ws_of(app, first)
        closed = [record.ws for record in doubles.records if record.name == "live.close"]
        assert len(closed) == 1 and not app.settings["store"].pairs.allowed(closed[0].pair)


def test_a_browser_keeps_at_most_four_tabs(web_settings, doubles):
    with serve({**web_settings, "max_sessions": 8}) as app:
        tabs = [WebClient(app) for _ in range(5)]
        tabs[0].open()
        for tab in tabs[1:]:
            tab.browser = tabs[0].browser
            tab.open()
        assert len(app.settings["store"]) == 4
        assert ws_of(app, tabs[0]) is None
        assert all(ws_of(app, tab) is not None for tab in tabs[1:])


# --- the same shell over the real controllers -----------------------------------------------

YELLOW = "给妈妈转500元"


@pytest.fixture
def real_app(st_sentinel):
    with serve({}) as app:
        yield app


def test_reads_leave_the_real_controllers_untouched(real_app):
    from tests.unit.test_ui_scam_friction import snapshot

    page = WebClient(real_app)
    page.open(view="judge")
    page.post("/api/session/login", {})
    page.post(
        "/api/chat/message", {"text": YELLOW, "external_files": 0, "client_msg_id": "m-real-0001"}
    )
    ws = ws_of(real_app, page)

    def observed():
        return (
            snapshot(ws.controller, ws.session),
            dict(ws.gestures),
            dict(ws.gates),
            dict(ws.gate_eval),
            ws.revision,
        )

    before = observed()
    assert before[1], "the yellow transfer offers a device gesture"
    for path in ("/api/state", "/api/i18n", "/api/tabs/operator", "/api/evidence/responses/R1"):
        for _ in range(3):
            assert page.get(path).code == 200, path
    assert observed() == before


def test_a_real_reset_starts_a_new_signed_out_session_in_the_same_tab(real_app):
    page = WebClient(real_app)
    opened = page.open(view="customer", locale="en")
    page.post("/api/session/login", {})
    old = ws_of(real_app, page)
    data = page.json(page.post("/api/session/reset", {}))
    new = ws_of(real_app, page)
    assert new is not old and page.tab == opened["tab"]
    assert new.controller is not old.controller and new.session is not old.session
    assert new.pair != old.pair and new.secret != old.secret
    assert (data["state"]["view"], data["state"]["locale"]) == ("customer", "en")
    assert "login" in data["state"] and data["revision"] == 0


# --- capabilities open exactly their entry points (contract §5.2) ---------------------------

GESTURE = {"gesture": "g" * 22, "card_version": "0" * 32}
GATED = [
    (
        "/api/chat/shortcut",
        {"shortcut": "birthday", "client_msg_id": "m-000001"},
        {"shortcut:birthday"},
    ),
    ("/api/chat/responses/R1/birthday", {"choice": "c0"}, {"plan_select"}),
    ("/api/device/tasks/TR1/nodes", {"card_version": "0" * 32, "nodes": ["n0"]}, {"plan_select"}),
    ("/api/chat/compensate", {"plan_version": "0" * 32}, {"compensate"}),
    ("/api/chat/responses/R1/aa", {"choices": ["c0", "c1"]}, {"aa"}),
    ("/api/subscriptions/query", {}, {"tab:accounts", "shortcut:subscriptions"}),
    (
        "/api/subscriptions/cancel",
        {"choice": "c0", "list_version": "0" * 32},
        {"tab:accounts", "shortcut:subscriptions"},
    ),
    ("/api/billing/analyze", {"period": "2026-09"}, {"tab:accounts", "shortcut:billing"}),
    ("/api/wealth/compare", {"objective": "liquidity"}, {"tab:wealth"}),
    ("/api/wealth/purchase", {"choice": None, "list_version": "0" * 32}, {"tab:wealth"}),
    ("/api/wealth/redeem", {"choice": "c0", "list_version": "0" * 32}, {"tab:wealth"}),
    ("/api/presets/suitability-r4", {}, {"tab:wealth"}),
    ("/api/live/challenge", {}, {"live"}),
    ("/api/live/unlock", {"nonce": "0" * 32, "proof": "0" * 64}, {"live"}),
    ("/api/live/select", {"deployment": "openai-gpt-6.1-sol"}, {"live"}),
    ("/api/live/toggle", {"on": False}, {"live"}),
]


@pytest.mark.parametrize(("path", "body", "needs"), GATED, ids=[item[0] for item in GATED])
def test_each_ext_entry_point_opens_with_exactly_its_capabilities(
    web_app, client, path, body, needs
):
    client.open()
    for missing in needs:
        web_app.settings["features"] = frozenset(needs - {missing})
        assert client.post(path, body).code == 404, missing
    web_app.settings["features"] = frozenset(needs)
    response = client.post(path, body)
    assert response.code == 200, client.json(response)


@pytest.mark.parametrize(
    ("name", "needs"),
    [("accounts", {"tab:accounts"}), ("wealth", {"tab:wealth"}), ("plans", {"tab:plans"})],
)
def test_tabs_open_with_their_capability_and_the_operator_tab_always(web_app, client, name, needs):
    client.open()
    web_app.settings["features"] = frozenset()
    assert client.get(f"/api/tabs/{name}").code == 404
    assert client.get("/api/tabs/operator").code == 200
    web_app.settings["features"] = frozenset(needs)
    data = client.json(client.get(f"/api/tabs/{name}"))
    assert data["tab"]["name"] == name and data["tab"]["revision"] == data["revision"]
    assert "state" not in data


def test_the_live_panel_and_its_script_open_with_the_live_capability(web_app, client):
    client.open()
    web_app.settings["features"] = frozenset({"live"})
    assert client.json(client.get("/api/live"))["live"]["enabled"] is False
    script = client.get("/vendor/corridor-hmac.js")
    assert script.code == 200 and script.headers["Content-Type"].startswith("text/javascript")
    assert b"corridor-live-access-v1" in script.body
    data = client.json(client.post("/api/live/unlock", {"nonce": "0" * 32, "proof": "0" * 64}))
    assert data["result"] == "invalid" and data["live"]["enabled"] is False
    assert client.json(client.post("/api/live/challenge", {}))["nonce"] == "0" * 32


def test_without_features_the_package_default_is_read_on_each_request(web_app, client, monkeypatch):
    import corridor.web

    client.open()
    web_app.settings["features"] = None
    body = {"shortcut": "billing", "client_msg_id": "m-000001"}
    monkeypatch.setattr(corridor.web, "FEATURES", corridor.web.FEATURES - {"shortcut:billing"})
    assert client.post("/api/chat/shortcut", body).code == 404
    monkeypatch.setattr(corridor.web, "FEATURES", corridor.web.FEATURES | {"shortcut:billing"})
    assert client.post("/api/chat/shortcut", body).code == 200


def test_each_request_tells_the_view_models_which_capabilities_are_open(web_app, client):
    import corridor.web

    client.open()
    ws = ws_of(web_app, client)
    assert ws.features == corridor.web.FEATURES
    web_app.settings["features"] = frozenset({"tab:plans"})
    client.get("/api/state")
    assert ws.features == frozenset({"tab:plans"})
    web_app.settings["features"] = frozenset({"aa"})
    client.post("/api/chat/stop", {})
    assert ws.features == frozenset({"aa"})


def test_a_new_session_renders_with_the_apps_capabilities(web_settings, doubles):
    seen = []
    original = doubles.vm.app.app_state
    doubles.vm.app.app_state = lambda ws: seen.append(ws.features) or original(ws)
    with serve({**web_settings, "features": {"live"}}) as app:
        WebClient(app).open()
    assert seen == [frozenset({"live"})]
