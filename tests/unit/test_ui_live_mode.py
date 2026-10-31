"""S7: the domestic UI's live-model switch is a product-layer gesture over the same session.

Off by default and byte-identical to the scripted demo when off. Live access needs the
process switch, a credential file read by path, the judges' access code and a journal
directory at once; two caps (calls per session, cumulative spend per journal directory and
currency) fall back to the scripted planner with a visible notice; every real call is
written by ``LLMClient``'s record mode into a journal that replays strictly offline; a
model that fails the output contract is shown as a failure, never faked as a scripted
success. No test here calls a model: the transport is replaced by a fake inside the
client, and no secret is printed.
"""

import ast
import json
import re
from fractions import Fraction
from pathlib import Path

import pytest

from corridor.agent.llm import _ENV_FIELDS, BudgetExceeded, LLMConfigurationError, LLMOutputError
from corridor.bench.recording import network_disabled, replay_client
from corridor.crypto.jcs import H
from corridor.schemas.base import loads_exact
from corridor.ui import live_config, live_controls, live_runtime
from corridor.ui.live_config import DEPLOYMENTS, Credential, LiveConfig, read_credential
from corridor.ui.live_runtime import JournalClient, LiveSession, access_proof
from corridor.ui.messages import UI_MESSAGES
from tests._ast_checks import wallclock_violations
from tests.e2e.test_demo_ui import app, navigate

FAKE_KEY = "sk-test-only-live-key-0123456789abcdef"
ACCESS_CODE = "judge-pass-2026-Kx7Qm9Rv"
UI = Path(live_config.__file__).parent
LIVE_MODULES = ("live_config.py", "live_runtime.py", "live_controls.py")
# Every draw of the page's access field in the current test (its keyword arguments).
ACCESS_FIELD: list[dict] = []


@pytest.fixture(autouse=True)
def _no_inherited_client_settings(monkeypatch):
    """The frozen client would inherit LLM_* from the process; live access refuses them."""
    for name in _ENV_FIELDS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _access_field(monkeypatch):
    """AppTest cannot run the browser component, so its draws are recorded instead.

    The browser side (live_access/) sends only {nonce, proof}; ``unlock`` below puts
    exactly that value where the component would, and its HMAC is checked against
    Python's in test_ui_live_review3.
    """
    ACCESS_FIELD.clear()

    def field(**kwargs):
        ACCESS_FIELD.append(dict(kwargs))
        return None

    monkeypatch.setattr(live_controls, "_access_component", field)
    yield
    ACCESS_FIELD.clear()


# --- helpers ------------------------------------------------------------------------------


