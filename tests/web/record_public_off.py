"""Record and replay the non-public wire golden (design §3.3, T-0; RW-8 item 4).

``record(app)`` drives one fixed journey against a served application whose settings
have no ``public`` entry and returns every answer as status, response headers, cookie
attributes and body. Run as a module it writes ``tests/web/golden/public-off-wire.json``
for the commit it ran on; ``test_public_off.py`` runs the same journey against the
current code and compares, so the non-public wire stays what the golden recorded.

Only the values the server draws from ``secrets`` are normalized, each by name: the
browser cookie value, the server-issued tab, every generation in order of appearance and
every phone pairing in order of appearance (``NORMALIZED``), and the fields keyed by the
session's secret, replaced by their key name wherever they occur (``KEYED``: the gesture
token and the card, list, gate, plan and compensation versions). Nothing else is touched:
a body whose other content moved, a header that appeared or changed, a cookie attribute
that changed, all fail the comparison. The ``Date`` header (the clock) and the static
files' ``Last-Modified`` (their time on disk, which changes with every checkout) are
dropped, and a non-JSON body is kept as its length and SHA-256. The golden also holds the
route table and the entry's ``SERVER_OPTIONS`` as they were when it was recorded.

``HISTORY`` says why the golden was recorded and every time it was recorded again; RW-9
item 7 allows that only for a deliberate change, with its reason.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.web.conftest import WebClient, serve, ws_of

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).with_name("golden") / "public-off-wire.json"
NORMALIZED = ("browser", "tab", "generation", "pair")
KEYED = (
    "token",
    "card_version",
    "compensate_version",
    "list_version",
    "gate_version",
    "plan_version",
)
# ``Last-Modified`` is the static file's time on disk: it changes with every checkout.
DROPPED_HEADERS = ("Date", "Last-Modified")
MESSAGE = {"text": "转账给李雷500元", "external_files": 0, "client_msg_id": "m-000001"}
HISTORY = (
    {
        "commit": "227358991e3527829991bf6f6e24b9485716d97c",
        "reason": "First recording, before any public-mode code existed.",
    },
    {
        "commit": "51c742f707c12f9a46a019bfbf3e9517fea70f2c",
        "changed": ["i18n"],
        "reason": (
            "RW-9 items 3 and 7: the six web.ui.login.fact_* strings of the login card were "
            "missing and the page showed their keys. Recorded on this commit with the strings "
            "added; /api/i18n is the only answer that moved."
        ),
    },
    {
        "commit": "5ddfc0802bc77613927128811286fa4d4be312ff",
        "changed": ["composer.contacts_note", "response.contact_hint"],
        "reason": (
            "RW-11 item 3 and RW-9 item 7: the page names the demo contacts beside the composer "
            "and under a transfer no contact matched. Two keys were added to the chat state, "
            "both built from trusted contacts and catalog text; no other value moved."
        ),
    },
    {
        "commit": "c3e309961ebb5be415420c72fa9e650d23610cb6",
        "changed": ["response.contact_hint"],
        "reason": (
            "RW-11 item 3 and RW-9 item 7: the hint also shows under a listed contact in a "
            "sentence the parser reads no recipient from, so its lead no longer says the address "
            "book lacks the name and its retry gives a form that works. Only the catalog text of "
            "that key moved, with the Content-Length of the answers carrying it."
        ),
    },
    {
        "commit": "d05cece8253e4e8c14767409fe2aafc87e34daec",
        "changed": ["shell.judge_strip"],
        "reason": (
            "C20 and RW-9 item 7: replace the internal freeze identifier in the bilingual "
            "judge strip with a frozen-version description and evidence-index reference. "
            "Recorded with the C20 working-tree changes; only other_open and its "
            "Content-Length change in this journey."
        ),
    },
    {
        "commit": "298f280157bde4941cd28f82a3f698aed892b24a",
        "changed": ["i18n"],
        "reason": (
            "C26 and RW-9 item 7: add the bilingual recommended-shortcuts heading "
            "and investment-page navigation label in the web view. Recorded with C26 working-tree "
            "changes; only /api/i18n and its Content-Length change in this journey."
        ),
    },
    {
        "commit": "452bbef6ed3fc293b096010c890f700ddc4830ee",
        "changed": ["i18n"],
        "reason": (
            "C33 A and RW-9 item 7: add the bilingual mobile SMS expand/collapse labels. "
            "Recorded with C33 working-tree changes; only /api/i18n and its Content-Length "
            "change in this journey. Desktop layout and task view fields are unchanged."
        ),
    },
    {
        "commit": "de50ee3710c083af610a3cc2779638702c47267d",
        "changed": ["i18n", "routes"],
        "reason": (
            "C38 and RW-12 / RW-9 item 7: add bilingual rogue demonstration strings and "
            "the session-independent read-only /api/rogue route. Recorded with C38 "
            "working-tree changes; existing banking response values are unchanged."
        ),
    },
    {
        "commit": "92810b5d62e79a344532989bc3a6ffb43a672a3f",
        "changed": ["i18n"],
        "reason": (
            "C39 and RW-9 item 7: add bilingual rogue animation, summary tiles, technical "
            "disclosure and replay labels, plus the site-only Zhipu budget note. Recorded "
            "with C39 working-tree changes; only /api/i18n and its Content-Length change. "
            "Banking response values, route table and server options are unchanged."
        ),
    },
)


class Tokens:
    """The secret-drawn values seen so far, each mapped to ``<name:n>`` by first sight."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.counts: dict[str, int] = dict.fromkeys(NORMALIZED, 0)

    def note(self, name: str, value: str | None) -> None:
        if value and value not in self.values:
            self.counts[name] += 1
            self.values[value] = f"<{name}:{self.counts[name]}>"

    def apply(self, text: str) -> str:
        # Longest first, so a value never hides inside another's replacement.
        for value in sorted(self.values, key=len, reverse=True):
            text = text.replace(value, self.values[value])
        return text


