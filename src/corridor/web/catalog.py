"""Bilingual web catalogs: shell copy (``web.ui.*``) and view-model copy (``web.vm.*``).

Each prefix lives in its own file. Every key carries both locales with the same
placeholders, keeps its file's prefix (a ``customer:`` override keeps it too and needs
its base key) and never shadows a UI catalog key; any violation fails at import, so a
broken catalog never reaches a page. Lookups fall back to the UI catalog's ``tr``.
"""

import json
import string
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from corridor.i18n import LOCALES
from corridor.web.compat import (
    UI_MESSAGES,
    get_audience,
    get_locale,
    reset_audience,
    reset_locale,
    set_audience,
    set_locale,
    tr,
)

OVERRIDE = "customer:"
FILES = {"web.ui.": "messages-web-ui.json", "web.vm.": "messages-web-vm.json"}


class CatalogError(ValueError):
    pass


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise CatalogError(f"Duplicate web catalog key: {key}")
        found[key] = value
    return found


def _no_number(value: str) -> Any:
    raise CatalogError("Web catalogs hold text only")


def placeholders(text: str) -> frozenset[str]:
    return frozenset(name for _, name, _, _ in string.Formatter().parse(text) if name is not None)


def check(entries: dict[str, Any], prefix: str) -> dict[str, dict[str, str]]:
    """Validate one catalog file's entries against its prefix and the UI catalog."""
    for key, value in entries.items():
        base = key.removeprefix(OVERRIDE)
        if not base.startswith(prefix):
            raise CatalogError(f"Web catalog key outside {prefix}*: {key}")
        if key in UI_MESSAGES or base in UI_MESSAGES:
            raise CatalogError(f"Web catalog key shadows a UI key: {key}")
        if not isinstance(value, dict) or set(value) != set(LOCALES):
            raise CatalogError(f"Web catalog key needs exactly {LOCALES}: {key}")
        if not all(isinstance(text, str) and text.strip() for text in value.values()):
            raise CatalogError(f"Web catalog text is empty: {key}")
        names = {placeholders(text) for text in value.values()}
        if len(names) != 1:
            raise CatalogError(f"Web catalog placeholders differ between locales: {key}")
        if key != base:
            if base not in entries:
                raise CatalogError(f"Override without its base key: {key}")
            if placeholders(entries[base][LOCALES[0]]) != next(iter(names)):
                raise CatalogError(f"Override placeholders differ from its base key: {key}")
    return entries


def load(path: Path, prefix: str) -> dict[str, dict[str, str]]:
    entries = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique,
        parse_float=_no_number,
        parse_int=_no_number,
        parse_constant=_no_number,
    )
    if not isinstance(entries, dict):
        raise CatalogError(f"Web catalog is not an object: {path.name}")
    return check(entries, prefix)


def _load_all() -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for prefix, name in FILES.items():
        path = Path(__file__).with_name(name)
        if not path.exists():
            # The shell catalog belongs to another line; the view models never need it.
            continue
        for key, value in load(path, prefix).items():
            if key in merged:
                raise CatalogError(f"Web catalog key defined twice: {key}")
            merged[key] = value
    return merged


WEB_MESSAGES = _load_all()


def wtr(key: str, **values: object) -> str:
    """A ``customer:`` override in the customer view, then the web key, then ``ui`` copy."""
    if key in WEB_MESSAGES:
        override = OVERRIDE + key
        if get_audience() == "customer" and override in WEB_MESSAGES:
            key = override
        return WEB_MESSAGES[key][get_locale()].format(**values)
    return tr(key, **values)


def keys(prefix: str) -> list[str]:
    """Base keys (no overrides) under one prefix, sorted."""
    return sorted(
        key for key in WEB_MESSAGES if key.startswith(prefix) and not key.startswith(OVERRIDE)
    )


def template(key: str) -> str:
    """A key's unformatted text for this locale and audience; placeholders are kept."""
    for catalog in (WEB_MESSAGES, UI_MESSAGES):
        if key in catalog:
            override = OVERRIDE + key
            if get_audience() == "customer" and override in catalog:
                key = override
            return catalog[key][get_locale()]
    return key


@contextmanager
def bound(ws) -> Generator[None, None, None]:
    """The session's locale and audience for one call, restored afterwards.

    The audience defaults to the judge view when unset, so every view-model entry point
    binds it from the session instead of trusting the caller.
    """
    locale, audience = set_locale(ws.locale), set_audience(ws.view)
    try:
        yield
    finally:
        reset_audience(audience)
        reset_locale(locale)