def credential_dir(tmp_path, *, files=(".env",), access_code_in_file=True):
    directory = tmp_path / "creds"
    directory.mkdir(parents=True, exist_ok=True)
    for name in files:
        lines = [
            "# synthetic credential file for tests; never a real key",
            f"LLM_API_KEY={FAKE_KEY}",
            "LLM_MAX_USD=1",
            "LLM_MODEL=some-other-model",
        ]
        if access_code_in_file:
            lines.append(f"LIVE_ACCESS_CODE={ACCESS_CODE}")
        (directory / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


def environ(tmp_path, **extra):
    values = {
        live_config.SWITCH: "1",
        live_config.ENV_DIR: str(credential_dir(tmp_path)),
        live_config.JOURNAL_DIR: str(tmp_path / "journal"),
    }
    values.update(extra)
    return values


class FakeModel:
    """Replace the client's transport: counts attempts, never opens a socket.

    ``behaviour`` applies to every attempt unless ``plan`` lists one per attempt:
    ``ok``; ``bad`` (the client refuses the response: ``output-rejected``); ``error`` (a
    failed provider exchange, as llm.py reports it: ``live-error``); ``timeout`` (the
    frozen client's own scripted fallback, reachable only on its non-Responses path).
    ``rule(step, output)`` may change one step's output or return an exception to raise.
    ``requests`` keeps every call's arguments and output, for strict replay.
    """

    def __init__(self, monkeypatch, behaviour="ok", *, cost=Fraction(1, 100), plan=None, rule=None):
        self.behaviour, self.cost, self.attempts = behaviour, cost, 0
        self.plan, self.rule, self.requests = list(plan or []), rule, []
        original = JournalClient.complete

        def complete(client, **kwargs):
            client._test_scripted = kwargs["scripted"]
            client._test_step = kwargs["step"]
            result = original(client, **kwargs)
            self.requests.append((kwargs, result.output))
            return result

        def live(client, prompt, state, trace):
            self.attempts += 1
            behaviour = self.plan.pop(0) if self.plan else self.behaviour
            if behaviour == "timeout":
                raise TimeoutError("fake provider timeout")
            client.api_calls += 1
            client.spent_usd += self.cost
            if behaviour == "error":
                trace["cost_usd"] = self.cost
                trace["source"] = "live-error"
                raise LLMOutputError("Model request failed; no replay recorded")
            trace.update(
                input_tokens=120,
                output_tokens=24,
                reasoning_tokens=0,
                total_tokens=144,
                cost_usd=self.cost,
                finish_reason="stop",
            )
            if behaviour == "bad":
                trace["source"] = "output-rejected"
                raise LLMOutputError("Invalid model response; no replay recorded")
            output = client._test_scripted()
            if self.rule is not None:
                output = self.rule(client._test_step, output)
                if isinstance(output, BaseException):
                    if isinstance(output, LLMOutputError):
                        trace["source"] = "output-rejected"
                    raise output
            return client._output(output)

        monkeypatch.setattr(JournalClient, "complete", complete)
        monkeypatch.setattr(JournalClient, "_live", live)


class Owner:
    """A stand-in for the controller the client is bound to (unit tests only)."""


def unit_session(tmp_path, monkeypatch, **extra):
    config = LiveConfig.from_environ(environ(tmp_path, **extra))
    session = LiveSession(config)
    assert session.authorize(ACCESS_CODE) == "accepted"
    return session


def call(client, step):
    return client.complete(
        task_id="S7",
        step=step,
        prompt={"instruction": "classify", "user_text": "balance"},
        tool_state={"previous_intent": None},
        scripted=lambda: {"intent": "balance_query", "needs_clarification": False},
    )


def shown_text(page):
    groups = (page.markdown, page.caption, page.warning, page.info, page.success, page.error)
    return "\n".join(str(item.value) for group in groups for item in group)


def everything_shown(page):
    """Every value AppTest holds for the page: text, labels, inputs and session state."""
    parts = [shown_text(page)]
    for group in (page.button, page.text_input, page.selectbox, page.toggle, page.expander):
        for element in group:
            parts.append(str(getattr(element, "label", "")))
            parts.append(str(getattr(element, "value", "")))
    parts.append(repr(dict(page.session_state._state.filtered_state)))
    return "\n".join(parts)


def start(monkeypatch, tmp_path, *, view="judge", locale="zh-CN", **extra):
    for name, value in environ(tmp_path, **extra).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", view)
    page = app()
    assert not page.exception
    page.button(key="enter").click().run()
    assert not page.exception
    return page


def unlock(page, code=ACCESS_CODE):
    """What the access field sends after the code is typed: the proof for the current
    challenge, never the code itself."""
    nonce = page.session_state["live_session"].challenge()
    page.session_state[live_controls.PROOF_KEY] = {
        "nonce": nonce,
        "proof": access_proof(code, nonce),
    }
    page.run()
    assert not page.exception
    return page


def access_field_disabled():
    """Whether the access field was drawn disabled in the latest run."""
    return bool(ACCESS_FIELD[-1]["disabled"])


def switch(page, value):
    page.toggle(key="live_mode").set_value(value).run()
    assert not page.exception
    return page


def toggle_disabled(page):
    """AppTest exposes the widget's disabled flag only on its proto."""
    return bool(page.toggle(key="live_mode").proto.disabled)


def send(page, text):
    page.text_input(key="domestic_text").set_value(text)
    page.button(key="domestic_send").click().run()
    assert not page.exception
    return page


def journal_records(root):
    """Recorded calls only: the per-directory manifest is not a call."""
    return sorted(path for path in Path(root).rglob("*.json") if path.name != live_runtime.MANIFEST)


def badge(page):
    return [str(item.value) for item in page.markdown if 'class="live-badge' in str(item.value)]


# --- registry and configuration -----------------------------------------------------------


def test_registry_names_models_and_prices_in_one_place():
    models = {item.model for item in DEPLOYMENTS}
    assert {"gpt-6.1-sol", "deepseek-flash", "glm-5.3", "glm-4.7-flash"} <= models
    assert [item.id for item in DEPLOYMENTS] == [
        "openai-gpt-6.1-sol",
        "openai-gpt-5.4-mini",
        "deepseek-flash",
        "zhipu-glm-5.3",
        "zhipu-glm-5.3-flash",
        "zhipu-glm-4.7-flash",
    ]
    assert len({item.id for item in DEPLOYMENTS}) == len(DEPLOYMENTS)
    for item in DEPLOYMENTS:
        assert item.provider in {"openai", "deepseek", "zhipu"}
        assert item.currency in {"USD", "CNY"}
        assert Fraction(item.price_in) > 0 and Fraction(item.price_out) > 0
        assert item.price_note in UI_MESSAGES and item.env_file.startswith(".env")
        assert item.label.isascii() and item.model in item.label
        assert item.api_base.startswith("https://")
        # The frozen client only accepts these generation profiles.
        if item.api == "chat":
            assert item.max_output_tokens == 512
        else:
            assert 128 <= item.max_output_tokens <= 8192
        assert item.api in {"chat", "responses"}
        assert live_config.deployment(item.id) is item
    by_model = {item.model: item for item in DEPLOYMENTS}
    assert by_model["gpt-6.1-sol"].currency == "USD" and by_model["gpt-6.1-sol"].env_file == ".env"
    assert by_model["deepseek-flash"].env_file == ".env.deepseek"
    assert by_model["glm-5.3"].env_file == ".env.zhipu" == by_model["glm-4.7-flash"].env_file
    assert (by_model["deepseek-flash"].price_in, by_model["deepseek-flash"].price_out) == ("2", "8")
    with pytest.raises(KeyError):
        live_config.deployment("no-such-deployment")


def test_mini_uses_the_frozen_formal_profile_and_budget_prices(tmp_path, monkeypatch):
    item = live_config.deployment("openai-gpt-5.4-mini")
    assert (item.provider, item.model, item.api) == ("openai", "gpt-5.4-mini", "responses")
    assert item.api_base == "https://api.openai.com/v1"
    assert (item.currency, item.price_in, item.price_out) == ("USD", "10", "40")
    assert item.env_file == ".env"
    assert (item.max_output_tokens, item.reasoning_effort) == (512, "low")
    note = UI_MESSAGES[item.price_note]
    assert "未经核验" in note["zh-CN"] and "not verified" in note["en"]
    session = unit_session(tmp_path, monkeypatch)
    session.select(item.id)
    client = session.client(Owner(), context_guard=None)
    assert (client.model, client.api, client.provider) == (item.model, item.api, item.provider)
    assert (client.max_output_tokens, client.reasoning_effort) == (512, "low")
    assert (client.assumed_price_in, client.assumed_price_out) == (Fraction(10), Fraction(40))
    assert session._reservation(item) == Fraction(16384 * 10 + 512 * 40, 1_000_000)


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ((".env", ".env.deepseek", ".env.zhipu"), "openai-gpt-6.1-sol"),
        ((".env.deepseek", ".env.zhipu"), "deepseek-flash"),
        ((".env.zhipu",), "zhipu-glm-5.3"),
    ],
)
def test_default_is_the_first_deployment_with_credentials(tmp_path, files, expected):
    values = environ(tmp_path)
    values[live_config.ENV_DIR] = str(credential_dir(tmp_path / "selected", files=files))
    session = LiveSession(LiveConfig.from_environ(values), environ=values)
    assert session.snapshot()["deployment"] == expected


