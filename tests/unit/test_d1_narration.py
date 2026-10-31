"""Keep the Chinese draft validation offline and block unresolved narration."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not (ROOT / "scripts/demo/narration_zh.py").is_file(),
    reason="the video narration tools are not distributed with the source package",
)


@pytest.fixture
def narrator(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/demo"))
    spec = importlib.util.spec_from_file_location(
        "d1_narrator", ROOT / "scripts/demo/narration_zh.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_draft_validation_never_reads_credentials(narrator, monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Offline validation must not read credentials or call TTS")

    monkeypatch.setattr(narrator, "read_key", forbidden)
    monkeypatch.setattr(narrator, "request", forbidden)
    narrator.generate(
        SimpleNamespace(script=ROOT / "scripts/demo/narration_zh.json", validate_only=True)
    )
    report = json.loads(capsys.readouterr().out)
    assert report["network_calls"] == 0
    assert report["planned_seconds"] == 280


def test_unresolved_draft_cannot_generate_audio(narrator, tmp_path):
    script = narrator.read_json(ROOT / "scripts/demo/narration_zh.json")
    script["render_status"] = "draft_not_rendered"
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(narrator.SafeFailure, match="not ready"):
        narrator.generate(SimpleNamespace(script=path, validate_only=False))


def test_unfilled_flagship_verdict_alone_still_blocks_audio(narrator, monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("TTS must stay blocked while the flagship verdict is unfilled")

    monkeypatch.setattr(narrator, "read_key", forbidden)
    monkeypatch.setattr(narrator, "request", forbidden)
    text = (ROOT / "scripts/demo/narration_zh.json").read_text(encoding="utf-8")
    script = json.loads(text.replace("{{TEAM_NAME}}", "Demo Team"))
    script["render_status"] = "ready"
    script["segments"][-1]["text"] = script["segments"][-1]["text"].replace(
        "两侧结果不同，差异照实保留，见技术文档第07章。", "{{FLAGSHIP_VERDICT}}"
    )
    assert json.dumps(script, ensure_ascii=False).count("{{") == 1
    assert "{{FLAGSHIP_VERDICT}}" in script["segments"][-1]["text"]
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(narrator.SafeFailure, match="not ready"):
        narrator.generate(
            SimpleNamespace(
                script=path, validate_only=False, env_file=tmp_path / "x", output_dir=tmp_path
            )
        )


def test_narration_rejects_excess_time_and_discontinuous_timeline(narrator):
    script = narrator.read_json(ROOT / "scripts/demo/narration_zh.json")
    script["segments"][-1]["seconds"] += 6
    script["segments"][-1]["end"] += 6
    with pytest.raises(narrator.SafeFailure, match="4:45"):
        narrator.validate_timeline(script)
    script = narrator.read_json(ROOT / "scripts/demo/narration_zh.json")
    script["segments"][1]["start"] += 1
    with pytest.raises(narrator.SafeFailure, match="continuous"):
        narrator.validate_timeline(script)
