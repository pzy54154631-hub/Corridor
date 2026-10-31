"""The e2e infrastructure itself: fixtures, runner bookkeeping and harness guards (no browser).

Runs inside the normal pytest suite; it starts no server of the app and opens no socket
beyond the loopback.
"""

import asyncio
import hashlib
import json
import os
import re
import socket
import ssl
from pathlib import Path

import pytest

from tests.web_e2e import harness, make_fixtures, run_e2e

HERE = Path(__file__).resolve().parent
TITLE = re.compile(r"\btest\(\s*'([^']+)'")
MODE_TAG = re.compile(
    r"@(scripted|live-fake|suggest-fake|fail-once|fail-unknown|public|lab-scripted|lab-unknown"
    r"|fixtures)\b"
)


def test_fixtures_match_the_generator_and_the_contract():
    assert make_fixtures.check() == []
    built = make_fixtures.build()
    on_disk = {path.stem for path in make_fixtures.FIXTURES.glob("*.json")}
    assert on_disk == set(built)
    for name, body in built.items():
        text = (make_fixtures.FIXTURES / f"{name}.json").read_text(encoding="utf-8")
        assert json.loads(text) == body, name


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda s: s["shell"].__setitem__("judge_strip", "x"), "judge-only key present"),
        (lambda s: s.__setitem__("revision", 1.5), "float"),
        (lambda s: s["device"]["tasks"][0].__setitem__("step", "approve"), "not in"),
        (lambda s: s["chat"].__setitem__("extra", 1), "not in the contract"),
        (lambda s: s["device"]["tasks"][0]["gesture"].__setitem__("token", "short"), "match"),
    ],
)
def test_the_checker_rejects_contract_violations(mutate, expected):
    body = json.loads(
        (make_fixtures.FIXTURES / "customer-red-code.json").read_text(encoding="utf-8")
    )
    mutate(body["state"])
    problems = make_fixtures.check_response(body)
    assert any(expected in problem for problem in problems), problems


def test_every_journey_spec_names_its_mode():
    for spec in sorted((HERE / "specs").glob("[0-9][0-9]*.spec.cjs")):
        titles = TITLE.findall(spec.read_text(encoding="utf-8"))
        assert titles, spec.name
        for title in titles:
            assert MODE_TAG.search(title), f"{spec.name}: {title}"


def test_grep_combines_the_mode_tag_and_the_user_pattern():
    pattern = re.compile(run_e2e.grep_for("scripted", "01-"))
    assert pattern.search("01-transfer-tiers.spec.cjs desktop yellow @scripted")
    assert not pattern.search("02-new-payee-gate.spec.cjs desktop x @scripted")
    assert not pattern.search("01-transfer-tiers.spec.cjs desktop x @fail-once")
    fixtures = re.compile(run_e2e.grep_for("fixtures", None))
    assert fixtures.search("c-shell.spec.cjs @c-header")
    assert not fixtures.search("01-transfer-tiers.spec.cjs @scripted")


def test_the_harness_serves_this_work_trees_code():
    """Whichever interpreter runs the journeys, the harness imports this tree's ``src``
    first (and refuses to serve another tree's ``corridor``)."""
    path = run_e2e.harness_env()["PYTHONPATH"].split(os.pathsep)
    assert path[:2] == [str(run_e2e.ROOT / "src"), str(run_e2e.ROOT)]


def test_only_the_lab_lanes_open_the_closed_lab():
    """RW-4'': the submission build closes the lab; only its own lanes open it, and the
    scripted lane (the materials' screenshots) never selects a lab journey."""
    assert run_e2e.harness_for("scripted", "") == ("scripted", "")
    assert run_e2e.harness_for("fail-unknown", "x") == ("fail-unknown", "x")
    assert run_e2e.harness_for("lab-scripted", "") == ("scripted", "lab")
    assert run_e2e.harness_for("lab-unknown", "x") == ("fail-unknown", "x,lab")
    scripted = re.compile(run_e2e.grep_for("scripted", None))
    assert not scripted.search("19-authorization-lab.spec.cjs yellow @lab-scripted")
    assert scripted.search("19-authorization-lab.spec.cjs no entry @scripted")
    unknown = re.compile(run_e2e.grep_for("fail-unknown", None))
    assert not unknown.search("19-authorization-lab.spec.cjs closed @lab-unknown")
    lab = re.compile(run_e2e.grep_for("lab-scripted", None))
    assert lab.search("19-authorization-lab.spec.cjs yellow @lab-scripted")
    assert not lab.search("01-transfer-tiers.spec.cjs yellow @scripted")
    assert set(run_e2e.LAB_LANES) <= set(run_e2e.MODES)


