"""Public mode's live gate, over the wire and against a canary model endpoint (RW-8 items 6,
8, 10, 11, 14; design §4.7, §4.8, §4.11).

The server holds a PBKDF2 verifier, never a code: ``access_setup`` draws the code and
prints the verifier pair, the challenge carries the derivation, the browser derives the
verifier (``static/js/kdf.js``, checked here against ``hashlib`` through node) and proves
it with the existing single-use HMAC. A public site with the live switch on refuses to
start without a whole, well-formed pair and the public spend caps; at run time the same
gate closes the live path (no challenge, grant, switch or client) on a half pair, a plain
code, a deployment other than the site's, caps beyond the bounds or a lost ledger. The
ledger's shared wrong-code lock is off in public mode and on outside it. A public site never
starts a ledger: the journal directory is fixed and a lost ledger closes live access until
``ledger_setup`` carries the reconciled total over (R8 S2).

A3: a canary endpoint on the loopback receives the frozen client's real outbound request
(the deployment's base URL is pointed at it; the scripted planner's own answer is wrapped
in the wire format) and the tests read what left the process, what the journal keeps and
what a failure and a retry look like; the site notice is worded from those readings. The
chat reaches four steps: ``intent`` for a message, ``extract`` and ``plan`` for a transfer,
``suggest`` for a message the local rules cannot read (RW-9 item 2).
"""

import http.server
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

import corridor.web
from corridor.ui import live_config, live_runtime
from corridor.ui.live_config import DEPLOYMENTS, LiveConfig, access_code_strong
from corridor.ui.live_runtime import JournalClient, LiveSession, access_proof
from corridor.web import compat
from corridor.web import live as web_live
from corridor.web import public as public_mode
from corridor.web.public import Kdf, PublicConfig
from corridor.web.security import HOST_COOKIE
from tests.unit.test_ui_live_review3 import reachable_text
from tests.web.conftest import WebClient, serve, ws_of

LIVE = corridor.web.FEATURES | {"live"}
HOST = "demo.example"
ADDRESS = "203.0.113.5"
DEEPSEEK = "deepseek-flash"
# A synthetic key, code and salt for tests only; the verifier is derived from them.
FAKE_KEY = "sk-test-only-public-live-key-0123456789abcdef"
CODE = "test-only-public-code-Qx7Rm9Vk2Lp4"
PLAIN_OLD_CODE = "judge-pass-2026-Kx7Qm9Rv"
KDF = Kdf(salt="00112233445566778899aabbccddeeff")
VERIFIER = KDF.verifier(CODE)
CANARY_ERROR = "CANARY-PROVIDER-FAILURE-0x7f3a"
BEFORE = "给妈妈转500元"
BALANCE = "查一下余额"
VAGUE = "帮我看看妈妈那边的事情怎么弄"
# The synthetic address book, by handle, as the extract step sends it (RW-9 item 2).
ADDRESS_BOOK = {
    "CONTACT_CHEN": ["NAME_CHEN", "PHONE_CHEN", "roommate", "室友", "妈妈", "老妈", "我妈", "MOM"],
    "CONTACT_LANDLORD": ["NAME_LANDLORD", "PHONE_LANDLORD", "房东", "LANDLORD"],
    "CONTACT_LIN_A": ["NAME_LIN", "PHONE_LIN_A", "classmate", "同学", "LIN_A"],
    "CONTACT_LIN_B": ["NAME_LIN", "PHONE_LIN_B", "neighbor", "邻居", "LIN_B"],
    "CONTACT_NEW": ["NAME_NEW", "PHONE_NEW", "新联系人", "NEW_PAYEE"],
    "CONTACT_SPOUSE": ["NAME_SPOUSE", "PHONE_SPOUSE", "爱人", "老婆", "SPOUSE"],
}
KDF_JS = Path(corridor.web.__file__).with_name("static") / "js" / "kdf.js"
ACTIONS_JS = KDF_JS.with_name("actions.js")
# What the frozen client's request may carry as headers (A3, measured against the canary):
# the host and content headers, the key and the OpenAI SDK's runtime telemetry.
HEADER_FAMILIES = (
    "host",
    "accept",
    "connection",
    "authorization",
    "content-",
    "user-agent",
    "x-stainless-",
)


def namespace(**values):
    base = {name: None for name in ("public_host", "max_sessions", *public_mode.PUBLIC_ONLY)}
    base.update(values)
    return SimpleNamespace(**base)


def live_environ(tmp_path, *, files=(".env.deepseek",), **extra):
    """A public live configuration: the deployment's credential file (a synthetic key, no
    code inside), one fixed journal directory, the public caps and the verifier pair."""
    creds = tmp_path / "creds"
    creds.mkdir(exist_ok=True)
    for name in files:
        (creds / name).write_text(
            f"# synthetic credential file for tests; never a real key\nLLM_API_KEY={FAKE_KEY}\n",
            encoding="utf-8",
        )
    values = {
        live_config.SWITCH: "1",
        live_config.ENV_DIR: str(creds),
        live_config.JOURNAL_DIR: str(tmp_path / "journal"),
        live_config.SESSION_CALLS: "20",
        live_config.BUDGET_CNY: "100",
        public_mode.KDF_ENV: KDF.text(),
        public_mode.VERIFIER_ENV: VERIFIER,
    }
    for name, value in extra.items():
        if value is None:
            values.pop(name, None)
        else:
            values[name] = value
    return values


def browser(app, address=ADDRESS, view="judge"):
    client = Recorded(
        app, host=HOST, scheme="https", cookie_name=HOST_COOKIE, forwarded_for=address
    )
    client.open(view=view)
    assert client.post("/api/session/login", {}).code == 200
    return client


class Recorded(WebClient):
    """A browser that keeps every response body it received."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bodies: list[str] = []

    def request(self, method, path, body, lines, *, read=True):
        response = super().request(method, path, body, lines, read=read)
        if response is not None:
            self.bodies.append(response.body.decode("utf-8", "replace"))
        return response


def data(client, response):
    return client.json(response)


def code_of(client, response):
    return data(client, response)["error"]["code"]


def challenge(client):
    response = client.post("/api/live/challenge", {})
    return response, data(client, response)


def unlock(client, secret=VERIFIER):
    """The browser's unlock: the proof of the verifier (or of what is given) for a fresh
    challenge; outside the kdf the page would prove the code itself."""
    _, answer = challenge(client)
    nonce = answer["nonce"]
    response = client.post(
        "/api/live/unlock", {"nonce": nonce, "proof": access_proof(secret, nonce)}
    )
    return response, data(client, response), nonce


def go_live(client):
    response, answer, _ = unlock(client)
    assert response.code == 200 and answer["result"] == "accepted", answer
    response = client.post("/api/live/toggle", {"on": True})
    assert response.code == 200, data(client, response)
    return data(client, response)


def send(client, text):
    body = {"text": text, "external_files": 0, "client_msg_id": secrets.token_hex(8)}
    return client.post("/api/chat/message", body)


def panel(client):
    return data(client, client.get("/api/live"))["live"]


def confirm(client):
    """The device's confirm of the task the page shows."""
    state = data(client, client.get("/api/state"))["state"]
    task = next(item for item in state["device"]["tasks"] if item["gesture"])
    gesture = task["gesture"]
    body = {"gesture": gesture["token"], "card_version": gesture["card_version"]}
    return client.post(f"/api/device/tasks/{task['key']}/confirm", body)


