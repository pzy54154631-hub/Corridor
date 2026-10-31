"""Source guards of the web package (design §2.1, §6.7, H4, H8).

Python sources are checked on their syntax tree; the static front end by patterns. Tests
with ``static`` in their name cover ``src/corridor/web/static``.
"""

import ast
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from tests._ast_checks import WALLCLOCK, Analysis

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "corridor"
WEB = SOURCE / "web"
STATIC = WEB / "static"
CJK = re.compile(r"[一-鿿]")  # the same pattern as tests/unit/test_ui_language.py

HOST_CLOCK = WALLCLOCK | {
    "time.gmtime",
    "time.localtime",
    "time.ctime",
    "time.asctime",
    "time.strftime",
    "time.monotonic",
    "time.monotonic_ns",
    "time.perf_counter",
    "time.perf_counter_ns",
    "time.process_time",
    "time.process_time_ns",
    "datetime.datetime.fromtimestamp",
    "datetime.date.fromtimestamp",
}
# Design §2.1: what the web package may import from corridor. ``schemas.money`` is a pure
# display formatter used by live.py; everything else in the trusted core stays out.
ALLOWED = (
    "corridor.web",
    "corridor.ui",
    "corridor.i18n",
    "corridor.agent.domestic_session",
    "corridor.agent.llm",
    "corridor.channels.im",
    "corridor.crypto.jcs",
    "corridor.sim.keys",
    "corridor.schemas.money",
)
FORBIDDEN = (
    "corridor.sim.device",
    "corridor.sim.device_workflow",
    "corridor.sim.wealth_device",
    "corridor.gateway",
    "corridor.eval",
    "corridor.bench",
    "corridor.kernel",
    "httpx",
    "litellm",
    "openai",
    "anthropic",
    "requests",
    "streamlit",
    # Identifiers come from ``secrets`` only (H4); no network clients or processes.
    "random",
    "uuid",
    "socket",
    "subprocess",
    "urllib",
    "http.client",
    "pickle",
)
THIRD_PARTY = ("tornado",)


def _within(name, prefix):
    return name == prefix or name.startswith(prefix + ".")


def web_sources():
    return sorted(WEB.rglob("*.py"))


