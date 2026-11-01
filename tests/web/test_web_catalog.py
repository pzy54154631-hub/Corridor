"""B2: the web catalogs (contract §8, design §4.5, H8, H13, INV-5.4, INV-6.1, INV-6.4).

Prefixes are split by line, every key is bilingual with the same placeholders, no key
shadows a UI key, and no copy makes a forbidden claim. Customer-visible copy
carries no internal words. The view-model sources hold no CJK literals.
"""

import ast
import json
import re
from pathlib import Path

import pytest

from corridor.ui.messages import UI_MESSAGES
from corridor.web import catalog

WEB = Path(catalog.__file__).parent
FILES = {"web.ui.": WEB / "messages-web-ui.json", "web.vm.": WEB / "messages-web-vm.json"}
OWN = [WEB / "catalog.py", WEB / "contracts.py", *sorted((WEB / "vm").glob("*.py"))]
# INV-6.1 (the "do not write" list), INV-6.6 (S7) and H13.
FORBIDDEN = (
    "零副作用",
    "拦截率",
    "识别诈骗",
    "延迟的是确认手势",
    "确定性风控",
    "已在真模型上验收",
    "密钥受硬件保护",
    "花费为账单金额",
    "每日上限",
    "超时会回退",
    "录制文件即被采用",
    "保证拦截",
    "已核实收款人",
    "已核验收款人",
    "硬件隔离",
    "证明本人点击",
    "证明真人点击",
    "绝不超支",
    # R1: the web shows the simulated SMS beside the code field; it is no independent channel.
    "独立",
    "zero side effect",
    "interception rate",
    "block rate",
    "identify fraud",
    "detects fraud",
    "deterministic risk control",
    "validated on a real model",
    "accepted on a real model",
    "protected by hardware",
    "daily cap",
    "falls back on timeout",
    "guaranteed to block",
    "guarantees interception",
    "verified recipient",
    "hardware isolation",
    "hardware-isolated",
    "proves the user clicked",
    "never overspend",
    "independent",
)
# INV-5.4: words the customer view never shows ("frozen kernel" stays in the S6 footer).
INTERNAL = ("门控", "规范卡", "签名载荷", "审计链", "前置条件", "展示层", "scripted")
JUDGE_ONLY = (
    "web.vm.evidence.",
    "web.vm.hash.",
    "web.ui.evidence.",
    "web.ui.hash.",
    "web.ui.judge.",
)
CJK = re.compile(r"[㐀-䶿一-鿿　-〿＀-￯]")


def entries():
    return [
        (key, locale, text)
        for prefix, path in FILES.items()
        if path.exists()
        for key, value in catalog.load(path, prefix).items()
        for locale, text in value.items()
    ]


def customer_visible(key):
    base = key.removeprefix(catalog.OVERRIDE)
    if base.startswith(JUDGE_ONLY):
        return False
    return key.startswith(catalog.OVERRIDE) or catalog.OVERRIDE + key not in catalog.WEB_MESSAGES


def test_each_catalog_keeps_its_own_prefix_and_both_locales():
    for prefix, path in FILES.items():
        if not path.exists():
            continue
        loaded = catalog.load(path, prefix)
        assert loaded
        for key, value in loaded.items():
            assert key.removeprefix(catalog.OVERRIDE).startswith(prefix), key
            assert set(value) == {"zh-CN", "en"} and all(text.strip() for text in value.values())
    assert set(catalog.WEB_MESSAGES) >= set(catalog.load(FILES["web.vm."], "web.vm."))


def test_no_web_key_shadows_a_ui_key():
    assert not set(catalog.WEB_MESSAGES) & set(UI_MESSAGES)
    bases = {key.removeprefix(catalog.OVERRIDE) for key in catalog.WEB_MESSAGES}
    assert not bases & set(UI_MESSAGES)


@pytest.mark.parametrize(
    ("entries_", "fault"),
    [
        ({"web.vm.a": {"zh-CN": "一"}}, "exactly"),
        ({"web.vm.a": {"zh-CN": "{x}", "en": "{y}"}}, "placeholders"),
        ({"web.ui.a": {"zh-CN": "一", "en": "a"}}, "outside"),
        ({"customer:web.vm.a": {"zh-CN": "一", "en": "a"}}, "base key"),
        ({"web.vm.a": {"zh-CN": " ", "en": "a"}}, "empty"),
        (
            {
                "web.vm.a": {"zh-CN": "{x}", "en": "{x}"},
                "customer:web.vm.a": {"zh-CN": "一", "en": "a"},
            },
            "Override placeholders",
        ),
    ],
)
def test_a_broken_catalog_fails_at_load(entries_, fault):
    with pytest.raises(catalog.CatalogError, match=fault):
        catalog.check(entries_, "web.vm.")