def sent(request):
    """What one request gave the model: the user message's ``prompt`` and ``tool_state``."""
    user = json.loads(request["body"]["messages"][1]["content"])
    assert set(user) == {"prompt", "tool_state"}
    return user["prompt"], user["tool_state"]


def steps_of(journal):
    """The step family of every attempt, in order (``plan:<id>:0`` is ``plan``)."""
    return [
        json.loads(line)["step"].split(":")[0]
        for session in sorted(journal.glob(f"{DEEPSEEK}/session-*"))
        for line in (session / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
    ]


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture
def web_settings(monkeypatch):
    # The routes read the app's features and the view models read the package's: both open.
    monkeypatch.setattr(corridor.web, "FEATURES", LIVE)
    return {"features": LIVE}


@pytest.fixture(autouse=True)
def fixed_journal(monkeypatch, tmp_path):
    """The one directory a public site accepts, moved under this test's root; a module
    constant on purpose, so no setting on the machine can move it."""
    monkeypatch.setattr(public_mode, "JOURNAL_PATH", tmp_path / "journal")


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    """Set the public live environment (``live_environ`` plus overrides) on the process,
    with the ledger ``enable_live.sh`` would have started."""

    def apply(**extra):
        for name in list(live_environ(tmp_path)) + list(extra):
            monkeypatch.delenv(name, raising=False)
        for name, value in live_environ(tmp_path, **extra).items():
            monkeypatch.setenv(name, value)
        journal = tmp_path / "journal"
        live_runtime.SpendLedger(journal).initialize("CNY", DEEPSEEK, Fraction(0))
        return journal

    return apply


@pytest.fixture
def public_app(web_settings, st_sentinel):
    with serve({
        **web_settings,
        "public": PublicConfig(host=HOST, kdf=KDF),
        "clock": lambda: 0.0,
    }) as app:
        yield app


class Canary(http.server.ThreadingHTTPServer):
    """A fake model endpoint on the loopback: keeps every request whole (method, path,
    headers, body) and answers in the provider's wire format with the scripted planner's
    own output for the step (stashed by the ``complete`` wrapper), or a failure."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), CanaryHandler)
        self.requests: list[dict] = []
        self.plan: list[str] = []
        self.stash: dict = {}
        self.lock = threading.Lock()

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def answer(self) -> tuple[int, dict]:
        with self.lock:
            behaviour = self.plan.pop(0) if self.plan else "ok"
            scripted = self.stash.get("scripted")
        if behaviour == "fail":
            return 500, {"error": {"message": CANARY_ERROR, "type": "server_error"}}
        output = scripted()
        return 200, {
            "id": "chatcmpl-canary",
            "object": "chat.completion",
            "created": 0,
            "model": DEEPSEEK,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_canary",
                                "type": "function",
                                "function": {
                                    "name": "emit_result",
                                    "arguments": json.dumps(output, ensure_ascii=False),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 180, "completion_tokens": 20, "total_tokens": 200},
        }


class CanaryHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - the handler's own naming
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        record = {
            "method": self.command,
            "path": self.path,
            "headers": {name: value for name, value in self.headers.items()},
            "raw": raw,
            "body": json.loads(raw.decode("utf-8")),
        }
        with self.server.lock:
            self.server.requests.append(record)
        status, payload = self.server.answer()
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - the base class's signature
        return


@pytest.fixture
def canary(monkeypatch):
    """The canary endpoint, with the deployment table pointed at it and the ``complete``
    wrapper that stashes each call's scripted answer for the endpoint to return."""
    server = Canary()
    thread = threading.Thread(target=server.serve_forever, name="canary", daemon=True)
    thread.start()
    item = replace(live_config.deployment(DEEPSEEK), api_base=server.api_base)
    table = tuple(item if entry.id == DEEPSEEK else entry for entry in DEPLOYMENTS)
    monkeypatch.setitem(live_config._BY_ID, DEEPSEEK, item)
    for module in (live_config, live_runtime, compat, web_live):
        monkeypatch.setattr(module, "DEPLOYMENTS", table)
    original = JournalClient.complete

    def complete(client, **kwargs):
        with server.lock:
            server.stash["scripted"] = kwargs["scripted"]
            server.stash["step"] = kwargs["step"]
        return original(client, **kwargs)

    monkeypatch.setattr(JournalClient, "complete", complete)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


# --- the verifier pair at start-up (RW-8 item 11) -----------------------------------------------


def test_the_kdf_text_round_trips_and_the_verifier_is_pbkdf2_sha256():
    assert Kdf.parse(KDF.text()) == KDF
    assert KDF.wire() == {"algorithm": "pbkdf2-sha256", "iterations": 200_000, "salt": KDF.salt}
    import hashlib

    expected = hashlib.pbkdf2_hmac("sha256", CODE.encode(), bytes.fromhex(KDF.salt), 200_000)
    assert VERIFIER == expected.hex() and re.fullmatch(r"[0-9a-f]{64}", VERIFIER)
    assert KDF.verifier("another code") != VERIFIER


@pytest.mark.parametrize(
    "text",
    [
        "pbkdf2-sha512$200000$" + KDF.salt,
        "pbkdf2-sha256$abc$" + KDF.salt,
        "pbkdf2-sha256$99999$" + KDF.salt,
        "pbkdf2-sha256$2000001$" + KDF.salt,
        "pbkdf2-sha256$200000$" + KDF.salt[:-2],
        "pbkdf2-sha256$200000$" + KDF.salt.upper(),
        "pbkdf2-sha256$200000",
        "",
    ],
)
def test_a_malformed_kdf_is_refused(text):
    with pytest.raises(ValueError):
        Kdf.parse(text)


@pytest.mark.parametrize(
    "overrides",
    [
        {public_mode.KDF_ENV: None, public_mode.VERIFIER_ENV: None},
        {public_mode.KDF_ENV: None},
        {public_mode.VERIFIER_ENV: None},
        {public_mode.VERIFIER_ENV: PLAIN_OLD_CODE},
        {public_mode.VERIFIER_ENV: VERIFIER.upper()},
        {public_mode.VERIFIER_ENV: VERIFIER[:-1]},
        {public_mode.KDF_ENV: "pbkdf2-sha256$1000$" + KDF.salt},
        {live_config.JOURNAL_DIR: None},
        {live_config.SESSION_CALLS: "21"},
        {live_config.SESSION_CALLS: None},
        {live_config.SESSION_CALLS: "0"},
        {live_config.BUDGET_CNY: "101"},
        {live_config.BUDGET_CNY: "0"},
        {live_config.BUDGET_CNY: "ten"},
    ],
)
def test_a_public_site_with_the_switch_on_does_not_start_without_the_pair_and_the_caps(
    tmp_path, overrides
):
    environ = live_environ(tmp_path, **overrides)
    environ[public_mode.HOST_ENV] = HOST
    with pytest.raises(ValueError):
        PublicConfig.from_args(namespace(), environ)
    with pytest.raises(ValueError):
        PublicConfig.from_args(namespace(public_host=HOST), environ)


def test_a_public_site_with_the_switch_on_starts_with_the_pair_and_the_caps(tmp_path):
    environ = live_environ(tmp_path)
    config = PublicConfig.from_args(namespace(public_host=HOST), environ)
    assert config is not None and config.kdf == KDF
    # The budget may be lower than the bound, never higher.
    environ[live_config.BUDGET_CNY], environ[live_config.SESSION_CALLS] = "30", "5"
    assert PublicConfig.from_args(namespace(public_host=HOST), environ).kdf == KDF


def test_with_the_switch_off_the_pair_is_optional_but_whole(tmp_path):
    environ = live_environ(tmp_path, **{live_config.SWITCH: None})
    assert PublicConfig.from_args(namespace(public_host=HOST), environ).kdf == KDF
    neither = live_environ(
        tmp_path,
        **{live_config.SWITCH: None, public_mode.KDF_ENV: None, public_mode.VERIFIER_ENV: None},
    )
    assert PublicConfig.from_args(namespace(public_host=HOST), neither).kdf is None
    for half in (
        {public_mode.KDF_ENV: None},
        {public_mode.VERIFIER_ENV: None},
        {public_mode.VERIFIER_ENV: PLAIN_OLD_CODE},
    ):
        with pytest.raises(ValueError):
            PublicConfig.from_args(
                namespace(public_host=HOST),
                live_environ(tmp_path, **{live_config.SWITCH: None, **half}),
            )
    # Outside public mode none of it is read.
    assert (
        PublicConfig.from_args(namespace(), live_environ(tmp_path, **{live_config.SWITCH: None}))
        is None
    )


def test_the_entry_point_refuses_a_public_live_site_without_the_pair(monkeypatch, tmp_path):
    from corridor.web.__main__ import main

    for name, value in live_environ(tmp_path, **{public_mode.KDF_ENV: None}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(public_mode.KDF_ENV, raising=False)
    monkeypatch.delenv(public_mode.DEPLOYMENTS_ENV, raising=False)
    with pytest.raises(SystemExit) as refused:
        main(["--public-host", HOST, "--port", "0"])
    assert refused.value.code == 2


def test_access_configured_reads_the_pair_and_drops_it(tmp_path):
    assert public_mode.access_configured(live_environ(tmp_path)) is True
    for broken in (
        {public_mode.KDF_ENV: None},
        {public_mode.VERIFIER_ENV: PLAIN_OLD_CODE},
        {public_mode.KDF_ENV: None, public_mode.VERIFIER_ENV: None},
    ):
        assert public_mode.access_configured(live_environ(tmp_path, **broken)) is False
    assert public_mode.public_caps(LiveConfig.from_environ(live_environ(tmp_path))) is None
    for over in ({live_config.SESSION_CALLS: "40"}, {live_config.BUDGET_CNY: "100.5"}):
        config = LiveConfig.from_environ(live_environ(tmp_path, **over))
        assert public_mode.public_caps(config) is not None


# --- access_setup (RW-8 item 8) ------------------------------------------------------------------


def test_access_setup_draws_the_code_once_and_prints_only_the_verifier_pair(capsys):
    from corridor.web import access_setup

    assert access_setup.CODE_BYTES * 8 >= 128
    assert access_setup.main([]) == 0
    out, err = capsys.readouterr()
    kdf_line, verifier_line = out.splitlines()
    assert out.endswith("\n") and out.count("\n") == 2
    assert kdf_line.startswith(public_mode.KDF_ENV + "=")
    assert verifier_line.startswith(public_mode.VERIFIER_ENV + "=")
    kdf = Kdf.parse(kdf_line.split("=", 1)[1])
    assert kdf.iterations == public_mode.KDF_ITERATIONS and len(kdf.salt) == 32
    code = err.splitlines()[-1]
    assert access_code_strong(code) and len(code) == 32 and re.fullmatch(r"[A-Za-z0-9_-]+", code)
    assert kdf.verifier(code) == verifier_line.split("=", 1)[1]
    assert code not in out
    # A second run draws a new code, salt and verifier.
    assert access_setup.main([]) == 0
    again, err_again = capsys.readouterr()
    assert again != out and err_again.splitlines()[-1] != code


def test_access_setup_takes_no_code_from_anyone(capsys):
    from corridor.web import access_setup

    for argv in (["--code", CODE], [CODE], ["--salt", KDF.salt]):
        with pytest.raises(SystemExit) as refused:
            access_setup.main(argv)
        assert refused.value.code == 2
    out, _ = capsys.readouterr()
    assert out == ""


def test_access_setup_runs_as_a_module_and_keeps_the_code_off_standard_output():
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("CORRIDOR_LIVE", "LLM_", "LIVE_", "CORRIDOR_WEB"))
    }
    process = subprocess.run(
        [sys.executable, "-m", "corridor.web.access_setup"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    kdf_line, verifier_line = process.stdout.splitlines()
    code = process.stderr.strip().splitlines()[-1]
    kdf = Kdf.parse(kdf_line.split("=", 1)[1])
    assert kdf.verifier(code) == verifier_line.split("=", 1)[1]
    assert code not in process.stdout
    assert (
        PublicConfig.from_args(
            namespace(public_host=HOST),
            {
                public_mode.KDF_ENV: kdf_line.split("=", 1)[1],
                public_mode.VERIFIER_ENV: verifier_line.split("=", 1)[1],
            },
        ).kdf
        == kdf
    )


# --- the browser's derivation (design §4.7) ------------------------------------------


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is needed to run the page's WebCrypto derivation")
@pytest.mark.parametrize("code", [CODE, "口令-with-ünïcode-✓", "x"])
def test_the_pages_webcrypto_derivation_matches_hashlib(code, tmp_path):
    script = tmp_path / "derive.mjs"
    script.write_text(
        "import { deriveVerifier, kdfParams } from " + json.dumps(KDF_JS.resolve().as_uri()) + ";\n"
        "const kdf = JSON.parse(process.env.KDF_WIRE);\n"
        "const code = process.env.KDF_CODE;\n"
        "const bad = [kdfParams({ ...kdf, algorithm: 'pbkdf2-sha512' }), kdfParams({ ...kdf, "
        "iterations: 1000 }), kdfParams({ ...kdf, salt: kdf.salt.slice(1) }), kdfParams(null)];\n"
        "const verifier = await deriveVerifier(code, kdf);\n"
        "process.stdout.write(JSON.stringify({ verifier, bad }));\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [NODE, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "KDF_WIRE": json.dumps(KDF.wire()), "KDF_CODE": code},
        timeout=120,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["verifier"] == KDF.verifier(code)
    assert result["bad"] == [None, None, None, None]


def test_the_unlock_action_derives_the_verifier_only_for_a_challenge_with_a_kdf():
    text = ACTIONS_JS.read_text(encoding="utf-8")
    assert 'import { deriveVerifier } from "./kdf.js";' in text
    unlock_source = text[text.index("export async function liveUnlock") :]
    unlock_source = unlock_source[: unlock_source.index("\n}\n") + 3]
    assert "challenge.data.kdf !== undefined" in unlock_source
    assert "await deriveVerifier(secret, challenge.data.kdf)" in unlock_source
    # Without a derived verifier nothing is sent (fail closed); without a kdf the code itself.
    assert 'if (typeof secret !== "string") return false;' in unlock_source
    assert "lib.proof(secret, nonce)" in unlock_source
    kdf_source = KDF_JS.read_text(encoding="utf-8")
    assert '"PBKDF2"' in kdf_source and 'hash: "SHA-256"' in kdf_source and "256,\n" in kdf_source


# --- the shared wrong-code lock (RW-8 item 8) ----------------------------------------


def test_the_shared_lock_is_off_only_when_asked_and_wrong_codes_still_reach_the_ledger(tmp_path):
    # The sessions read this mapping, not the process environment (which the autouse
    # fixture has cleared of every live and client variable).
    values = live_environ(tmp_path, **{public_mode.KDF_ENV: None})
    config = LiveConfig.from_environ(values)
    for _ in range(4):
        attacker = LiveSession(config, environ=values)
        for attempt in range(live_runtime.LOCK_AFTER_FAILURES):
            assert attacker.authorize(f"wrong-{attempt}") == "invalid"
    ledger = live_runtime.SpendLedger(config.journal_dir)
    assert ledger.access_failures() == live_runtime.GLOBAL_LOCK_AFTER
    locked = LiveSession(config, environ=values)
    assert locked.authorize(VERIFIER) == "locked_all"
    assert locked.snapshot()["locked_all"] is True and locked.snapshot()["reason"] == "locked_all"
    unlocked = LiveSession(config, environ=values, global_lock=False)
    snapshot = unlocked.snapshot()
    assert snapshot["locked_all"] is False and snapshot["reason"] == "access_required"
    assert unlocked.authorize("still-wrong") == "invalid"
    assert ledger.access_failures() == live_runtime.GLOBAL_LOCK_AFTER + 1
    assert unlocked.authorize(VERIFIER) == "accepted"
    # The per-session lock stays.
    other = LiveSession(config, environ=values, global_lock=False)
    for attempt in range(live_runtime.LOCK_AFTER_FAILURES):
        assert other.authorize(f"wrong-{attempt}") == "invalid"
    assert other.authorize(VERIFIER) == "locked"


def test_twenty_wrong_codes_from_other_addresses_lock_nobody_else_in_public_mode(
    public_app, hosted
):
    """C4: four addresses each burn their five attempts (twenty wrong codes on the one
    ledger); the address bucket then answers before the session lock could, and a fifth
    address still unlocks: no shared lock in public mode."""
    hosted()
    for index in range(4):
        client = browser(public_app, address=f"203.0.113.{10 + index}")
        results = [unlock(client, f"wrong-{attempt}")[1]["result"] for attempt in range(5)]
        assert results == ["invalid"] * 5
        response = client.post("/api/live/unlock", {"nonce": "0" * 32, "proof": "0" * 64})
        assert response.code == 503 and code_of(client, response) == "busy"
        assert response.headers["Retry-After"] == public_mode.RETRY_AFTER
        assert ws_of(public_app, client).live.snapshot()["reason"] == "locked"
    ledger = live_runtime.SpendLedger(Path(os.environ[live_config.JOURNAL_DIR]))
    assert ledger.access_failures() == live_runtime.GLOBAL_LOCK_AFTER
    late = browser(public_app, address="198.51.100.9")
    response, answer, _ = unlock(late)
    assert response.code == 200 and answer["result"] == "accepted", answer
    assert ws_of(public_app, late).live.snapshot()["locked_all"] is False


# --- the gate at run time (RW-8 items 6, 10, 11) -------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {public_mode.KDF_ENV: None},
        {public_mode.VERIFIER_ENV: PLAIN_OLD_CODE},
        {public_mode.KDF_ENV: None, public_mode.VERIFIER_ENV: PLAIN_OLD_CODE},
        {public_mode.KDF_ENV: None, public_mode.VERIFIER_ENV: None},
        {live_config.SESSION_CALLS: "40"},
        {live_config.BUDGET_CNY: "500"},
    ],
)
def test_a_broken_pair_or_wide_caps_close_the_live_path_and_no_call_leaves(
    public_app, hosted, canary, overrides
):
    hosted(**overrides)
    for view in ("judge", "customer"):
        client = browser(public_app, view=view)
        shown = panel(client)
        assert shown["enabled"] is True
        assert shown["unlock"]["disabled"] is True and shown["toggle"]["disabled"] is True
        assert shown["reason_text"], shown
        for path, body in (
            ("/api/live/challenge", {}),
            ("/api/live/unlock", {"nonce": "0" * 32, "proof": "0" * 64}),
            ("/api/live/toggle", {"on": True}),
            ("/api/live/select", {"deployment": DEEPSEEK}),
        ):
            response = client.post(path, body)
            assert response.code == 409 and code_of(client, response) == "live_unavailable", path
        # No challenge was issued, so no proof of the old plain code can be tried either.
        assert send(client, BALANCE).code == 200
        ws = ws_of(public_app, client)
        assert not isinstance(ws.controller.client, JournalClient)
        assert ws.live is None or ws.live.snapshot()["authorized"] is False
    assert canary.requests == []
    assert not list(Path(hosted()).glob("*/session-*"))


def test_only_credentialed_public_deployments_are_listed_and_selectable(public_app, hosted, canary):
    # A stray OpenAI credential file next to the DeepSeek one: the product's default would
    # fall on the first deployment of its table.
    hosted()
    creds = Path(os.environ[live_config.ENV_DIR])
    (creds / ".env").write_text(f"LLM_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    client = browser(public_app)
    shown = panel(client)
    assert [item["id"] for item in shown["deployments"]] == [DEEPSEEK, "deepseek-v4-pro"]
    assert shown["selected"] == DEEPSEEK
    # The product's own default on this credential directory is the first OpenAI entry.
    assert LiveSession(LiveConfig.from_environ()).deployment.id == DEPLOYMENTS[0].id
    response = client.post("/api/live/select", {"deployment": "openai-gpt-6.1-sol"})
    assert response.code == 422 and code_of(client, response) == "invalid_input"
    ws = ws_of(public_app, client)
    assert ws.live is None  # refused before the action: no live session was even built
    response = client.post("/api/live/select", {"deployment": DEEPSEEK})
    assert response.code == 200 and data(client, response)["live"]["selected"] == DEEPSEEK
    assert ws.live.deployment.id == DEEPSEEK
    # The product call itself refuses too, should a selection ever reach it.
    with pytest.raises(Exception) as refused:
        web_live.select(ws, "openai-gpt-6.1-sol")
    assert getattr(refused.value, "code", None) == "invalid_input"
    assert ws.live.deployment.id == DEEPSEEK
    go_live(client)
    assert send(client, BALANCE).code == 200
    assert ws_of(public_app, client).controller.client.deployment.id == DEEPSEEK
    assert len(canary.requests) == 1 and canary.requests[0]["body"]["model"] == DEEPSEEK


# --- A3: the outbound request and the journal (RW-8 item 14) ----------------------------


def test_all_six_site_models_share_the_grant_caps_and_selected_disclosure(
    public_app, hosted, monkeypatch
):
    from corridor.agent.llm import LLMClient

    def forbidden(*args, **kwargs):
        raise AssertionError("Selection must not call a model")

    monkeypatch.setattr(LLMClient, "complete", forbidden)
    hosted(files=(".env.deepseek", ".env.zhipu"))
    client = browser(public_app)
    assert {item["id"] for item in panel(client)["deployments"]} == public_mode.DEPLOYMENTS
    for excluded in ("zhipu-glm-5.3", "zhipu-glm-5.3-flash", "openai-gpt-6.1-sol"):
        response = client.post("/api/live/select", {"deployment": excluded})
        assert response.code == 422 and code_of(client, response) == "invalid_input"
    assert unlock(client)[1]["result"] == "accepted"
    ws = ws_of(public_app, client)
    for item in live_config.SITE_DEPLOYMENTS:
        if item.id not in public_mode.DEPLOYMENTS:
            continue
        response = client.post("/api/live/select", {"deployment": item.id})
        assert response.code == 200
        assert data(client, response)["live"]["selected"] == item.id
        assert ws.live.snapshot()["available"]
        assert ws.live.config.budget_cny == Fraction(100)
        disclosure = data(client, client.get("/api/state"))["state"]["shell"]["public_disclosure"]
        assert item.label in disclosure and item.provider in disclosure
        assert client.post("/api/live/toggle", {"on": True}).code == 200
        refused = client.post("/api/live/select", {"deployment": DEEPSEEK})
        assert refused.code == 409 and code_of(client, refused) == "live_on"
        assert client.post("/api/live/toggle", {"on": False}).code == 200


def test_before_the_unlock_nothing_leaves_and_the_challenge_carries_the_kdf(
    public_app, hosted, canary
):
    journal = hosted()
    client = browser(public_app)
    assert send(client, BEFORE).code == 200
    # The yellow transfer holds the boundary until it is withdrawn.
    assert client.post("/api/stop", {}).code == 200
    assert send(client, BALANCE).code == 200
    assert canary.requests == [] and not list(journal.glob("*/session-*"))
    response, answer = challenge(client)
    assert response.code == 200 and "state" in answer
    assert answer["kdf"] == KDF.wire()
    assert re.fullmatch(r"[0-9a-f]{32}", answer["nonce"])
    from corridor.web import contracts

    assert contracts.problems(answer) == []
    # The code itself does not unlock a public site: only its verifier does.
    nonce = answer["nonce"]
    response = client.post("/api/live/unlock", {"nonce": nonce, "proof": access_proof(CODE, nonce)})
    assert response.code == 200 and data(client, response)["result"] == "invalid"
    assert canary.requests == []


def test_after_the_unlock_each_request_sends_the_message_and_the_journal_keeps_digests(
    public_app, hosted, canary, caplog
):
    caplog.set_level(logging.DEBUG)
    litellm_before = "litellm" in sys.modules
    journal = hosted()
    client = browser(public_app)
    assert send(client, BEFORE).code == 200
    assert client.post("/api/stop", {}).code == 200
    go_live(client)
    ws = ws_of(public_app, client)
    assert isinstance(ws.controller.client, JournalClient)
    response = send(client, BALANCE)
    assert response.code == 200, data(client, response)
    assert len(canary.requests) == 1
    request = canary.requests[0]
    body = request["body"]
    assert request["method"] == "POST" and request["path"] == "/v1/chat/completions"
    # The headers: the key, the content headers and the SDK's own telemetry about this
    # server's runtime (its OS, architecture and Python version); nothing of the browser.
    assert request["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert request["headers"]["User-Agent"].startswith("OpenAI/Python")
    for name in request["headers"]:
        assert name.lower().startswith(HEADER_FAMILIES), name
    assert set(body) == {
        "model",
        "messages",
        "max_tokens",
        "n",
        "parallel_tool_calls",
        "stream",
        "tools",
        "tool_choice",
        "thinking",
    }
    assert body["model"] == DEEPSEEK and body["max_tokens"] == 512 and body["n"] == 1
    assert body["stream"] is False and body["parallel_tool_calls"] is False
    assert body["thinking"] == {"type": "disabled"}
    assert body["tool_choice"] == {"type": "function", "function": {"name": "emit_result"}}
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    user = json.loads(body["messages"][1]["content"])
    assert set(user) == {"prompt", "tool_state"}
    assert set(user["prompt"]) == {
        "instruction",
        "intent_definitions",
        "user_text",
        "response_schema",
    }
    # The message itself (numerals normalized, long numbers tokenized by the vault)...
    assert "余额" in user["prompt"]["user_text"] and len(user["prompt"]["user_text"]) <= len(
        BALANCE
    )
    # ...and the previous reply's intent label: here the one of the transfer sent before the
    # unlock, a closed label that holds none of its words.
    assert user["tool_state"] == {"previous_intent": "transfer"}
    wire = request["raw"].decode("utf-8")
    assert BEFORE not in wire
    for secret in (ws.pair, client.browser, client.tab, client.generation):
        assert secret not in wire
    # A sentence the rules cannot read, naming a contact by an alias: the suggestion step.
    # Its prompt adds the contacts the text mentions (handle and the alias as written) and
    # the message itself; the vault does not tokenize an alias such as this one.
    response = send(client, VAGUE)
    assert response.code == 200, data(client, response)
    assert len(canary.requests) == 2
    later = json.loads(canary.requests[1]["body"]["messages"][1]["content"])
    assert set(later["prompt"]) == {
        "contacts",
        "instruction",
        "intent_definitions",
        "user_text",
        "response_schema",
    }
    assert later["prompt"]["contacts"] == {"CONTACT_CHEN": ["妈妈"]}
    assert later["prompt"]["user_text"] == VAGUE
    assert later["tool_state"] == {"previous_intent": "balance_query"}
    assert BEFORE not in canary.requests[1]["raw"].decode("utf-8")
    # The journal: a manifest, the outcome index and one record per attempt; the records
    # hold digests of the request and the model's output, never the words sent.
    sessions = sorted(journal.glob(f"{DEEPSEEK}/session-*"))
    assert len(sessions) == 1
    manifest = json.loads((sessions[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["deployment"] == DEEPSEEK and manifest["provider"] == "deepseek"
    records = sorted(sessions[0].glob("*/*.json"))
    assert len(records) == len(canary.requests)
    for path in records:
        text = path.read_text(encoding="utf-8")
        record = json.loads(text)
        assert set(record) == {
            "cost_usd",
            "output",
            "output_hash",
            "prompt_fingerprints",
            "replay_key",
            "request",
            "response_metadata",
            "response_metadata_hash",
            "source",
            "usage",
            "usage_hash",
        }
        assert record["source"] == "live-recorded"
        assert {"prompt_hash", "tool_state_hash", "step", "model", "api_base_hash"} <= set(
            record["request"]
        )
        assert "user_text" not in record["request"] and "messages" not in record
        for word in (BALANCE, BEFORE, VAGUE, "妈妈", "余额", FAKE_KEY):
            assert word not in text
    outcomes = [
        json.loads(line)
        for line in (sessions[0] / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [item["step"].split(":")[0] for item in outcomes] == ["intent", "suggest"]
    assert all(
        item["source"] == "live-recorded" and item["settlement"] == "settled" for item in outcomes
    )
    ledger = sqlite3.connect(journal / "live-ledger.sqlite3")
    rows = ledger.execute("SELECT currency, deployment, amount, settled FROM spend").fetchall()
    ledger.close()
    assert len(rows) == len(canary.requests) and all(row[3] == 1 for row in rows)
    assert all(row[0] == "CNY" and row[1] == DEEPSEEK for row in rows)
    assert ("litellm" in sys.modules) == litellm_before
    # Nothing secret in any response body or log line.
    logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (FAKE_KEY, CODE, VERIFIER):
        assert all(secret not in body for body in client.bodies)
        assert secret not in logs
    assert CANARY_ERROR not in logs


def test_a_transfer_sends_only_matching_contact_names_and_then_typed_handles(
    public_app, hosted, canary
):
    """Public extraction sends only locally mentioned names; planning uses typed handles."""
    journal = hosted()
    client = browser(public_app)
    go_live(client)
    assert send(client, BEFORE).code == 200
    assert steps_of(journal) == ["intent", "extract"]
    prompt, state = sent(canary.requests[1])
    assert set(prompt) == {"instruction", "repair", "response_schema", "user_text"}
    assert prompt["user_text"] == BEFORE and prompt["repair"] == 0
    assert state == {"contacts": {"CONTACT_CHEN": ["妈妈"]}, "previous_contact": None}
    # Handles and aliases, never the values behind the handles.
    vault = ws_of(public_app, client).controller.gateway.vault
    wire = canary.requests[1]["raw"].decode("utf-8")
    for value in ("Chen", "Qiao", *(v for k, v in vault._values.items() if k.startswith("PHONE_"))):
        assert value not in wire, value
    assert confirm(client).code == 200
    assert steps_of(journal) == ["intent", "extract", "plan"]
    prompt, state = sent(canary.requests[2])
    assert set(prompt) == {"instruction", "repair", "response_schema"}
    assert set(state) == {"amount", "contact", "source", "tool"} and state["tool"] == "transfer"
    assert state["contact"] == {"handle_type": "state", "ref": "CONTACT_CHEN"}
    assert state["source"] == {"handle_type": "state", "ref": "A_CURRENT"}
    assert state["amount"]["handle_type"] == "input"
    assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", state["amount"]["ref"])
    wire = canary.requests[2]["raw"].decode("utf-8")
    for word in (BEFORE, "500元", "妈妈", "房东"):
        assert word not in wire, word
    # The next transfer's extract step names the contact just used.
    assert send(client, "给妈妈转300元").code == 200
    assert sent(canary.requests[4])[1]["previous_contact"] == "CONTACT_CHEN"
    # Whatever the steps send, the journal keeps digests and the model's own output.
    session = next(iter(journal.glob(f"{DEEPSEEK}/session-*")))
    records = sorted(session.glob("*/*.json"))
    assert len(records) == len(canary.requests) == 5
    for path in records:
        text = path.read_text(encoding="utf-8")
        for word in (BEFORE, "给妈妈转300元", "妈妈", "房东", "500元", FAKE_KEY):
            assert word not in text, word


def test_only_long_numbers_emails_codes_and_keys_become_tokens(public_app, hosted, canary):
    """What the notice says of the message itself (RW-9 item 2): the vault replaces numbers
    of 11 or more digits, email addresses, verification codes and keys; a person's name, a
    place and an amount go as written. A message with a password never leaves."""
    hosted()
    client = browser(public_app)
    go_live(client)
    text = "我叫韩梅梅，住在中关村大街，身份证110101199003071234，邮箱hmm@example.com，帮我查一下"
    assert send(client, text).code == 200
    prompt, _ = sent(canary.requests[-1])
    words = prompt["user_text"]
    assert "韩梅梅" in words and "中关村大街" in words
    assert "110101199003071234" not in words and "hmm@example.com" not in words
    assert len(re.findall(r"PII_[0-9a-f]{16}", words)) == 2
    text = "给房东转300元，他手机13812345678，卡号6222021234567890123"
    assert send(client, text).code == 200
    prompt, _ = sent(canary.requests[-1])
    assert "300" in prompt["user_text"] and "房东" in prompt["user_text"]
    assert "13812345678" not in prompt["user_text"]
    assert "6222021234567890123" not in prompt["user_text"]
    assert prompt["contacts"] == {"CONTACT_LANDLORD": ["房东"]}
    assert send(client, "验证码123456").code == 200
    assert re.fullmatch(r"PII_[0-9a-f]{16}", sent(canary.requests[-1])[0]["user_text"])
    assert send(client, "sk-abcdefgh12345678 查余额").code == 200
    assert "abcdefgh" not in sent(canary.requests[-1])[0]["user_text"]
    before = len(canary.requests)
    assert send(client, "我的密码是abc123").code == 200
    assert len(canary.requests) == before


def test_the_live_panel_says_what_the_notice_says(public_app, hosted):
    """The judge's panel, once the switch is on, reads as measured too (RW-9 item 2)."""
    hosted()
    client = browser(public_app)
    first = client.open(view="judge")
    disclosure = first["state"]["shell"]["public_disclosure"]
    assert "服务器保留请求摘要与模型输出" in disclosure
    from corridor.web import handlers

    source = (handlers.STATIC / "js/views/live.js").read_text(encoding="utf-8")
    assert 'line(h, state.shell.public_disclosure, "live-disclosure")' in source
    go_live(client)
    text = panel(client)["reason_text"]
    assert "只发送本地命中的联系人代号和原话称呼" in text
    assert "未命中则澄清" in text and "类型化句柄" in text and DEEPSEEK in text
    assert "整张合成通讯录" not in text
    assert "11 位以上的号码" in text and "隐去个人信息" not in text


def test_the_chat_reaches_only_the_four_measured_steps(public_app, hosted, canary):
    """A sweep of the chat's business: every request that leaves is one of the steps the
    notice covers. The wealth pages' controller keeps the scripted client."""
    journal = hosted()
    client = browser(public_app)
    go_live(client)
    assert send(client, BEFORE).code == 200
    assert confirm(client).code == 200
    for text in (
        BALANCE,
        VAGUE,
        "看看9月账单",
        "查看我的订阅",
        "我想买点理财",
        "帮我准备老婆的生日",
        "我的卡丢了",
    ):
        assert send(client, text).code == 200, text
        assert client.post("/api/stop", {}).code == 200
    assert set(steps_of(journal)) == {"intent", "extract", "plan", "suggest"}
    assert len(steps_of(journal)) == len(canary.requests)


def test_switching_off_stops_the_requests_and_on_again_opens_a_new_journal(
    public_app, hosted, canary
):
    journal = hosted()
    client = browser(public_app)
    go_live(client)
    assert send(client, BALANCE).code == 200 and len(canary.requests) == 1
    assert client.post("/api/live/toggle", {"on": False}).code == 200
    assert send(client, BALANCE).code == 200 and len(canary.requests) == 1
    ws = ws_of(public_app, client)
    assert ws.controller.client is ws.scripted_client
    assert client.post("/api/live/toggle", {"on": True}).code == 200
    assert send(client, BALANCE).code == 200 and len(canary.requests) == 2
    assert len(sorted(journal.glob(f"{DEEPSEEK}/session-*"))) == 2


def test_a_failed_exchange_is_a_failure_on_the_page_and_the_retry_is_one_more_request(
    public_app, hosted, canary, caplog
):
    caplog.set_level(logging.DEBUG)
    journal = hosted()
    client = browser(public_app)
    go_live(client)
    canary.plan.append("fail")
    response = send(client, BALANCE)
    answer = data(client, response)
    assert len(canary.requests) == 1
    # An expected failure of the gesture: the page says so, names the failure's source for
    # the judge, and the provider's own message never reaches a body or a log.
    assert response.code == 422 and answer["error"]["code"] == "operation_failed", answer
    notices = answer["state"]["shell"]["notices"]
    assert any("live-error" in notice["text"] for notice in notices), notices
    assert CANARY_ERROR not in "\n".join(client.bodies)
    assert CANARY_ERROR not in "\n".join(record.getMessage() for record in caplog.records)
    # The retry is the judge's next message: exactly one more request, no automatic retry.
    assert client.post("/api/stop", {}).code == 200
    response = send(client, BALANCE)
    assert response.code == 200, data(client, response)
    assert len(canary.requests) == 2
    sessions = sorted(journal.glob(f"{DEEPSEEK}/session-*"))
    outcomes = [
        json.loads(line)
        for line in (sessions[0] / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(item["failed"], item["source"], item["settlement"]) for item in outcomes] == [
        # Sent, no usage returned: settled at the client's own pre-send bound, never zero.
        (True, "live-error", "bound"),
        (False, "live-recorded", "settled"),
    ]
    assert len(list(sessions[0].glob("*/*.json"))) == 1  # the failed attempt left no record


def test_the_session_call_cap_returns_the_page_to_the_script_at_the_boundary(
    public_app, hosted, canary
):
    hosted(**{live_config.SESSION_CALLS: "1"})
    client = browser(public_app)
    go_live(client)
    assert send(client, BALANCE).code == 200 and len(canary.requests) == 1
    answer = data(client, send(client, BALANCE))
    assert len(canary.requests) == 1
    ws = ws_of(public_app, client)
    assert ws.controller.client is ws.scripted_client and ws.live_toggle is False
    assert answer["state"]["shell"]["badge"]["kind"] == "demo"
    shown = panel(client)
    assert shown["toggle"]["on"] is False and shown["toggle"]["disabled"] is True
    assert ws.live.snapshot()["reason"] == "budget_exhausted"


def test_public_live_logs_hold_no_secrets_or_identifiers(public_app, hosted, canary, caplog):
    for name in ("tornado.access", "tornado.application", "tornado.general"):
        caplog.set_level(logging.DEBUG, logger=name)
    hosted()
    client = browser(public_app)
    ws = ws_of(public_app, client)
    _, wrong, nonce_wrong = unlock(client, "not-the-verifier")
    assert wrong["result"] == "invalid"
    response, right, nonce_right = unlock(client)
    assert right["result"] == "accepted"
    assert client.post("/api/live/toggle", {"on": True}).code == 200
    canary.plan.append("fail")
    send(client, BALANCE)
    send(client, BALANCE)
    text = caplog.text
    assert "/api/live/unlock" in text and "/api/chat/message" in text
    for secret in (
        FAKE_KEY,
        CODE,
        VERIFIER,
        nonce_wrong,
        nonce_right,
        access_proof(VERIFIER, nonce_right),
        access_proof("not-the-verifier", nonce_wrong),
        ADDRESS,
        client.browser,
        client.tab,
        client.generation,
        ws.pair,
        CANARY_ERROR,
    ):
        assert secret not in text
    assert "Traceback" not in text
    for found in reachable_text(ws):
        assert CODE not in found and FAKE_KEY not in found


# --- the client libraries' logs (design §4.11) ----------------------------------------------------


def test_the_public_entry_keeps_only_the_model_clients_warnings(caplog):
    loggers = [logging.getLogger(name) for name in public_mode.CLIENT_LOGGERS]
    levels = [logger.level for logger in loggers]
    try:
        caplog.set_level(logging.DEBUG)
        logging.getLogger("openai._base_client").debug("request body CANARY-BODY before")
        assert "CANARY-BODY before" in caplog.text
        public_mode.quiet_client_logs()
        logging.getLogger("openai._base_client").debug("request body CANARY-BODY after")
        logging.getLogger("httpx").info("HTTP Request CANARY-URL after")
        logging.getLogger("httpcore.http11").debug("CANARY-TRACE after")
        logging.getLogger("openai").warning("CANARY-WARNING after")
        assert "after" not in caplog.text.replace("CANARY-WARNING after", "")
        assert "CANARY-WARNING after" in caplog.text
        assert all(logger.level == logging.WARNING for logger in loggers)
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def test_the_entry_point_quiets_the_model_clients_only_in_public_mode():
    import ast
    from pathlib import Path

    from corridor.web import __main__ as entry

    tree = ast.parse(Path(entry.__file__).read_text(encoding="utf-8"))
    (main,) = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "main"
    ]
    guarded = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.If)
        and any(
            isinstance(call, ast.Call) and getattr(call.func, "id", None) == "quiet_client_logs"
            for call in ast.walk(node)
        )
    ]
    assert len(guarded) == 1
    assert ast.unparse(guarded[0].test) == "public is not None"


# --- the ledger of a public site (R8 S2) -----------------------------------------------------


def ledger_file(journal):
    return journal / live_runtime.SpendLedger.FILE


def assert_live_is_closed(public_app, client, canary):
    """Every door of the live path refuses, the scripted planner answers, nothing leaves."""
    seen = len(canary.requests)
    shown = panel(client)
    assert shown["unlock"]["disabled"] is True and shown["toggle"]["disabled"] is True
    assert "台账" in shown["reason_text"]
    response, answer = challenge(client)
    assert response.code == 409 and answer["error"]["code"] == "live_unavailable"
    response = client.post("/api/live/toggle", {"on": True})
    assert response.code == 409 and code_of(client, response) == "live_unavailable"
    assert send(client, BALANCE).code == 200
    assert len(canary.requests) == seen


def test_the_public_caps_line_promises_no_new_total(public_app, hosted):
    hosted()
    text = panel(browser(public_app))["caps_text"]
    assert "整站累计 100.00 CNY" in text and "不会重新计" in text
    assert "LIVE_JOURNAL_DIR" not in text


def test_a_new_journal_directory_closes_the_live_path_at_run_time(
    public_app, hosted, canary, tmp_path
):
    hosted(**{live_config.JOURNAL_DIR: str(tmp_path / "elsewhere")})
    client = browser(public_app)
    assert panel(client)["unlock"]["disabled"] is True
    response = client.post("/api/live/challenge", {})
    assert response.code == 409 and code_of(client, response) == "live_unavailable"
    assert canary.requests == []
    assert not ledger_file(tmp_path / "elsewhere").exists()


@pytest.mark.parametrize(
    "ruin",
    [
        lambda path: path.unlink(),
        lambda path: path.write_bytes(b""),
        lambda path: path.write_bytes(b"not a database, not at all, not even close"),
    ],
    ids=["deleted", "emptied", "corrupted"],
)
def test_a_lost_ledger_closes_the_live_path_and_no_budget_appears(public_app, hosted, canary, ruin):
    journal = hosted()
    ruin(ledger_file(journal))
    size = ledger_file(journal).stat().st_size if ledger_file(journal).exists() else None
    assert_live_is_closed(public_app, browser(public_app), canary)
    assert canary.requests == []
    # Neither the panel nor the refused doors started a ledger.
    after = ledger_file(journal).stat().st_size if ledger_file(journal).exists() else None
    assert after == size


def test_a_ledger_lost_while_a_session_is_live_stops_the_next_call(public_app, hosted, canary):
    journal = hosted()
    client = browser(public_app)
    go_live(client)
    assert send(client, BALANCE).code == 200
    assert len(canary.requests) == 1
    ledger_file(journal).unlink()
    # The session is at a boundary: the gate returns it to the scripted planner.
    assert send(client, BALANCE).code == 200
    assert len(canary.requests) == 1
    assert not ledger_file(journal).exists()
    assert not isinstance(ws_of(public_app, client).controller.client, JournalClient)
    assert_live_is_closed(public_app, client, canary)


def test_the_reservation_itself_refuses_a_lost_ledger(tmp_path):
    journal = tmp_path / "journal"
    live_runtime.SpendLedger(journal).initialize("CNY", DEEPSEEK, Fraction(0))
    ledger = live_runtime.SpendLedger(journal, create=False)
    assert ledger.remaining("CNY", public_mode.BUDGET_CNY) == public_mode.BUDGET_CNY
    ledger_file(journal).unlink()
    for call in (
        lambda: ledger.reserve("CNY", DEEPSEEK, 1, 100),
        lambda: ledger.remaining("CNY", 100),
        ledger.unreconciled,
        ledger.access_failures,
        ledger.record_access_failure,
    ):
        with pytest.raises(live_runtime.LedgerUnavailable):
            call()
    assert not ledger_file(journal).exists()


def test_the_reconciled_total_reopens_the_live_path_without_a_restart(public_app, hosted, canary):
    from corridor.web import ledger_setup

    journal = hosted()
    client = browser(public_app)
    ledger_file(journal).unlink()
    assert_live_is_closed(public_app, client, canary)
    assert ledger_setup.main(["--journal-dir", str(journal), "--spent-cny", "37.5"]) == 0
    go_live(client)
    assert send(client, BALANCE).code == 200
    assert len(canary.requests) == 1
    ledger = live_runtime.SpendLedger(journal, create=False)
    # The reconciled total stands; the one call settled a fraction of a yuan.
    left = ledger.remaining("CNY", public_mode.BUDGET_CNY)
    assert 62 < left < 62.5


def test_the_spend_survives_a_restart_of_the_process(public_app, hosted, canary):
    journal = hosted()
    client = browser(public_app)
    go_live(client)
    assert send(client, BALANCE).code == 200
    ledger = live_runtime.SpendLedger(journal, create=False)
    left = ledger.remaining("CNY", public_mode.BUDGET_CNY)
    assert left < public_mode.BUDGET_CNY
    # What a new process would see: a new session on the same file, no new budget.
    config = LiveConfig.from_environ()
    fresh = LiveSession(config, environ=live_runtime.PROCESS, ledger_create=False)
    assert fresh.ledger_ready() is True
    assert fresh.snapshot()["remaining"]["CNY"] == live_runtime.fraction_string(left)


def test_concurrent_reservations_on_an_existing_ledger_stop_at_the_cap(tmp_path):
    journal = tmp_path / "journal"
    live_runtime.SpendLedger(journal).initialize("CNY", DEEPSEEK, Fraction(0))
    ledger = live_runtime.SpendLedger(journal, create=False)
    granted, refused = [], []

    def reserve():
        try:
            ledger.reserve("CNY", DEEPSEEK, 1, 5)
        except live_runtime.BudgetExceeded:
            refused.append(1)
        else:
            granted.append(1)

    threads = [threading.Thread(target=reserve) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(granted) == 5 and len(refused) == 7
    assert ledger.remaining("CNY", 5) == 0


def test_outside_public_mode_a_session_still_starts_its_own_ledger(tmp_path):
    values = live_environ(tmp_path)
    config = LiveConfig.from_environ(values)
    session = LiveSession(config, environ=values)
    session.snapshot()
    assert session.ledger_ready() is True
    assert ledger_file(tmp_path / "journal").exists()