def test_config_is_off_by_default_and_validates_caps(tmp_path):
    config = LiveConfig.from_environ({})
    assert config.enabled is False and config.env_dir is None and config.journal_dir is None
    assert config.session_calls == live_config.DEFAULT_SESSION_CALLS
    assert config.budget_cap("USD") == live_config.DEFAULT_BUDGET_USD
    assert config.budget_cap("CNY") == live_config.DEFAULT_BUDGET_CNY
    # The switch is exactly "1"; anything else keeps the scripted demo.
    for value in ("", "0", "true", "yes", "on"):
        assert LiveConfig.from_environ({live_config.SWITCH: value}).enabled is False
    config = LiveConfig.from_environ(
        environ(
            tmp_path,
            **{
                live_config.SESSION_CALLS: "3",
                live_config.BUDGET_USD: "1/2",
                live_config.BUDGET_CNY: "0",
            },
        )
    )
    assert config.enabled and config.session_calls == 3
    assert config.budget_cap("USD") == Fraction(1, 2) and config.budget_cap("CNY") == 0
    assert config.journal_dir == tmp_path / "journal"
    for bad in (
        {live_config.SESSION_CALLS: "-1"},
        {live_config.SESSION_CALLS: "two"},
        {live_config.BUDGET_USD: "-5"},
        {live_config.BUDGET_USD: "5.0.1"},
        {live_config.BUDGET_CNY: "nan"},
    ):
        with pytest.raises(LLMConfigurationError):
            LiveConfig.from_environ({live_config.SWITCH: "1", **bad})
    with pytest.raises(ValueError):
        config.budget_cap("EUR")


def test_credential_file_reads_only_the_named_fields_and_hides_the_key(tmp_path):
    directory = credential_dir(tmp_path)
    path = directory / ".env"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "LLM_API_BASE='https://api.openai.com/v1'\nLLM_API=responses\n"
        + "LLM_ASSUMED_PRICE_IN=999\nOTHER=value\n",
        encoding="utf-8",
    )
    credential = read_credential(path)
    assert isinstance(credential, Credential)
    assert credential.api_key == FAKE_KEY
    assert credential.access_code == ACCESS_CODE
    assert credential.model == "some-other-model"
    assert credential.api == "responses"
    assert credential.api_base == "https://api.openai.com/v1"
    # Only the five names are read; nothing else from the file is kept anywhere.
    assert not hasattr(credential, "max_usd") and not hasattr(credential, "assumed_price_in")
    assert set(live_config.CREDENTIAL_NAMES) == {
        "LLM_API_KEY",
        "LIVE_ACCESS_CODE",
        "LLM_MODEL",
        "LLM_API",
        "LLM_API_BASE",
    }
    assert FAKE_KEY not in repr(credential) and ACCESS_CODE not in repr(credential)
    assert FAKE_KEY not in str(credential)
    assert read_credential(directory / ".env.missing") is None
    (directory / ".env.nokey").write_text("LLM_MODEL=x\nLLM_API_KEY=\n", encoding="utf-8")
    assert read_credential(directory / ".env.nokey") is None
    config = LiveConfig.from_environ({live_config.SWITCH: "1", live_config.ENV_DIR: str(directory)})
    assert config.credential_path(DEPLOYMENTS[0]) == directory / DEPLOYMENTS[0].env_file
    assert LiveConfig.from_environ({}).credential_path(DEPLOYMENTS[0]) is None


# --- runtime: the three conditions, the caps, the journal, the failures -------------------


