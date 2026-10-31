"""Plain text for view models: catalog copy may carry HTML or Markdown; pages get neither."""

import re
from html import unescape
from typing import Any

_TAG = re.compile(r"<[^<>]*>")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def plain(value: Any) -> str:
    """Strip tags, Markdown emphasis, headings and links, then unescape entities."""
    text = "" if value is None else str(value)
    text = _BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = _HEADING.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = text.replace("**", "")
    return unescape(text).strip()


def lines(value: Any) -> list[str]:
    """Plain text split into its non-empty lines."""
    return [line.strip() for line in plain(value).splitlines() if line.strip()]