def test_the_public_lane_runs_its_own_journeys_over_tls_on_the_public_name():
    assert "public" in run_e2e.HARNESS_MODES and run_e2e.harness_for("public", "") == ("public", "")
    public = re.compile(run_e2e.grep_for("public", None))
    assert public.search("24-raw-keys.spec.cjs first screen @scripted @public")
    assert not public.search("01-transfer-tiers.spec.cjs yellow @scripted")
    assert not re.compile(run_e2e.grep_for("scripted", None)).search("banner on every page @public")
    config = (HERE / "playwright.config.cjs").read_text(encoding="utf-8")
    assert "--host-resolver-rules=" in config and "ignoreHTTPSErrors: true" in config


def test_the_public_lanes_terminator_pipes_tls_to_the_app(tmp_path):
    """The certificate names the public host and a TLS client reaches the plain port."""

    async def run() -> bytes:
        async def echo(reader, writer):
            writer.write(await reader.readline())
            await writer.drain()
            writer.close()

        plain = await asyncio.start_server(echo, "127.0.0.1", 0)
        context = harness.tls_context(tmp_path)
        tls, port = await harness.terminate_tls(plain.sockets[0].getsockname()[1], context)
        client = ssl.create_default_context(cafile=str(tmp_path / "chain.pem"))
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=client, server_hostname=harness.PUBLIC_HOST
        )
        writer.write(b"hello\n")
        await writer.drain()
        answer = await reader.readline()
        writer.close()
        tls.close()
        plain.close()
        return answer

    assert asyncio.run(run()) == b"hello\n"


def write_shot(screens: Path, entry: dict, data: bytes) -> dict:
    path = screens / entry["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    with (screens / f"manifest-{run_e2e.RUN}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def test_manifest_keeps_test_only_shots_out_of_materials(tmp_path, monkeypatch):
    monkeypatch.setattr(run_e2e, "SCREENS", tmp_path)
    base = {"project": "desktop", "width": 1, "height": 1}
    write_shot(
        tmp_path,
        {
            **base,
            "spec": "01",
            "step": "a",
            "mode": "scripted",
            "path": "01/a-desktop.png",
            "not_for_materials": False,
        },
        b"a",
    )
    write_shot(
        tmp_path,
        {
            **base,
            "spec": "10",
            "step": "b",
            "mode": "live-fake",
            "path": "test-only/10/b-desktop.png",
            "not_for_materials": True,
        },
        b"b",
    )
    assert run_e2e.build_manifest() == ([], [])
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert [item["path"] for item in manifest] == ["01/a-desktop.png", "test-only/10/b-desktop.png"]

    write_shot(
        tmp_path,
        {
            **base,
            "spec": "13",
            "step": "c",
            "mode": "fail-once",
            "path": "13/c-desktop.png",
            "not_for_materials": False,
        },
        b"c",
    )
    problems, _ = run_e2e.build_manifest()
    assert problems == ["fail-once screenshot outside test-only: 13/c-desktop.png"]
    (tmp_path / "01" / "a-desktop.png").write_bytes(b"changed")
    _, warnings = run_e2e.build_manifest()
    assert warnings == ["overwritten since recorded, dropped: 01/a-desktop.png"]
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "01/a-desktop.png" not in [item["path"] for item in manifest]


def test_harness_refuses_non_loopback_connections(monkeypatch):
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, getattr(socket.socket, name))
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    harness.loopback_only()
    with pytest.raises(OSError, match="non-loopback"):
        socket.create_connection(("203.0.113.7", 443), timeout=0.1)
    probe = socket.socket()
    try:
        with pytest.raises(OSError, match="non-loopback"):
            probe.connect(("198.51.100.1", 80))
    finally:
        probe.close()


def test_harness_clears_model_settings(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("CORRIDOR_LIVE", "1")
    monkeypatch.setenv("LIVE_ACCESS_CODE", "x")
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "judge")
    monkeypatch.setenv("CORRIDOR_LOCALE", "en")
    harness.clean_environment()
    assert "LLM_API_KEY" not in os.environ
    assert "CORRIDOR_LIVE" not in os.environ
    assert "LIVE_ACCESS_CODE" not in os.environ
    assert os.environ["CORRIDOR_DEFAULT_VIEW"] == "customer"
    assert os.environ["CORRIDOR_LOCALE"] == "zh-CN"


def test_harness_marks_every_deployment_as_a_test_fake(monkeypatch):
    from corridor.ui import live_config, live_runtime

    monkeypatch.setattr(live_config, "DEPLOYMENTS", live_config.DEPLOYMENTS)
    monkeypatch.setattr(live_config, "_BY_ID", dict(live_config._BY_ID))
    monkeypatch.setattr(live_runtime, "DEPLOYMENTS", live_runtime.DEPLOYMENTS)
    harness.relabel(live_config)
    for module in (live_config, live_runtime):
        assert all(item.label.endswith(harness.FAKE_SUFFIX) for item in module.DEPLOYMENTS)
    assert all(item.label.endswith(harness.FAKE_SUFFIX) for item in live_config._BY_ID.values())