def test_live_access_needs_the_switch_the_credential_file_and_the_access_code(tmp_path):
    journal = {live_config.JOURNAL_DIR: str(tmp_path / "journal")}
    # 1. switch off: nothing else matters.
    off = LiveSession(LiveConfig.from_environ(environ(tmp_path, **{live_config.SWITCH: ""})))
    assert off.snapshot()["enabled"] is False
    assert off.snapshot()["available"] is False and off.snapshot()["reason"] == "disabled"
    assert off.authorize(ACCESS_CODE) == "unavailable"
    # 2. switch on, no credential directory / no file / a file without a key.
    nodir = LiveSession(LiveConfig.from_environ({live_config.SWITCH: "1", **journal}))
    assert nodir.snapshot()["reason"] == "missing_credentials"
    assert nodir.authorize(ACCESS_CODE) == "unavailable"
    empty = tmp_path / "empty"
    empty.mkdir()
    nofile = LiveSession(
        LiveConfig.from_environ({
            live_config.SWITCH: "1",
            live_config.ENV_DIR: str(empty),
            **journal,
        })
    )
    assert nofile.snapshot()["reason"] == "missing_credentials"
    assert {item["credential"] for item in nofile.snapshot()["deployments"]} == {"missing_file"}
    (empty / ".env").write_text("LLM_MODEL=x\n", encoding="utf-8")
    nokey = LiveSession(
        LiveConfig.from_environ({
            live_config.SWITCH: "1",
            live_config.ENV_DIR: str(empty),
            **journal,
        })
    )
    assert nokey.snapshot()["deployments"][0]["credential"] == "no_key"
    assert nokey.snapshot()["reason"] == "missing_credentials"
    # 3. a credential file whose endpoint disagrees with the registry is refused: the key
    #    is only ever sent to the registry's fixed endpoint for that provider.
    (empty / ".env").write_text(
        f"LLM_API_KEY={FAKE_KEY}\nLLM_API_BASE=https://example.invalid/v1\n", encoding="utf-8"
    )
    mismatch = LiveSession(
        LiveConfig.from_environ({
            live_config.SWITCH: "1",
            live_config.ENV_DIR: str(empty),
            **journal,
        })
    )
    assert mismatch.snapshot()["deployments"][0]["credential"] == "endpoint_mismatch"
    assert mismatch.snapshot()["reason"] == "missing_credentials"
    # 4. switch on and a valid file: the access code is still required.
    session = LiveSession(LiveConfig.from_environ(environ(tmp_path)))
    info = session.snapshot()
    assert info["enabled"] and info["deployments"][0]["credential"] == "ok"
    assert info["available"] is False and info["reason"] == "access_required"
    assert info["authorized"] is False
    assert session.authorize("wrong") == "invalid"
    assert session.snapshot()["reason"] == "access_required"
    assert session.authorize(ACCESS_CODE) == "accepted"
    info = session.snapshot()
    assert info["authorized"] and info["available"] and info["reason"] == "ready"
    assert info["deployment"] == DEPLOYMENTS[0].id
    # The code from the process environment wins over the one in the file; it is read
    # from the environment when a code is checked, never kept in the configuration.
    values = environ(tmp_path, **{live_config.ACCESS_CODE: "env-code-Kx7Qm9Rv2Lp4"})
    env_code = LiveSession(LiveConfig.from_environ(values), environ=values)
    assert "env-code-Kx7Qm9Rv2Lp4" not in repr(env_code.config)
    assert env_code.authorize(ACCESS_CODE) == "invalid"
    assert env_code.authorize("env-code-Kx7Qm9Rv2Lp4") == "accepted"
    # Without any code anywhere, live access cannot be unlocked at all.
    nocode_dir = credential_dir(tmp_path / "nocode", access_code_in_file=False)
    nocode = LiveSession(
        LiveConfig.from_environ({
            live_config.SWITCH: "1",
            live_config.ENV_DIR: str(nocode_dir),
            **journal,
        })
    )
    assert nocode.snapshot()["reason"] == "access_required"
    assert nocode.authorize("") == "unavailable" and nocode.authorize("anything") == "unavailable"
    # Selecting a deployment whose file is missing makes the session unavailable, not
    # the other deployment's key reused.
    session.select("deepseek-flash")
    assert session.snapshot()["reason"] == "missing_credentials"
    session.select(DEPLOYMENTS[0].id)
    assert session.snapshot()["reason"] == "ready"
    with pytest.raises(KeyError):
        session.select("no-such-deployment")


def test_wrong_access_codes_lock_the_session_after_five_failures(tmp_path):
    session = LiveSession(LiveConfig.from_environ(environ(tmp_path)))
    for _ in range(live_runtime.LOCK_AFTER_FAILURES):
        assert session.authorize("nope") == "invalid"
    assert session.authorize(ACCESS_CODE) == "locked"
    assert session.snapshot()["locked"] is True and session.snapshot()["available"] is False
    # A fresh session (a new browser session) starts again.
    assert LiveSession(LiveConfig.from_environ(environ(tmp_path))).authorize(ACCESS_CODE) == (
        "accepted"
    )


def test_session_call_cap_stops_live_calls_and_marks_exhaustion(tmp_path, monkeypatch):
    fake = FakeModel(monkeypatch)
    session = unit_session(tmp_path, monkeypatch, **{live_config.SESSION_CALLS: "2"})
    owner = Owner()
    client = session.client(owner, context_guard=None)
    assert isinstance(client, JournalClient) and client.mode == "record"
    assert client.max_calls == 2
    assert call(client, "one").source == "live-recorded"
    assert call(client, "two").source == "live-recorded"
    info = session.snapshot()
    assert info["calls"] == 2 and info["api_calls"] == 2 and info["calls_remaining"] == 0
    assert info["available"] is False and info["reason"] == "budget_exhausted"
    with pytest.raises(BudgetExceeded):
        call(client, "three")
    assert fake.attempts == 2
    assert session.last_call()["source"] == "budget-rejected"
    assert session.last_call()["failed"] is True
    # An exhausted session hands out no client at all; the spend settled to usage.
    with pytest.raises(BudgetExceeded):
        session.client(owner, context_guard=None)
    assert session.snapshot()["spent"]["USD"] == str(Fraction(2, 100))