def test_a_shadowed_ui_key_fails_at_load(monkeypatch):
    monkeypatch.setitem(UI_MESSAGES, "web.vm.shadow", {"zh-CN": "一", "en": "a"})
    with pytest.raises(catalog.CatalogError, match="shadows"):
        catalog.check({"web.vm.shadow": {"zh-CN": "一", "en": "a"}}, "web.vm.")


@pytest.mark.parametrize(
    "raw",
    [
        '{"web.vm.a": {"zh-CN": "一", "en": "a"}, "web.vm.a": {"zh-CN": "二", "en": "b"}}',
        '{"web.vm.a": {"zh-CN": 1.5, "en": "a"}}',
        '{"web.vm.a": {"zh-CN": 1, "en": "a"}}',
    ],
)
def test_duplicate_keys_and_numbers_fail_at_load(raw, tmp_path):
    path = tmp_path / "messages-web-vm.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(catalog.CatalogError):
        catalog.load(path, "web.vm.")


def test_no_copy_makes_a_forbidden_claim():
    for key, _locale, text in entries():
        lowered = text.lower()
        for phrase in FORBIDDEN:
            assert phrase.lower() not in lowered, (key, phrase)


def test_verified_appears_only_as_the_users_own_statement():
    for key, _locale, text in entries():
        for sentence in re.split(r"[。；！？\n]", text):
            if "已核实" in sentence:
                assert "（用户自述）" in sentence, (key, sentence)


def test_customer_visible_copy_has_no_internal_words():
    for key, _locale, text in entries():
        if not customer_visible(key):
            continue
        for word in INTERNAL:
            assert word.lower() not in text.lower(), (key, word)
        assert "canonical card" not in text.lower(), key


def test_the_view_model_copy_names_self_reports_as_such():
    assert "自述" in catalog.WEB_MESSAGES["web.vm.gate.self_report"]["zh-CN"]
    assert "own statement" in catalog.WEB_MESSAGES["web.vm.gate.self_report"]["en"]
    note = catalog.WEB_MESSAGES["web.vm.composer.attach_note"]
    assert "自报" in note["zh-CN"] and "as you report it" in note["en"]


def test_lookup_falls_back_from_the_override_to_the_web_key_to_the_ui_catalog():
    from corridor.i18n import reset_locale, set_locale
    from corridor.ui.audience import reset_audience, set_audience

    tokens = (set_locale("zh-CN"), set_audience("customer"))
    try:
        assert catalog.wtr("web.vm.device.title") == "设备确认（模拟）"
        assert catalog.wtr("web.vm.not_signed") == "不属于签名内容"
        assert catalog.wtr("Cancel") == "取消"
        assert catalog.wtr("web.vm.device.queue", count=2) == "待确认队列：另有 2 项"
    finally:
        reset_audience(tokens[1])
        reset_locale(tokens[0])


def test_view_model_sources_hold_no_cjk_literals():
    for path in OWN:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not CJK.search(node.value), (path.name, node.value)


def test_the_view_model_catalog_is_plain_text():
    raw = json.loads(FILES["web.vm."].read_text(encoding="utf-8"))
    for key, value in raw.items():
        for text in value.values():
            assert not re.search(r"<[a-z/][^>]*>|\*\*|^#", text), key


def test_every_device_step_and_reply_status_has_its_own_words():
    from typing import get_args

    from corridor.i18n import reset_locale, set_locale
    from corridor.web.contracts import Step
    from corridor.web.vm.chat import status_text
    from corridor.web.vm.device import step_text

    for locale in ("zh-CN", "en"):
        token = set_locale(locale)
        try:
            for step in get_args(Step):
                text = step_text(step)
                assert text and not text.startswith(("web.vm.", "live.")), (locale, step)
            assert status_text("settled") != status_text("no_such_status")
        finally:
            reset_locale(token)
