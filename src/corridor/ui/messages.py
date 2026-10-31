"""Bilingual display catalog; canonical facts and identifiers are never translated."""

from pathlib import Path
from typing import Any, cast

from corridor.i18n import tr as core_tr
from corridor.i18n import translated_literal
from corridor.schemas.base import loads_exact
from corridor.ui.audience import get_audience

UI_MESSAGES = cast(
    dict[str, dict[str, str]], loads_exact(Path(__file__).with_suffix(".json").read_bytes())
)
for _fragment in sorted(Path(__file__).parent.glob("messages-*.json")):
    for _key, _translations in cast(
        dict[str, dict[str, str]], loads_exact(_fragment.read_bytes())
    ).items():
        if _key in UI_MESSAGES and UI_MESSAGES[_key] != _translations:
            raise ValueError(f"Conflicting UI translation: {_key}")
        UI_MESSAGES[_key] = _translations


def tr(key: str, **values: object) -> str:
    """Render fixed UI copy in the current request's locale and display audience."""
    if key in UI_MESSAGES:
        override = "customer:" + key
        if override in UI_MESSAGES and get_audience() == "customer":
            return core_tr(override, catalog=UI_MESSAGES, **values)
        return core_tr(key, catalog=UI_MESSAGES, **values)
    return translated_literal(key)


def source_utterance(key: str) -> str:
    """Return a fixed translated demo command, independent of display locale.

    Button commands use D2's supported Chinese vocabulary. This never translates
    arbitrary user text or changes a device-confirmed financial value.
    """
    return UI_MESSAGES[key]["zh-CN"]


def contact_aliases(group: str) -> frozenset[str]:
    """Read exact display-name aliases across locales, never authorization inputs."""
    return frozenset(
        alias
        for names in UI_MESSAGES[f"contact_aliases.{group}"].values()
        for alias in names.split("|")
        if alias
    )


def prose(value: str) -> str:
    """Localize fixed dialogue clauses; never interpret financial values or model notes."""
    translated = tr(value)
    if translated != value:
        return translated
    for key in sorted(UI_MESSAGES, key=len, reverse=True):
        if len(key) > 15 and value.startswith(key) and len(value) > len(key):
            remainder = value[len(key) :]
            whitespace = remainder[: len(remainder) - len(remainder.lstrip())]
            return tr(key) + whitespace + prose(remainder.lstrip())
    return value


def display_value(value: Any) -> Any:
    """Project display labels without changing the stored facts."""
    if isinstance(value, str):
        return tr(value)
    if isinstance(value, tuple):
        return tuple(display_value(item) for item in value)
    if isinstance(value, list):
        return [display_value(item) for item in value]
    return value