def test_spend_cap_is_shared_across_sessions_through_the_ledger(tmp_path, monkeypatch):
    fake = FakeModel(monkeypatch, cost=Fraction(3, 100))
    first = unit_session(tmp_path, monkeypatch, **{live_config.BUDGET_USD: "1/2"})
    client = first.client(Owner(), context_guard=None)
    # One call reserves the assumed cost of 16384 input tokens plus the output cap (the
    # external review: the frozen client then refuses before sending any request whose
    # own bound exceeds it), then settles to the provider's exact usage.
    reservation = live_runtime.LiveSession._reservation(DEPLOYMENTS[0])
    assert live_runtime.RESERVE_INPUT_TOKENS == 16384
    assert reservation == Fraction(16384 * 10 + 1024 * 40, 1_000_000)
    assert first.snapshot()["reservation"] == str(reservation)
    assert call(client, "one").source == "live-recorded"
    assert first.snapshot()["remaining"]["USD"] == str(Fraction(47, 100))
    assert first.snapshot()["spent"]["USD"] == str(Fraction(3, 100))
    # Another session sharing the journal directory sees the same cumulative spend.
    second = unit_session(tmp_path, monkeypatch, **{live_config.BUDGET_USD: "1/2"})
    assert second.snapshot()["remaining"]["USD"] == str(Fraction(47, 100))
    # A cap that cannot cover one reservation is exhausted before any transport attempt.
    tiny = unit_session(tmp_path, monkeypatch, **{live_config.BUDGET_USD: "1/1000"})
    assert tiny.snapshot()["available"] is False and tiny.snapshot()["reason"] == (
        "budget_exhausted"
    )
    with pytest.raises(BudgetExceeded):
        tiny.client(Owner(), context_guard=None)
    assert fake.attempts == 1
    # A cap reached by earlier sessions refuses the reservation of a new one.
    later = unit_session(tmp_path, monkeypatch, **{live_config.BUDGET_USD: "3/20"})
    assert later.snapshot()["reason"] == "budget_exhausted"
    assert fake.attempts == 1
    # A zero cap for the deployment's currency is "exhausted" from the start.
    zero = unit_session(tmp_path, monkeypatch, **{live_config.BUDGET_USD: "0"})
    assert zero.snapshot()["reason"] == "budget_exhausted"
    # The other currency's cap does not apply to a USD deployment.
    cny_zero = unit_session(tmp_path, monkeypatch, **{live_config.BUDGET_CNY: "0"})
    assert cny_zero.snapshot()["reason"] == "ready"


def test_each_live_call_is_recorded_and_replays_strictly_without_network(tmp_path, monkeypatch):
    FakeModel(monkeypatch)
    session = unit_session(tmp_path, monkeypatch)
    client = session.client(Owner(), context_guard=None)
    result = call(client, "one")
    assert result.source == "live-recorded"
    assert result.output == {"intent": "balance_query", "needs_clarification": False}
    root = client.replay_dir
    assert root.is_relative_to(tmp_path / "journal")
    assert root.parent.name == DEPLOYMENTS[0].id and root.name.startswith("session-")
    # The page only ever sees the directory relative to LIVE_JOURNAL_DIR.
    assert session.snapshot()["journal_dir"] == f"{DEPLOYMENTS[0].id}/{root.name}"
    records = journal_records(root)
    assert len(records) == 1
    record = loads_exact(records[0].read_bytes())
    assert record["source"] == "live-recorded"
    assert record["output"] == result.output and record["output_hash"] == H(result.output)
    assert record["cost_usd"] == "1/100"
    assert record["request"]["model"] == DEPLOYMENTS[0].model
    assert FAKE_KEY.encode("utf-8") not in records[0].read_bytes()
    # Strict replay from the journal, with every network transport disabled.
    replay = replay_client(client)
    assert replay.mode == "strict" and replay.replay_dir == client.replay_dir
    with network_disabled():
        again = call(replay, "one")
    assert again.source == "replay" and again.output == result.output
    # A second session of the same deployment gets its own journal directory, so the
    # same request identity can be recorded again without clashing.
    other = unit_session(tmp_path, monkeypatch)
    other_client = other.client(Owner(), context_guard=None)
    assert other_client.replay_dir != client.replay_dir
    assert call(other_client, "one").source == "live-recorded"
    assert len(journal_records(tmp_path / "journal")) == 2


def test_model_output_failure_raises_and_records_no_fake_success(tmp_path, monkeypatch):
    fake = FakeModel(monkeypatch, behaviour="bad")
    session = unit_session(tmp_path, monkeypatch)
    client = session.client(Owner(), context_guard=None)
    with pytest.raises(LLMOutputError):
        call(client, "one")
    assert fake.attempts == 1
    assert journal_records(tmp_path / "journal") == []
    last = session.last_call()
    assert last["source"] == "output-rejected" and last["failed"] is True
    info = session.snapshot()
    # The attempt counted and the provider's usage was charged; live access remains.
    assert info["calls"] == 1 and info["spent"]["USD"] == "1/100"
    assert info["available"] is True and client.mode == "record"
    # The frozen client's own timeout fallback (its non-Responses path; the registered
    # deployments never take it, see the transport test) is recorded as what it is.
    FakeModel(monkeypatch, behaviour="timeout")
    result = call(client, "two")
    assert result.source == "scripted-timeout"
    assert session.last_call() == {"source": "scripted-timeout", "failed": False}
    assert journal_records(tmp_path / "journal") == []


