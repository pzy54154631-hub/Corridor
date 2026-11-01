"""The deployment checker uses the real product with an offline client."""

import json

from corridor.agent.llm import LLMClient
from corridor.ui.live_config import Credential
from scripts.public import check_live_deployment as runner


def test_six_messages_use_the_product_without_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "read_credential", lambda path: Credential("test-secret"))
    configurations = []

    def factory(**kwargs):
        configurations.append(kwargs)
        return LLMClient(mode="scripted", env_file=None, context_guard=kwargs["context_guard"])

    output = tmp_path / "check.json"
    result = runner.check("deepseek-v4-pro", tmp_path, output, client_factory=factory)
    assert len(result["rows"]) == len(configurations) == 6
    assert all(row["error"] is None for row in result["rows"])
    assert all(row["card"] or row["answer"] for row in result["rows"])
    assert all(row["api_calls"] == 0 and row["cost"] == "0/1" for row in result["rows"])
    assert all(c["model"] == "deepseek-v4-pro" for c in configurations)
    assert "test-secret" not in output.read_text(encoding="utf-8")
    assert json.loads(output.read_text(encoding="utf-8")) == result


def test_provider_failure_never_prints_its_message(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "read_credential", lambda path: Credential("test-secret"))

    def broken(**kwargs):
        raise RuntimeError("test-secret")

    output = tmp_path / "failed.json"
    result = runner.check("deepseek-flash", tmp_path, output, client_factory=broken)
    assert all(row["error"] == "RuntimeError" for row in result["rows"])
    assert "test-secret" not in output.read_text(encoding="utf-8")