def keyed(value: Any) -> Any:
    """``value`` with every ``KEYED`` field replaced by its key name, recursively."""
    if isinstance(value, dict):
        return {
            key: f"<{key}>" if key in KEYED and isinstance(item, str) else keyed(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [keyed(item) for item in value]
    return value


def capture(label: str, response: Any, tokens: Tokens) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    for name, value in sorted(response.headers.get_all()):
        if name in DROPPED_HEADERS:
            continue
        if name in headers:
            held = headers[name]
            headers[name] = [*(held if isinstance(held, list) else [held]), value]
        else:
            headers[name] = value
    body = response.body
    if "application/json" in response.headers.get("Content-Type", ""):
        parsed = keyed(json.loads(body.decode("utf-8")))
        shown: dict[str, Any] = {
            "json": json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        }
    else:
        shown = {"length": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    _, _, rest = response.request.url.partition("//")
    return {
        "label": label,
        "method": response.request.method,
        "path": "/" + rest.partition("/")[2],
        "status": response.code,
        "headers": headers,
        "body": shown,
    }


def _note_session(tokens: Tokens, app: Any, client: WebClient) -> None:
    tokens.note("browser", client.browser)
    tokens.note("tab", client.tab)
    tokens.note("generation", client.generation)
    ws = ws_of(app, client)
    if ws is not None:
        tokens.note("pair", ws.pair)


def journey(app: Any) -> tuple[list[tuple[str, Any]], Tokens]:
    """The fixed sequence: every response kind the non-public service produces."""
    tokens = Tokens()
    client = WebClient(app)
    other = WebClient(app)
    taken: list[tuple[str, Any]] = []

    def take(label: str, response: Any) -> Any:
        taken.append((label, response))
        return response

    take("open", client.post("/api/session", {"view": "customer", "locale": "zh-CN"}))
    _note_session(tokens, app, client)
    pair = ws_of(app, client).pair
    take("reuse", client.post("/api/session", {"tab": client.tab}))
    take("login", client.post("/api/session/login", {}))
    take(
        "shortcut",
        client.post("/api/chat/shortcut", {"shortcut": "transfer", "client_msg_id": "m-000000"}),
    )
    take("message", client.post("/api/chat/message", MESSAGE))
    take("state", client.get("/api/state"))
    take("tab_operator", client.get("/api/tabs/operator"))
    take("live_panel", client.get("/api/live"))
    take("i18n", client.get("/api/i18n"))
    take("phone_api", client.get(f"/api/phone/{pair}?lang=zh-CN&view=customer"))
    take("phone_api_judge_en", client.get(f"/api/phone/{pair}?lang=en&view=judge"))
    take("phone_page", client.get(f"/phone/{pair}"))
    take("index", client.get("/"))
    take("static", client.get("/static/js/main.js"))
    take("vendor", client.get("/vendor/corridor-hmac.js"))
    take("no_session", client.get("/api/state", headers={"Cookie": None}))
    take(
        "foreign_origin",
        client.post("/api/chat/stop", {}, headers={"Origin": "http://evil.example"}),
    )
    take("not_found", client.get("/api/nothing"))
    take("lab_closed", client.get("/api/lab"))
    lines = [("Host", client.host), ("Origin", client.origin), *client.cookie().items()]
    take("method_not_allowed", client.request("PUT", "/api/state", b"{}", lines))
    take(
        "too_large",
        client.post("/api/chat/message", raw=b'{"text": "' + b"x" * (64 * 1024) + b'"}'),
    )
    take("bad_json", client.post("/api/chat/message", raw=b"{"))
    take("invalid_input", client.post("/api/chat/message", {**MESSAGE, "text": " "}))
    old = client.generation
    take("reset", client.post("/api/session/reset", {}))
    _note_session(tokens, app, client)
    take("stale", client.post("/api/chat/stop", {}, headers={"X-Corridor-Generation": old}))
    take("other_open", other.post("/api/session", {"view": "judge", "locale": "en"}))
    _note_session(tokens, app, other)
    take("other_reads_first_pair", other.get(f"/api/phone/{pair}?lang=en&view=judge"))
    take("other_cookie_first_tab", other.get("/api/state", headers={"X-Corridor-Tab": client.tab}))
    return taken, tokens


def record(app: Any) -> list[dict[str, Any]]:
    taken, tokens = journey(app)
    recorded = []
    for label, response in taken:
        shown = capture(label, response, tokens)
        text = json.dumps(shown, ensure_ascii=False)
        recorded.append(json.loads(tokens.apply(text)))
    return recorded


def golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def route_table() -> list[list[Any]]:
    """The route table as ``[pattern, handler, route template]`` rows, in order."""
    from corridor.web.app import routes

    return [[pattern, cls.__name__, options.get("route")] for pattern, cls, options in routes()]


def server_options() -> dict[str, Any]:
    from corridor.web.app import SERVER_OPTIONS

    return dict(SERVER_OPTIONS)


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    # The same environment the web tests run under (tests/conftest.py, no_network).
    for name in list(os.environ):
        if name.startswith(("LLM_", "CORRIDOR_LIVE", "LIVE_")):
            del os.environ[name]
    os.environ.setdefault("CORRIDOR_DEFAULT_VIEW", "judge")
    os.environ["CORRIDOR_LOCALE"] = "zh-CN"
    with serve({}) as app:
        recorded = record(app)
    document = {
        "recorded_at_commit": _commit(),
        "history": list(HISTORY),
        "script": "tests/web/record_public_off.py",
        "normalized": list(NORMALIZED),
        "keyed": list(KEYED),
        "dropped_headers": list(DROPPED_HEADERS),
        "server_options": server_options(),
        "routes": route_table(),
        "responses": recorded,
    }
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(
        json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    sys.stdout.write(f"{len(recorded)} responses -> {GOLDEN.relative_to(ROOT).as_posix()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
