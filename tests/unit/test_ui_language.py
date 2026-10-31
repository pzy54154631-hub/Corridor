"""Domestic text lives in complete bilingual display catalogs."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CJK = re.compile(r"[\u4e00-\u9fff]")


def test_ui_copy_is_kept_in_bilingual_catalogs():
    paths = sorted((ROOT / "src/corridor/ui").rglob("*.py"))
    paths.append(ROOT / "src/corridor/gateway/facts.py")
    violations = []
    for path in paths:
        if path.name == "messages.py":
            continue
        source = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(source):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and CJK.search(node.value)
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not violations, "UI translations outside catalogs: " + ", ".join(violations)


def test_bilingual_catalog_entries_have_both_locales():
    from corridor.messages import MESSAGES
    from corridor.ui.messages import UI_MESSAGES

    for catalog in (MESSAGES, UI_MESSAGES):
        assert catalog
        for key, translations in catalog.items():
            assert set(translations) == {"zh-CN", "en"}, key
            assert all(isinstance(value, str) and value for value in translations.values()), key