def test_snapshot_and_reprs_never_expose_secrets(tmp_path, monkeypatch):
    FakeModel(monkeypatch)
    session = unit_session(tmp_path, monkeypatch)
    client = session.client(Owner(), context_guard=None)
    call(client, "one")
    for text in (
        json.dumps(session.snapshot(), ensure_ascii=False, default=str),
        repr(session),
        repr(session.config),
        repr(client),
        str(session.snapshot()["deployments"]),
    ):
        assert FAKE_KEY not in text and ACCESS_CODE not in text
    assert "api_key" not in session.snapshot() and "access_code" not in session.snapshot()
    ledger = tmp_path / "journal" / "live-ledger.sqlite3"
    assert ledger.exists()
    assert FAKE_KEY.encode("utf-8") not in ledger.read_bytes()
    assert ACCESS_CODE.encode("utf-8") not in ledger.read_bytes()


def test_live_modules_are_free_of_printing_logging_and_wallclock():
    for name in LIVE_MODULES:
        source = (UI / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = {
            getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert "print" not in calls, name
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "logging" not in imports and "datetime" not in imports, name
        assert not wallclock_violations(source, "ui/" + name), name
        # No host clock at all: the spend cap is cumulative per journal directory.
        assert "time" not in imports and "gmtime" not in source, name
    # The page module never touches a key or a code; it hands the typed code to the
    # session and renders snapshots only.
    controls = (UI / "live_controls.py").read_text(encoding="utf-8")
    assert "api_key" not in controls and "_api_key" not in controls
    assert "access_code" not in controls.replace("live.access_code", "").replace(
        "live_access_code", ""
    )
    messages = loads_exact((UI / "messages-live.json").read_bytes())
    for key, translations in messages.items():
        assert key.startswith("live."), key
        for locale in ("zh-CN", "en"):
            assert "{key}" not in translations[locale] and "{code}" not in translations[locale]


# --- the page -----------------------------------------------------------------------------


def test_switch_off_keeps_the_scripted_client_and_shows_the_scripted_badge(monkeypatch, tmp_path):
    # The three variables of the other tests are absent; the credential directory alone
    # changes nothing.
    monkeypatch.delenv(live_config.SWITCH, raising=False)
    monkeypatch.setenv(live_config.ENV_DIR, str(credential_dir(tmp_path)))
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "judge")
    page = app()
    assert not page.exception
    controller = page.session_state["controller"]
    before = controller.client
    assert before.mode == "scripted"
    assert "live_session" not in page.session_state
    assert not [t for t in page.toggle if t.key == "live_mode"]
    assert not ACCESS_FIELD
    assert badge(page) and UI_MESSAGES["live.badge.scripted"]["zh-CN"] in badge(page)[0]
    assert UI_MESSAGES["live.off_hint"]["zh-CN"] in shown_text(page)
    page.button(key="enter").click().run()
    send(page, "给妈妈转800元")
    assert controller.client is before and controller.client.mode == "scripted"
    session = page.session_state["domestic_session"]
    assert session.client is before
    assert session.response(page.session_state["domestic_response_id"])["planner_mode"] == (
        "scripted"
    )
    assert [c["source"] for c in before.calls] and set(c["source"] for c in before.calls) == {
        "scripted"
    }
    assert "live_session" not in page.session_state
    assert FAKE_KEY not in everything_shown(page)
    # The customer view shows no hint about the switch, only the badge.
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "customer")
    page = app()
    assert UI_MESSAGES["live.off_hint"]["zh-CN"] not in shown_text(page)
    assert UI_MESSAGES["live.badge.scripted"]["zh-CN"] in badge(page)[0]


def test_unlocking_switches_the_client_and_the_badge_both_ways(monkeypatch, tmp_path):
    page = start(monkeypatch, tmp_path)
    controller = page.session_state["controller"]
    scripted = controller.client
    live = page.session_state["live_session"]
    assert isinstance(live, LiveSession)
    assert toggle_disabled(page) is True
    assert UI_MESSAGES["live.reason.access_required"]["zh-CN"] in shown_text(page)
    unlock(page, "wrong-code")
    assert UI_MESSAGES["live.denied"]["zh-CN"] in shown_text(page)
    assert toggle_disabled(page) is True
    assert controller.client is scripted
    unlock(page)
    assert UI_MESSAGES["live.unlocked"]["zh-CN"] in shown_text(page)
    assert toggle_disabled(page) is False
    assert page.toggle(key="live_mode").value is False
    # Unlocked but not switched on: still the scripted client and badge.
    assert controller.client is scripted
    assert UI_MESSAGES["live.badge.scripted"]["zh-CN"] in badge(page)[0]
    switch(page, True)
    assert isinstance(controller.client, JournalClient) and controller.client.mode == "record"
    assert controller.client.model == DEPLOYMENTS[0].model
    assert controller.chat.client is controller.client
    assert page.session_state["domestic_session"].client is controller.client
    assert controller.planner_source == f"record: {DEPLOYMENTS[0].model}"
    label = DEPLOYMENTS[0].label
    assert UI_MESSAGES["live.badge.live"]["zh-CN"].format(deployment=label) in badge(page)[0]
    assert UI_MESSAGES["live.badge.scripted"]["zh-CN"] not in badge(page)[0]
    # Off again: the very same scripted client object is restored.
    switch(page, False)
    assert controller.client is scripted
    assert page.session_state["domestic_session"].client is scripted
    assert UI_MESSAGES["live.badge.scripted"]["zh-CN"] in badge(page)[0]
    assert controller.planner_source == "scripted"