def module_of(path):
    parts = path.relative_to(SOURCE.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def analyses():
    for path in web_sources():
        yield path, Analysis(path.read_text(encoding="utf-8"), module_of(path))


def calls(tree):
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def attribute(node):
    return node.func.attr if isinstance(node.func, ast.Attribute) else None


def static_files(*suffixes):
    return sorted(path for path in STATIC.rglob("*") if path.suffix in suffixes)


def relative(path):
    return path.relative_to(ROOT).as_posix()


# --- Python ---------------------------------------------------------------------------------


def test_the_web_package_has_no_chinese_literals():
    found = []
    for path, analysis in analyses():
        for node in ast.walk(analysis.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if CJK.search(node.value):
                    found.append(f"{relative(path)}:{node.lineno}")
    assert not found, "copy belongs in the web catalogs: " + ", ".join(found)


def test_the_web_package_reads_no_host_clock():
    """One exception, reviewed: public mode's ``public.now`` reads the monotonic clock for
    its refill and idle intervals (RW-8 items 1, 2, 7). It is the only clock call in the
    package, it is never the wall clock, and nothing derives an identifier from it."""
    found = []
    monotonic = []
    for path, analysis in analyses():
        for node in calls(analysis.tree):
            name = analysis.name(node.func)
            if name == "time.monotonic" and path.name == "public.py":
                monotonic.append(node.lineno)
            elif name in HOST_CLOCK:
                found.append(f"{relative(path)}:{node.lineno}: {name}")
    assert not found, found
    tree = ast.parse((WEB / "public.py").read_text(encoding="utf-8"))
    (now,) = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "now"
    ]
    assert len(monotonic) == 1 and now.lineno <= monotonic[0] <= now.end_lineno, monotonic


@pytest.mark.parametrize(
    "source",
    [
        "import time; time.monotonic()",
        "from time import perf_counter as clock; clock()",
        "import datetime; datetime.datetime.now()",
        "from uuid import uuid4; uuid4()",
    ],
)
def test_the_clock_check_sees_aliases(source):
    analysis = Analysis(source)
    assert any(analysis.name(node.func) in HOST_CLOCK for node in calls(analysis.tree))


def boundary_violations(name):
    if name is None:
        return "computed import"
    if any(_within(name, prefix) for prefix in FORBIDDEN):
        return "forbidden"
    top = name.split(".")[0]
    if top == "corridor":
        if name == "corridor" or any(_within(name, prefix) for prefix in ALLOWED):
            return None
        return "outside the allowed corridor modules"
    if top in sys.stdlib_module_names or top in THIRD_PARTY:
        return None
    return "third-party module"


def test_the_web_package_imports_only_its_allowed_dependencies():
    found = []
    for path, analysis in analyses():
        for reference in analysis.imports:
            problem = boundary_violations(reference.name)
            if problem:
                found.append(f"{relative(path)}:{reference.lineno}: {reference.name} ({problem})")
    assert not found, found


@pytest.mark.parametrize(
    "name",
    [
        "corridor.sim.device",
        "corridor.sim.device_workflow.approve",
        "corridor.gateway.mfa",
        "corridor.eval.mockbank.exec",
        "corridor.bench.baseline_exec",
        "corridor.kernel.check",
        "httpx",
        "litellm",
        "random",
        "uuid",
        "streamlit",
        "pydantic",
        None,
    ],
)
def test_the_boundary_check_refuses(name):
    assert boundary_violations(name)


def test_nothing_outside_the_web_package_imports_it():
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        if _within(module_of(path), "corridor.web"):
            continue
        analysis = Analysis(path.read_text(encoding="utf-8"), module_of(path))
        for reference in analysis.imports:
            if reference.name and _within(reference.name, "corridor.web"):
                found.append(f"{relative(path)}:{reference.lineno}")
    assert not found, found


def mfa_violations(path, tree):
    found = []
    for node in calls(tree):
        name = attribute(node)
        where = f"{relative(path)}:{node.lineno}"
        if name == "complete_mfa":
            codes = [keyword for keyword in node.keywords if keyword.arg == "code"]
            unpacked = any(keyword.arg is None for keyword in node.keywords)
            if (
                not codes
                or unpacked
                or (isinstance(codes[0].value, ast.Constant) and codes[0].value.value is None)
            ):
                found.append(f"{where}: complete_mfa without a typed code")
        if name == "verify_leg":
            found.append(f"{where}: verify_leg")
        if name == "click":
            first = node.args[0] if node.args else None
            literal = first.value if isinstance(first, ast.Constant) else None
            if literal not in ("confirm", "verify_mfa"):
                found.append(f"{where}: click({ast.unparse(first) if first else ''})")
    return found


def verify_sites(path, tree):
    """(function, statement index, body) of every verify_mfa call or click("verify_mfa")."""
    sites = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for body in (
            node_body
            for node in ast.walk(function)
            for node_body in (getattr(node, "body", None), getattr(node, "orelse", None))
            if isinstance(node_body, list)
        ):
            for index, statement in enumerate(body):
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in calls(statement):
                    first = node.args[0] if node.args else None
                    if attribute(node) == "verify_mfa" or (
                        attribute(node) == "click"
                        and isinstance(first, ast.Constant)
                        and first.value == "verify_mfa"
                    ):
                        sites.append((function.name, index, body))
    return sites


def follows_the_verified_check(body, index):
    """The sync follows an explicit ``if ... pending_verified(...) ...: raise``.

    An ``assert`` does not count: ``python -O`` removes it (R1-E4).
    """
    if index == 0 or not isinstance(body[index - 1], ast.If):
        return False
    check = body[index - 1]
    raises = bool(check.body) and all(isinstance(node, ast.Raise) for node in check.body)
    return raises and any(
        (isinstance(node.func, ast.Attribute) and node.func.attr == "pending_verified")
        or (isinstance(node.func, ast.Name) and node.func.id == "pending_verified")
        for node in calls(check.test)
    )


def test_mfa_calls_always_carry_a_typed_code_and_clicks_only_confirm():
    found = []
    sites = []
    for path, analysis in analyses():
        found += mfa_violations(path, analysis.tree)
        sites += [(path, *site) for site in verify_sites(path, analysis.tree)]
    for path, function, index, body in sites:
        if path.name != "service.py" or not follows_the_verified_check(body, index):
            found.append(f"{relative(path)}:{function}: verify sync without the explicit check")
    assert len({(path, function) for path, function, *_ in sites}) <= 1, sites
    assert not found, found


@pytest.mark.parametrize(
    "source",
    [
        "controller.complete_mfa(pid)",
        "controller.complete_mfa(pid, code=None)",
        "controller.complete_mfa(pid, **options)",
        "workflow.verify_leg(leg)",
        "adapter.click(operation, rid)",
        "adapter.click('cancel', rid)",
    ],
)
def test_the_mfa_check_refuses(source):
    assert mfa_violations(Path(WEB / "x.py"), ast.parse(source))


def test_the_verify_sync_must_follow_an_explicit_check():
    good = ast.parse(
        "def sync(ws, task):\n"
        "    if compat.pending_verified(ws.controller, task.identity) is not True:\n"
        "        raise _fail('outcome_unknown')\n"
        "    compat.IMAdapter(ws.session).click('verify_mfa', task.rid)\n"
    )
    asserted = ast.parse(
        "def sync(ws, task):\n"
        "    assert compat.pending_verified(ws.controller, task.identity) is True\n"
        "    compat.IMAdapter(ws.session).click('verify_mfa', task.rid)\n"
    )
    bad = ast.parse(
        "def sync(ws, task):\n    compat.IMAdapter(ws.session).click('verify_mfa', 1)\n"
    )
    ((_, index, body),) = verify_sites(WEB / "service.py", good)
    assert follows_the_verified_check(body, index)
    for source in (asserted, bad):
        ((_, index, body),) = verify_sites(WEB / "service.py", source)
        assert not follows_the_verified_check(body, index)


def test_limits_are_read_from_their_modules_not_written_down():
    found = []
    for path, analysis in analyses():
        for node in ast.walk(analysis.tree):
            if isinstance(node, ast.Constant) and type(node.value) is int:
                if node.value in (16384, 8192):
                    found.append(f"{relative(path)}:{node.lineno}")
    for path in static_files(".js", ".html"):
        if re.search(r"\b(?:16384|8192)\b", path.read_text(encoding="utf-8")):
            found.append(relative(path))
    assert not found, found


def test_the_web_package_never_reads_env_files():
    found = []
    for path, analysis in analyses():
        for reference in analysis.imports:
            if reference.name and _within(reference.name, "dotenv"):
                found.append(f"{relative(path)}:{reference.lineno}")
        for node in ast.walk(analysis.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(r"(^|[\\/])\.env\b", node.value):
                    found.append(f"{relative(path)}:{node.lineno}")
    assert not found, found


def test_the_application_runs_without_debug_or_signed_cookies():
    from corridor.web.app import make_app
    from tests.web.conftest import Doubles

    settings = make_app(Doubles().settings(port=1)).settings
    for name in ("debug", "autoreload", "serve_traceback", "xsrf_cookies"):
        assert settings[name] is False, name
    assert "cookie_secret" not in settings and settings["log_function"].__module__ == (
        "corridor.web.logs"
    )
    settings["executor"].shutdown()


# --- static front end -----------------------------------------------------------------------

STATIC_FORBIDDEN = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "Date(",
    "Date.now",
    "@import",
    "http://",
    "https://",
    "javascript:",
    'setAttribute("style"',
    "setAttribute('style'",
)


def test_static_files_use_no_unsafe_sinks_clocks_or_external_urls():
    found = []
    for path in static_files(".js", ".html", ".css"):
        text = path.read_text(encoding="utf-8")
        for pattern in STATIC_FORBIDDEN:
            if pattern in text:
                found.append(f"{relative(path)}: {pattern}")
    assert not found, found


def test_static_files_have_no_chinese_text():
    found = [
        f"{relative(path)}:{number}"
        for path in static_files(".js", ".html", ".css")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if CJK.search(line)
    ]
    assert not found, "copy comes from /api/i18n and AppState: " + ", ".join(found)


def test_static_pages_load_scripts_and_styles_from_files_only():
    found = []
    for path in static_files(".html"):
        text = path.read_text(encoding="utf-8")
        for script in re.finditer(r"<script\b([^>]*)>(.*?)</script\s*>", text, re.S | re.I):
            attributes, inline = script.groups()
            if inline.strip() or "src=" not in attributes:
                found.append(f"{relative(path)}: inline script")
        if re.search(r"<style\b", text, re.I):
            found.append(f"{relative(path)}: style element")
        if re.search(r"\sstyle\s*=", text, re.I):
            found.append(f"{relative(path)}: style attribute")
        if re.search(r"\son[a-z]+\s*=", text, re.I):
            found.append(f"{relative(path)}: inline event handler")
        for link in re.findall(r"""(?:src|href)\s*=\s*["']([^"']*)["']""", text, re.I):
            if not link.startswith("/") or link.startswith("//"):
                found.append(f"{relative(path)}: {link}")
    assert not found, found


def test_static_phone_and_main_page_keep_to_their_documents():
    found = []
    for path in static_files(".js"):
        text = path.read_text(encoding="utf-8")
        if path.name == "sms.js":
            found += [
                f"{relative(path)}: {word}"
                for word in ("postMessage", "parent.", "top.", "opener")
                if word in text
            ]
        else:
            found += [
                f"{relative(path)}: {word}"
                for word in ("contentDocument", "contentWindow")
                if word in text
            ]
    assert not found, found


CONTRACT_ATTRIBUTES = {
    "class",
    "id",
    "role",
    "type",
    "name",
    "value",
    "checked",
    "disabled",
    "for",
    "inputmode",
    "autocomplete",
    "maxlength",
    "tabindex",
    "title",
    "lang",
    "href",
    "src",
}
DOM_PROBE = r"""
const recorded = [];
class Node {}
function element(tag) {
  const target = Object.assign(new Node(), {
    tag, children: [], attributes: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    addEventListener(type, fn) { this.attributes["listener:" + type] = typeof fn; },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; },
  });
  const proxy = new Proxy(target, {
    set(object, key, value) {
      object[key] = value;
      recorded.push([tag, String(key)]);
      return true;
    },
  });
  return proxy;
}
globalThis.Node = Node;
globalThis.DOMParser = class {
  parseFromString() { return { body: { firstElementChild: { namespaceURI: "svg" } } }; }
};
globalThis.document = {
  createElement: element,
  createElementNS: (ns, tag) => element(tag),
  createTextNode: (text) => Object.assign(new Node(), { text }),
};
const { h } = await import(process.argv[2]);
const cases = JSON.parse(process.argv[3]);
const results = cases.map(([tag, attrs, child]) => {
  try {
    const el = h(tag, attrs, child);
    return { attributes: el.attributes, children: el.children.map((c) => c.text ?? null) };
  } catch (error) {
    return { refused: String(error && error.name) };
  }
});
console.log(JSON.stringify({ results, recorded }));
"""


def test_static_dom_builder_accepts_only_whitelisted_attributes_and_same_origin_paths(
    tmp_path,
):
    dom = STATIC / "js" / "dom.js"
    node = shutil.which("node")
    if not dom.exists() or node is None:
        pytest.skip("static/js/dom.js or node is not available")
    cases = [
        ["a", {"href": "/static/ok"}, "go"],
        ["a", {"href": "https://evil.example/x"}, None],
        ["a", {"href": "//evil.example/x"}, None],
        ["a", {"href": "/\\evil.example"}, None],
        ["a", {"href": "javascript:alert(1)"}, None],
        ["img", {"src": "data:image/png;base64,AAAA"}, None],
        ["div", {"style": "color:red"}, None],
        ["div", {"onclick": "alert(1)"}, None],
        ["div", {"srcdoc": "<b>x</b>"}, None],
        ["div", {"innerHTML": "<b>x</b>"}, None],
        ["div", {"on": {"click": "alert(1)"}}, None],
        ["div", {"data-testid": "x", "aria-label": "y"}, "<b>text</b>"],
    ]
    probe = tmp_path / "probe.mjs"
    probe.write_text(DOM_PROBE, encoding="utf-8")
    completed = subprocess.run(
        [node, str(probe), dom.as_uri(), json.dumps(cases)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    results = report["results"]
    assert results[0]["attributes"] == {"href": "/static/ok"} and results[0]["children"] == ["go"]
    for case, result in zip(cases[1:-1], results[1:-1], strict=True):
        attributes = result.get("attributes", {})
        name = next(iter(case[1]))
        assert "refused" in result or name not in attributes, case
        assert not any(key.startswith("listener:") for key in attributes), case
    assert results[-1]["attributes"] == {"data-testid": "x", "aria-label": "y"}
    assert results[-1]["children"] == ["<b>text</b>"]
    properties = {key for _, key in report["recorded"]}
    assert not properties - {"value", "checked", "disabled"}, properties
    for name in whitelist_in(dom):
        assert name in CONTRACT_ATTRIBUTES or name.startswith(("aria-", "data-")), name


def whitelist_in(dom):
    """The quoted names of the first attribute whitelist in dom.js."""
    text = dom.read_text(encoding="utf-8")
    block = re.search(r"new Set\(\[(.*?)\]\)", text, re.S)
    return re.findall(r'"([^"]+)"', block.group(1)) if block else []


# --- RW-6 item 3: method calls on the product objects' sub-objects --------------------------

# The session's product objects reach the web layer behind ``state.Product`` proxies that
# count every method call outside a read-only allow list (RW-5 item 2). A field that is not
# callable passes through a proxy as it is: ``.state``, ``.gateway``, ``._pending``,
# ``session.controller`` and the like are the product's own objects, and a method called on
# one of them is never counted. Every such use in the web layer is on the reviewed list
# below (RW-6 item 3): a method call on a sub-object ("call"), a store into one ("store"),
# a sub-object handed to another function ("argument") or entered as a context ("with").
# A product object is ``<anything>.controller``, ``.session`` or ``.wealth``, a parameter
# of that name, or a local name bound to one; a sub-object is anything reached from one by
# an attribute, an index or ``getattr``, or a local name bound to that (closures included).
# The result of a call is not followed: the proxied methods return new values.
PRODUCT_FIELDS = frozenset({"controller", "session", "wealth"})
# Builtins that only test or read an attribute: handing them a sub-object calls nothing.
NO_HANDOVER = frozenset({"getattr", "hasattr", "isinstance", "id", "type"})
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _depth(node, roots, subs):
    """Steps from the innermost product object to ``node``; None when not reached from one."""
    if isinstance(node, ast.Name):
        return 0 if node.id in roots else 1 if node.id in subs else None
    if isinstance(node, ast.Attribute):
        inner = _depth(node.value, roots, subs)
        if inner is not None:
            return inner + 1
        return 0 if node.attr in PRODUCT_FIELDS else None
    if isinstance(node, ast.Subscript):
        inner = _depth(node.value, roots, subs)
        return None if inner is None else inner + 1
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and node.args
    ):
        inner = _depth(node.args[0], roots, subs)
        return None if inner is None else inner + 1
    return None


def _own_nodes(body):
    """A scope's nodes, without the bodies of the functions and classes defined in it."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _parameters(node):
    arguments = node.args
    names = [item.arg for item in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
    names += [item.arg for item in (arguments.vararg, arguments.kwarg) if item is not None]
    return names


def product_uses(source, roots=()):
    """(scope, kind, expression) of every use of a product sub-object in ``source``."""
    found = []

    def visit(body, scope, roots, subs):
        own = list(_own_nodes(body))
        grown = True
        while grown:  # local aliases, whatever their order in the scope: to a fixed point
            known = len(roots) + len(subs)
            for node in own:
                if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                    continue
                if node.value is None:
                    continue
                depth = _depth(node.value, roots, subs)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and depth is not None:
                        (roots if depth == 0 else subs).add(target.id)
            grown = len(roots) + len(subs) > known
        for node in own:
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and (_depth(func.value, roots, subs) or 0):
                    found.append((scope, "call", ast.unparse(func)))
                if not (isinstance(func, ast.Name) and func.id in NO_HANDOVER):
                    for argument in (*node.args, *(item.value for item in node.keywords)):
                        if _depth(argument, roots, subs):
                            text = f"{ast.unparse(func)}({ast.unparse(argument)})"
                            found.append((scope, "argument", text))
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                targets = (
                    node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                )
                for target in targets:
                    if isinstance(target, (ast.Attribute, ast.Subscript)):
                        if _depth(target.value, roots, subs):
                            found.append((scope, "store", ast.unparse(target)))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if _depth(item.context_expr, roots, subs):
                        found.append((scope, "with", ast.unparse(item.context_expr)))
        prefix = "" if scope == "<module>" else scope + "."
        for node in own:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name, set(), set())
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = set(_parameters(node))
                inner_roots = (roots - names) | (names & PRODUCT_FIELDS)
                visit(node.body, prefix + node.name, inner_roots, subs - names)

    visit(ast.parse(source).body, "<module>", set(roots), set())
    return found


def web_product_uses():
    found = Counter()
    for path in web_sources():
        name = path.relative_to(WEB).as_posix()
        for scope, kind, text in product_uses(path.read_text(encoding="utf-8")):
            found[(name, scope, kind, text)] += 1
    return found


# The reviewed list: (file under src/corridor/web, scope, kind, expression, times, review).
# "read" names what is read and why nothing changes; a write names the counted call it
# follows. Three need a closer look: compat.vault_tokenize, compat.attach_source_text
# (both after the counted ``ws.session.receive`` of ``service._submit``) and the
# ``gateway.query`` inside ``insights.load_billing`` (product code, below).
REVIEWED_USES = (
    # compat.py: the accessors of the controller's and the session's private fields.
    ("compat.py", "pending_entry", "call", "controller._pending.get", 1, "read: dict lookup"),
    ("compat.py", "pending_verified", "call", "controller._pending.get", 1, "read: dict lookup"),
    ("compat.py", "pending_card", "call", "controller._pending.get", 1, "read: dict lookup"),
    ("compat.py", "challenges", "call", "controller._pending.items", 1, "read: dict view"),
    ("compat.py", "draft_entry", "call", "controller._drafts.get", 1, "read: dict lookup"),
    ("compat.py", "plan_entry", "call", "controller._plans.get", 1, "read: dict lookup"),
    ("compat.py", "result_entry", "call", "controller._results.get", 1, "read: dict lookup"),
    ("compat.py", "result_status", "call", "controller._results.get", 1, "read: dict lookup"),
    ("compat.py", "envelope_mode", "call", "session._modes.get", 1, "read: dict lookup"),
    (
        "compat.py",
        "safety",
        "call",
        "controller.gateway.safety_snapshot",
        1,
        "read: GatewayService.safety_snapshot projects the audit records and the store",
    ),
    (
        "compat.py",
        "safety",
        "argument",
        "controller.gateway.safety_snapshot(controller.device.principal)",
        1,
        "read: the principal is a string",
    ),
    (
        "compat.py",
        "vault_tokenize",
        "call",
        "controller.gateway.vault.tokenize",
        1,
        "WRITE: PIIVault.tokenize registers new values (_values); its one caller,"
        " service._submit, calls it after the counted ws.session.receive, inside _unknown",
    ),
    ("compat.py", "kernel_view", "call", "controller._pending.get", 1, "read: dict lookup"),
    ("compat.py", "draft_display", "call", "controller._drafts.get", 1, "read: dict lookup"),
    ("compat.py", "confirmed_view", "call", "workflow._requests.items", 1, "read: dict view"),
    (
        "compat.py",
        "attach_source_text",
        "store",
        "controller._plans[plan_id]['source_text']",
        1,
        "WRITE: the plan keeps the user's words (B3); its one caller, service._submit,"
        " stores after the counted ws.session.receive, inside _unknown",
    ),
    # live.py: identity checks and the live client's context guard.
    (
        "live.py",
        "_usable",
        "argument",
        "ws.live.is_current(ws.controller.client)",
        1,
        "read: LiveSession.is_current compares the client's identity",
    ),
    (
        "live.py",
        "_apply_on",
        "argument",
        "live.client(controller.gateway.vault.ensure_safe)",
        1,
        "read: PIIVault.ensure_safe only raises on private values; the live client calls it",
    ),
    (
        "live.py",
        "_reason_line",
        "argument",
        "live.is_current(controller.client)",
        1,
        "read: identity comparison",
    ),
    # purchase.py, refresh.py, service.py, tasks.py: projections of the product state.
    ("purchase.py", "snapshot", "call", "state.accounts.get", 1, "read: dict lookup"),
    (
        "refresh.py",
        "deliver",
        "argument",
        "compat.build_sms(ws.controller.state)",
        1,
        "read: phone.build_sms formats the SMS from the state",
    ),
    (
        "service.py",
        "subscription_choices",
        "call",
        "c.state.subscriptions.items",
        1,
        "read: dict view",
    ),
    (
        "service.py",
        "subscription_choices",
        "call",
        "c.state.merchants[ref].model_dump",
        1,
        "read: a pydantic dump is a new dict",
    ),
    (
        "service.py",
        "schedule_cancel_choices",
        "call",
        "state.authorizations.items",
        1,
        "read: dict view",
    ),
    (
        "service.py",
        "schedule_cancel_choices",
        "call",
        "state.schedules.values",
        1,
        "read: dict view",
    ),
    (
        "service.py",
        "schedule_cancel_choices",
        "call",
        "contacts[ref].model_dump",
        1,
        "read: a pydantic dump is a new dict",
    ),
    (
        "tasks.py",
        "subscription_listing",
        "argument",
        "reversed(ws.session.history)",
        1,
        "read: iteration",
    ),
    ("tasks.py", "object_facts", "argument", "_owner(item)", 1, "read: tasks._owner"),
    ("tasks.py", "object_facts", "argument", "_record(item)", 1, "read: tasks._record"),
    ("tasks.py", "object_facts", "call", "table.items", 1, "read: dict view"),
    (
        "tasks.py",
        "object_facts",
        "argument",
        "compat.display_names.catalog_product(product)",
        1,
        "read: display_names.catalog_product looks the product up",
    ),
    (
        "tasks.py",
        "object_facts",
        "argument",
        "compat.display_names.catalog_product(state)",
        1,
        "read: display_names.catalog_product looks the product up",
    ),
    (
        "tasks.py",
        "list_facts",
        "argument",
        "compat.display_names.catalog_product(state)",
        1,
        "read: display_names.catalog_product looks the product up",
    ),
    # suggest.py: the session's suggestion desk, a sub-object the session's proxy does not
    # count. Its reads are made on the desk itself; its one write, ``adopt``, is made only
    # through a proxy that counts into the session's own Effects (the module docstring).
    (
        "suggest.py",
        "choices",
        "call",
        "desk.view",
        1,
        "read: SuggestionDesk.view lists one reply's offers with their states; it reads the"
        " desk's _offers and, through is_open, session._responses; writes nothing",
    ),
    (
        "suggest.py",
        "_counted",
        "argument",
        "web_state.Product(ws.session.suggestions)",
        1,
        "WRITE: the desk is handed to a proxy counting into the session's own Effects with"
        " no allow list; its one use, choose's adopt (which marks the suggestion, appends"
        " to the desk's log and sends through the counted ws.session.receive), counts"
        " before it runs, so a refusal raised after it is an unknown result",
    ),
    (
        "suggest.py",
        "_judge_lines",
        "call",
        "desk.log.records",
        1,
        "read: SuggestionLog.records returns a deep copy of one reply's records",
    ),
    (
        "suggest.py",
        "_judge_lines",
        "call",
        "desk.reason",
        1,
        "read: a static function of the record, for the judge line",
    ),
    # vm/: the view models read the state for labels and lines.
    (
        "vm/cards.py",
        "card_notes",
        "argument",
        "suitability.lines(state)",
        1,
        "read: suitability.lines builds text",
    ),
    ("vm/cards.py", "card_notes", "call", "contacts.get", 1, "read: dict lookup"),
    (
        "vm/cards.py",
        "card_notes",
        "argument",
        "fraud_hint.payee_name(state)",
        1,
        "read: fraud_hint.payee_name looks the payee up",
    ),
    ("vm/chat.py", "response_vm", "call", "state.schedules.values", 1, "read: dict view"),
    (
        "vm/chat.py",
        "chat_vm",
        "argument",
        "demo_contacts(state)",
        1,
        "read: controller.state is a copy of the store's state; demo_contacts reads its"
        " contacts' owner, verification and display name",
    ),
    (
        "vm/chat.py",
        "chat_vm",
        "argument",
        "turn_vm(state)",
        1,
        "read: controller.state is a copy of the store's state; turn_vm hands it to"
        " display_names.display_text, which replaces tokens by labels",
    ),
    (
        "vm/evidence.py",
        "evidence_vm",
        "call",
        "c.gateway.audit.checkpoint",
        1,
        "read: AuditLog.checkpoint verifies the chain and returns a deep copy of its head",
    ),
    (
        "vm/evidence.py",
        "evidence_vm",
        "argument",
        "str(c.planner_source)",
        1,
        "read: a label",
    ),
    (
        "vm/gate.py",
        "gate_vm",
        "argument",
        "scam_friction.facts(getattr(controller, 'state', None))",
        1,
        "read: scam_friction.facts is a pure projection",
    ),
    ("vm/tabs.py", "operator_vm", "argument", "reversed(session.history)", 1, "read: iteration"),
    (
        "vm/tabs.py",
        "latest_reply",
        "argument",
        "reversed(ws.session.history)",
        1,
        "read: iteration",
    ),
    (
        "vm/tabs.py",
        "accounts_vm",
        "argument",
        "display_names.label(c.state)",
        1,
        "read: display_names.label",
    ),
    (
        "vm/tabs.py",
        "product_name",
        "argument",
        "display_names.label(controller.state)",
        1,
        "read: display_names.label",
    ),
    (
        "vm/tabs.py",
        "wealth_vm",
        "argument",
        "tr(c.client.deployment.label)",
        1,
        "read: a label",
    ),
)

# Product functions the web layer hands a controller proxy to (through ``compat``) that
# reach a sub-object themselves, by the same check on their own source; ``controller`` is
# the proxy there. load_billing's ``gateway.query`` is the one such reach: the
# function's first statement is the counted ``controller._require_login()``.
PRODUCT_REACHES = {
    ("ui/insights.py", "load_billing"): Counter({
        ("load_billing", "call", "controller.gateway.query"): 1,
        ("load_billing", "call", "controller.state.transactions.values"): 1,
    }),
    ("ui/manual_mfa.py", "_challenge_active"): Counter({
        ("_challenge_active", "with", "verifier._lock"): 1,
        ("_challenge_active", "call", "verifier._challenges.get"): 1,
    }),
    ("ui/phone.py", "_read_challenge"): Counter({
        ("_read_challenge", "with", "verifier._lock"): 1,
        ("_read_challenge", "call", "verifier._challenges.get"): 1,
        ("_read_challenge", "call", "verifier.test_code"): 1,
    }),
}


def test_the_web_layer_uses_product_sub_objects_only_where_reviewed():
    reviewed = Counter()
    for name, scope, kind, text, times, review in REVIEWED_USES:
        assert review, (name, scope, kind, text)
        reviewed[(name, scope, kind, text)] += times
    found = web_product_uses()
    assert found - reviewed == Counter(), "not reviewed (RW-6 item 3)"
    assert reviewed - found == Counter(), "listed but gone: update the review"


def test_the_writes_on_the_reviewed_list_follow_a_counted_call():
    """The two writes of the web layer outside the proxies have one caller, which calls
    them after the counted ``ws.session.receive`` and inside ``_unknown``."""
    from corridor.web.state import SESSION_READS

    tree = ast.parse((WEB / "service.py").read_text(encoding="utf-8"))
    callers = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    if inner.func.attr in {"vault_tokenize", "attach_source_text"}:
                        callers.setdefault(inner.func.attr, set()).add(node.name)
    assert callers == {"vault_tokenize": {"_submit"}, "attach_source_text": {"_submit"}}
    for path in web_sources():
        if path.name in {"service.py", "compat.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert "vault_tokenize" not in text and "attach_source_text" not in text, path
    (submit,) = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_submit"
    ]
    operation, unknown = submit.body[1], submit.body[2]
    # The session reads the count of external files with the words (SG-6), never a file.
    assert "ws.session.receive(text, external_files=external_files)" in ast.unparse(operation)
    assert "receive" not in SESSION_READS
    assert ast.unparse(unknown.items[0].context_expr) == "_unknown()"
    written = ast.unparse(unknown)
    assert "compat.vault_tokenize" in written and "compat.attach_source_text" in written


def test_the_product_functions_handed_a_proxy_reach_only_the_reviewed_sub_objects():
    from corridor.web import compat
    from corridor.web.state import CONTROLLER_READS

    for (name, function), expected in PRODUCT_REACHES.items():
        tree = ast.parse((SOURCE / name).read_text(encoding="utf-8"))
        (node,) = [
            item
            for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == function
        ]
        found = Counter(product_uses(ast.unparse(node), roots={"controller"}))
        assert found == +expected, (name, function)
    tree = ast.parse((SOURCE / "ui" / "insights.py").read_text(encoding="utf-8"))
    (billing,) = [
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "load_billing"
    ]
    statements = billing.body[1:] if ast.get_docstring(billing) else billing.body
    assert ast.unparse(statements[0]) == "controller._require_login()"
    assert "_require_login" not in CONTROLLER_READS
    query = next(i for i, item in enumerate(statements) if "gateway.query" in ast.unparse(item))
    assert query > 0
    from corridor.ui import insights, manual_mfa, phone

    assert compat.load_billing is insights.load_billing
    assert compat._challenge_active is manual_mfa._challenge_active
    assert compat._read_challenge is phone._read_challenge


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def f(ws):\n    ws.controller.state.gateway.query(1)\n", ("f", "call")),
        ("def f(ws):\n    ws.session.controller.dispatch()\n", ("f", "call")),
        ("def f(controller):\n    controller._pending.pop('x')\n", ("f", "call")),
        ("def f(ws):\n    s = ws.controller.state\n    s.ledger.post(1)\n", ("f", "call")),
        (
            "def f(ws):\n    g(item)\n    item = table[r]\n    table = s.x\n"
            "    s = ws.controller.state\n",
            ("f", "argument"),
        ),
        ("def f(ws):\n    getattr(ws.controller, 'gateway').query()\n", ("f", "call")),
        (
            "def f(ws):\n    c = ws.controller\n    def g():\n        c.gateway.query()\n",
            ("f.g", "call"),
        ),
        ("def f(controller):\n    controller._plans['p']['t'] = 1\n", ("f", "store")),
        ("def f(ws):\n    del ws.controller._closed['x']\n", ("f", "store")),
        ("def f(ws):\n    ws.controller.state.now += 1\n", ("f", "store")),
        ("def f(ws):\n    helper(ws.controller.gateway)\n", ("f", "argument")),
        ("def f(ws):\n    with ws.controller.gateway.mfa._lock:\n        pass\n", ("f", "with")),
        (
            "class Desk:\n    def m(self, ws):\n        ws.wealth._book.clear()\n",
            ("Desk.m", "call"),
        ),
        ("F = lambda c: c.ws.controller._pending.clear()\n", ("<module>", "call")),
    ],
)
def test_the_sub_object_check_refuses(source, expected):
    """The check itself: each new use of a sub-object is found, so a new one fails the
    guard above until it is reviewed."""
    assert expected in [(scope, kind) for scope, kind, _ in product_uses(source)]


@pytest.mark.parametrize(
    "source",
    [
        "def f(ws):\n    ws.controller.snapshot()\n",
        "def f(ws):\n    ws.session.client = None\n",
        "def f(ws):\n    unwrap(ws.controller)\n",
        "def f(ws):\n    compat.load_billing(ws.controller)\n",
        "def f(ws):\n    isinstance(getattr(ws.controller, 'client', None), object)\n",
        "def f(ws):\n    r = ws.session.response('x')\n    r['payload'].get('y')\n",
        "def f(controller):\n    def g(controller):\n        return controller\n    return g\n",
    ],
)
def test_the_sub_object_check_passes_proxy_calls_and_new_values(source):
    """A call or a store on the proxy itself is counted (or allow-listed), and what a
    proxied call returns is a new value: none of them is a sub-object use."""
    assert product_uses(source) == []


# --- the shared fixtures guard themselves ---------------------------------------------------


def test_the_network_is_closed_except_loopback():
    import os
    import socket

    for call in (
        lambda: socket.create_connection(("192.0.2.1", 80), timeout=1),
        lambda: socket.getaddrinfo("example.com", 443),
        lambda: socket.socket().connect(("198.51.100.7", 443)),
    ):
        with pytest.raises(RuntimeError, match="network disabled"):
            call()
    assert socket.getaddrinfo("127.0.0.1", 0)
    assert os.environ["CORRIDOR_LOCALE"] == "zh-CN"
    assert not [name for name in os.environ if name.startswith(("LLM_", "LIVE_", "CORRIDOR_LIVE"))]


def test_the_streamlit_sentinel_fails_any_use(st_sentinel):
    import corridor.ui.domestic_views as views

    assert views.st is st_sentinel
    with pytest.raises(AssertionError, match="st.session_state"):
        views.st.session_state  # the access itself is the check
    with pytest.raises(AssertionError):
        st_sentinel()


@pytest.mark.parametrize("locale", ["en", "zh-CN"])
def test_judge_copy_uses_evidence_index_instead_of_internal_run_names(locale):
    live = json.loads((SOURCE / "ui/messages-live.json").read_text(encoding="utf-8"))
    polish = json.loads((SOURCE / "ui/messages-polish.json").read_text(encoding="utf-8"))
    texts = [
        live[key][locale]
        for key in (
            "live.judge_strip",
            "live.judge_strip_revoked",
            "live.price_note.openai-gpt-5.4-mini",
            "live.price_note.deepseek-flash",
        )
    ] + [polish["polish.view.judge_strip"][locale]]
    for text in texts:
        assert not any(internal in text for internal in ("domestic-freeze-10", "F9", "F7b"))
        assert ("evidence index" if locale == "en" else "证据索引") in text
    # An unverified assumption must not become an official price claim.
    assert ("not verified" if locale == "en" else "未经核验") in texts[2]


@pytest.mark.parametrize(
    "locale,words", [(None, "本会话审批过于频繁"), ("en", "Too many approvals")]
)
def test_approval_rate_lock_explains_restart_without_changing_outcome(web_app, locale, words):
    from tests.web.test_chat_contact_hint import page_of

    page = page_of(web_app, locale=locale)
    for _ in range(6):
        rkey = page.send("给妈妈转1元")
        if page.state["shell"]["safety"]["locked"]:
            break
        page.device("T" + rkey, "confirm")
    safety = page.state["shell"]["safety"]
    assert safety["locked"]
    assert page.ws.safety["reason"] == "approval_rate"
    assert words in safety["hint"]
    assert ("重新开始" if locale is None else "Restart") in safety["hint"]
    count = page.ws.controller.gateway.bank.execute_count
    page.send("给妈妈转1元")
    assert page.ws.controller.gateway.bank.execute_count == count
    page.post("/api/session/reset", {})
    assert not page.state["shell"]["safety"]["locked"]