def test_live_request_is_journaled_and_judge_view_counts_it(monkeypatch, tmp_path):
    fake = FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    send(page, "给妈妈转800元")
    session = page.session_state["domestic_session"]
    response = session.response(page.session_state["domestic_response_id"])
    assert response["planner_mode"] == "record"
    assert response["card"]["kind"] == "transfer"
    assert fake.attempts >= 1
    sources = [c["source"] for c in controller.client.calls]
    assert sources and set(sources) == {"live-recorded"}
    root = controller.client.replay_dir
    relative = f"{DEPLOYMENTS[0].id}/{root.name}"
    assert page.session_state["live_session"].snapshot()["journal_dir"] == relative
    assert len(journal_records(root)) == len(sources)
    text = shown_text(page)
    counters = UI_MESSAGES["live.status"]["zh-CN"].split("{")[0]
    assert counters in text
    # The judge line names the directory relative to LIVE_JOURNAL_DIR, never a host path.
    assert relative in text and str(root) not in text and str(tmp_path) not in text
    assert UI_MESSAGES["live.journal"]["zh-CN"].split("{")[0] in text
    # Every request of the page replays strictly from the journal without any network.
    replay = replay_client(controller.client)
    assert replay.mode == "strict" and replay.replay_dir == root
    assert len(fake.requests) == len(sources)
    with network_disabled():
        for kwargs, output in fake.requests:
            again = replay.complete(**kwargs)
            assert again.source == "replay" and again.output == output
    # The customer view keeps the badge and drops the counters.
    page.button(key="view_toggle").click().run()
    assert not page.exception and page.session_state["polish_view"] == "customer"
    text = shown_text(page)
    assert counters not in text and relative not in text
    assert UI_MESSAGES["live.badge.live"]["zh-CN"].split("{")[0] in badge(page)[0]


def test_output_format_failure_is_shown_as_a_failure_in_live_mode(monkeypatch, tmp_path):
    FakeModel(monkeypatch, behaviour="bad")
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    page.text_input(key="domestic_text").set_value("给妈妈转800元")
    page.button(key="domestic_send").click().run()
    assert not page.exception
    text = shown_text(page)
    failure = UI_MESSAGES["live.failure"]["zh-CN"].format(
        source="output-rejected",
        reason=UI_MESSAGES["live.reason_of.output-rejected"]["zh-CN"],
    )
    assert failure in text
    # No reply was produced, nothing was faked and the live client stays live.
    assert "domestic_response_id" not in page.session_state
    assert "domestic_transcript" not in page.session_state
    assert controller.client.mode == "record"
    assert all(c["source"] == "output-rejected" for c in controller.client.calls)
    assert journal_records(tmp_path / "journal") == []
    assert page.session_state["live_session"].snapshot()["available"] is True
    # The line belongs to that request only: the next run does not repeat it.
    page.run()
    assert not page.exception
    assert failure not in shown_text(page)
    assert controller.client.mode == "record"


def test_cap_exhaustion_returns_to_scripted_with_a_visible_notice(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path, **{live_config.SESSION_CALLS: "1"})
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    scripted = page.session_state["live_scripted_client"]
    # A balance query is one classification call and leaves no draft behind, so the
    # cap is reached at a transaction boundary.
    send(page, "查一下余额")
    live = page.session_state["live_session"]
    session = page.session_state["domestic_session"]
    assert session.response(page.session_state["domestic_response_id"])["planner_mode"] == (
        "record"
    )
    # The reply itself was live; the rerun that follows it finds the cap reached at a
    # transaction boundary, switches off, says so and restores the scripted client.
    assert live.snapshot()["calls"] == 1 and live.snapshot()["reason"] == "budget_exhausted"
    text = shown_text(page)
    notice = UI_MESSAGES["live.exhausted_notice"]["zh-CN"].format(
        reason=UI_MESSAGES["live.exhausted.calls"]["zh-CN"]
    )
    assert notice in text
    assert UI_MESSAGES["live.reason.budget_exhausted"]["zh-CN"] in text
    assert page.toggle(key="live_mode").value is False
    assert toggle_disabled(page) is True
    assert controller.client is scripted and controller.client.mode == "scripted"
    assert session.client is scripted
    assert UI_MESSAGES["live.badge.scripted"]["zh-CN"] in badge(page)[-1]
    # The notice is shown once; the scripted demo keeps working after the fallback.
    page.run()
    assert not page.exception
    assert notice not in shown_text(page)
    send(page, "查一下余额")
    assert controller.client is scripted
    assert session.response(page.session_state["domestic_response_id"])["planner_mode"] == (
        "scripted"
    )


def test_timeout_fallback_to_scripted_is_disclosed(monkeypatch, tmp_path):
    # The frozen client's own timeout fallback (not reachable by the registered
    # deployments, whose timeouts are live-error failures) is disclosed on the reply.
    fake = FakeModel(monkeypatch, behaviour="timeout")
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    send(page, "给妈妈转800元")
    assert fake.attempts >= 1
    sources = [c["source"] for c in controller.client.calls]
    assert set(sources) == {"scripted-timeout"}
    note = UI_MESSAGES["live.note.partly_scripted"]["zh-CN"].format(count=len(sources))
    assert note in shown_text(page)
    assert controller.client.mode == "record"
    assert journal_records(tmp_path / "journal") == []


def test_no_secret_reaches_the_page_the_session_state_or_the_journal(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    send(page, "给妈妈转800元")
    navigate(page, "device")
    assert not page.exception
    assert FAKE_KEY not in everything_shown(page)
    assert ACCESS_CODE not in shown_text(page)
    navigate(page, "audit")
    assert not page.exception
    assert FAKE_KEY not in everything_shown(page)
    for path in (tmp_path / "journal").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert FAKE_KEY.encode("utf-8") not in data, path
            assert ACCESS_CODE.encode("utf-8") not in data, path
    controller = page.session_state["controller"]
    assert FAKE_KEY not in json.dumps(controller.client.calls, default=str)
    assert FAKE_KEY not in repr(page.session_state["live_session"].snapshot())


def test_switching_is_locked_while_an_action_is_pending(monkeypatch, tmp_path):
    page = start(monkeypatch, tmp_path)
    unlock(page)
    controller = page.session_state["controller"]
    assert controller.at_transaction_boundary() is True
    assert toggle_disabled(page) is False
    # A confirmed red transfer waits for a device gesture: no switch until it is done.
    send(page, "给妈妈转5000元")
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert controller.at_transaction_boundary() is False
    assert toggle_disabled(page) is True
    assert UI_MESSAGES["live.boundary_locked"]["zh-CN"] in shown_text(page)
    with pytest.raises(ValueError):
        controller.set_client(object())
    assert controller.client.mode == "scripted"
    page.button(key="stop_all").click().run()
    assert not page.exception
    assert controller.at_transaction_boundary() is True
    assert toggle_disabled(page) is False


def test_model_proposals_still_go_through_the_kernel_and_gateway(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    # A red transfer proposed through the live client still needs the device gesture and
    # the simulated SMS; the card is the gateway's canonical card.
    send(page, "给妈妈转5000元")
    session = page.session_state["domestic_session"]
    response = session.response(page.session_state["domestic_response_id"])
    assert response["planner_mode"] == "record" and response["tier"] == "red"
    assert response["card"]["tier"] == "red"
    assert controller.gateway.bank.execute_count == 0
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert session.response(page.session_state["domestic_response_id"])["status"] == ("waiting_mfa")
    assert controller.gateway.bank.execute_count == 0
    # The S6 recipient check applies to a live proposal exactly as to a scripted one.
    page.button(key="stop_all").click().run()
    assert not page.exception
    send(page, "给新联系人转480元")
    identifier = page.session_state["domestic_response_id"]
    stem = f"domestic_scam:{identifier}:"
    assert {e.key.rsplit(":", 1)[-1] for e in page.radio if e.key.startswith(stem)} == {
        "verified",
        "guided",
    }
    assert "domestic_confirm" not in {button.key for button in page.button}
    assert controller.gateway.bank.execute_count == 0
    assert all(trace["source"] == "live-recorded" for trace in controller.client.calls)


def test_demo_presets_keep_their_own_scripted_controller_and_the_badge_says_so(
    monkeypatch, tmp_path
):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    assert controller.client.mode == "record"
    page.button(key="load_demo_preset").click().run()
    assert not page.exception and page.session_state["show_demo"] is True
    demo = page.session_state["demo_scenario"]
    assert demo["controller"] is not controller
    assert demo["controller"].client.mode == "scripted"
    # The open preset is scripted, and the badge describes it, not the live banking
    # controller behind it, which keeps its live client.
    assert UI_MESSAGES["live.badge.scripted"]["zh-CN"] in badge(page)[-1]
    assert controller.client.mode == "record"
    page.button(key="close_demo").click().run()
    assert not page.exception
    label = DEPLOYMENTS[0].label
    assert UI_MESSAGES["live.badge.live"]["zh-CN"].format(deployment=label) in badge(page)[-1]


def test_badge_copy_is_bilingual_and_names_the_deployment():
    for key in ("live.badge.live", "live.badge.scripted", "live.status", "live.failure"):
        assert key in UI_MESSAGES, key
    for locale in ("zh-CN", "en"):
        assert "{deployment}" in UI_MESSAGES["live.badge.live"][locale]
    assert UI_MESSAGES["live.badge.scripted"]["zh-CN"] == "脚本演示"
    assert UI_MESSAGES["live.badge.live"]["zh-CN"].startswith("实时模型：")
    assert re.search(r"[Ll]ive model", UI_MESSAGES["live.badge.live"]["en"])
    zh = {key: value["zh-CN"] for key, value in UI_MESSAGES.items() if key.startswith("live.")}
    for key, text in zh.items():
        for banned in ("保证", "零副作用", "风控", "识别诈骗", "密钥已", "key="):
            assert banned not in text, (key, banned)
    assert "未退回脚本" in zh["live.failure"]
    assert "脚本" in zh["live.note.partly_scripted"]
