"""Run the web front end's Playwright journeys against a loopback harness.

    python tests/web_e2e/run_e2e.py
        --mode <scripted|live-fake|suggest-fake|fail-once|fail-unknown|public|
            lab-scripted|lab-unknown|fixtures|all>
        [--grep <pattern>] [--project desktop|mobile] [--features <cap,...>]

Playwright 1.64.1 is taken from ``CORRIDOR_PLAYWRIGHT_NODE_MODULES`` or the local npx cache;
nothing is downloaded and no browser is installed (the specs drive the system Edge). Each
harness mode starts ``tests.web_e2e.harness`` on a free loopback port, runs the specs tagged
for that mode, then stops it. ``fixtures`` starts no server: specs answer ``/api/*`` from
``fixtures/*.json`` through ``page.route``. Results land in ``.tmp/web-e2e/``: one JSON
report per mode, a summary ``report.json`` and the screenshot ``screens/manifest.json``.
Exit code 0 means every selected spec passed (a skipped spec is reported, never counted as
passed). ``--features`` adds capabilities to the test server only, so an ext item can be
checked before it joins ``corridor.web.FEATURES``; the report records them. The two lab
lanes run the authorization lab's journeys on a server that opens ``lab``, which the
submission build closes. ``public`` runs the journeys tagged for it against public mode over
TLS on the public name, which the browser maps to the harness (``host_rules``).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT = ROOT / ".tmp" / "web-e2e"
SCREENS = OUT / "screens"
# Lanes share the work tree and may run at once: each run keeps its own reports, results and
# screenshot records; only report.json and screens/manifest.json are shared (last writer).
RUN = str(os.getpid())
RUN_DIR = OUT / "runs" / RUN
STATIC_ROOT = ROOT / "src" / "corridor" / "web" / "static"
PLAYWRIGHT_VERSION = "1.64.1"
HARNESS_MODES = ("scripted", "live-fake", "suggest-fake", "fail-once", "fail-unknown", "public")
# The authorization lab is closed in the submission build (RW-4''). Its journeys run on a
# server of their own that opens ``lab`` (lane -> harness mode), so no other journey, and
# no screenshot meant for the materials, sees the lab; a lane's screenshots are test-only.
LAB_LANES = {"lab-scripted": "scripted", "lab-unknown": "fail-unknown"}
LAB = "lab"
MODES = (*HARNESS_MODES, *LAB_LANES, "fixtures")
# Which spec titles each mode runs. Component specs (C, D lanes) run only on fixtures.
MODE_TAGS = {
    "scripted": "@scripted",
    "live-fake": "@live-fake",
    "suggest-fake": "@suggest-fake",
    "fail-once": "@fail-once",
    "fail-unknown": "@fail-unknown",
    "public": "@public",
    "lab-scripted": "@lab-scripted",
    "lab-unknown": "@lab-unknown",
    "fixtures": "@fixtures|@c-|@d-",
}
READY_TIMEOUT = 120.0


class RunnerError(RuntimeError):
    pass


def find_node_modules() -> Path:
    override = os.environ.get("CORRIDOR_PLAYWRIGHT_NODE_MODULES")
    candidates = []
    if override:
        candidates.append(Path(override))
    else:
        cache = Path(os.environ.get("LOCALAPPDATA", "")) / "npm-cache" / "_npx"
        if cache.is_dir():
            candidates.extend(sorted(cache.glob("*/node_modules")))
    for modules in candidates:
        package = modules / "playwright" / "package.json"
        try:
            version = json.loads(package.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError):
            continue
        if version == PLAYWRIGHT_VERSION:
            return modules
    raise RunnerError(
        f"playwright {PLAYWRIGHT_VERSION} not found (set CORRIDOR_PLAYWRIGHT_NODE_MODULES to a "
        "node_modules directory that holds it). Nothing was downloaded; install it by hand."
    )


def find_node() -> str:
    node = shutil.which("node")
    if node is None:
        raise RunnerError("node was not found on PATH")
    return node


def grep_for(mode: str, user: str | None) -> str:
    mode_part = f"(?=[\\s\\S]*(?:{MODE_TAGS[mode]}))"
    user_part = f"(?=[\\s\\S]*(?:{user}))" if user else ""
    return f"^{mode_part}{user_part}"


def harness_for(lane: str, features: str = "") -> tuple[str, str]:
    """The harness mode and the extra capabilities of a lane: a lab lane opens ``lab``."""
    if lane in LAB_LANES:
        return LAB_LANES[lane], ",".join(item for item in (features, LAB) if item)
    return lane, features


def harness_env() -> dict:
    """The harness serves this work tree's code, whichever interpreter runs it: its
    ``src`` comes first on the path, before any editable install of another tree."""
    path = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    return dict(os.environ, PYTHONPATH=path, PYTHONIOENCODING="utf-8")


def start_harness(
    mode: str, ready: Path, features: str = "", *, lane: str | None = None
) -> tuple[subprocess.Popen, dict]:
    ready.unlink(missing_ok=True)
    name = lane or mode
    log = (RUN_DIR / f"harness-{name}.log").open("w", encoding="utf-8")  # the child keeps its copy
    command = [
        sys.executable,
        "-m",
        "tests.web_e2e.harness",
        "--port",
        "0",
        "--mode",
        mode,
        "--ready",
        str(ready),
        "--features",
        features,
    ]
    with log:
        process = subprocess.Popen(
            command, cwd=ROOT, env=harness_env(), stdout=log, stderr=subprocess.STDOUT
        )
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = (RUN_DIR / f"harness-{name}.log").read_text(encoding="utf-8")[-2000:]
            raise RunnerError(f"harness ({name}) exited with {process.returncode}:\n{tail}")
        if ready.exists():
            try:
                info = json.loads(ready.read_text(encoding="utf-8"))
            except (ValueError, PermissionError):
                # Windows may expose the file while the harness still holds its write handle.
                # Keep observing this process until the existing readiness deadline.
                info = None
            if info:
                return process, info
        time.sleep(0.2)
    stop_harness(process)
    raise RunnerError(f"harness ({name}) was not ready within {READY_TIMEOUT:.0f} s")


def stop_harness(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)


def run_playwright(
    mode: str, node: str, modules: Path, grep: str, project: str | None, extra_env: dict
) -> dict:
    report = RUN_DIR / f"report-{mode}.json"
    command = [
        node,
        str(modules / "playwright" / "cli.js"),
        "test",
        "-c",
        str(HERE / "playwright.config.cjs"),
        "--grep",
        grep,
        "--pass-with-no-tests",
    ]
    if project:
        command += ["--project", project]
    env = dict(
        os.environ,
        NODE_PATH=str(modules),
        CORRIDOR_E2E_MODE=mode,
        CORRIDOR_E2E_OUT=str(OUT),
        CORRIDOR_E2E_REPORT=str(report),
        CORRIDOR_E2E_RESULTS=str(RUN_DIR / "test-results"),
        CORRIDOR_E2E_RUN=RUN,
        CORRIDOR_E2E_STATIC_ROOT=str(STATIC_ROOT),
        CORRIDOR_E2E_ROOT=str(ROOT),
        **extra_env,
    )
    env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    started = time.monotonic()
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    return summarise(mode, report, completed.returncode, time.monotonic() - started)


def summarise(mode: str, report: Path, exit_code: int, seconds: float) -> dict:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "flaky": 0}
    failed, skipped = [], []
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if data is not None:
        for title, outcome in walk(data.get("suites", []), []):
            if outcome == "expected":
                counts["passed"] += 1
            elif outcome == "skipped":
                counts["skipped"] += 1
                skipped.append(title)
            elif outcome == "flaky":
                counts["flaky"] += 1
                failed.append(title)
            else:
                counts["failed"] += 1
                failed.append(title)
    no_tests = data is not None and sum(counts.values()) == 0
    ok = exit_code == 0 and data is not None and not failed
    return {
        "mode": mode,
        "exit": exit_code,
        "ok": ok,
        "no_tests": no_tests,
        "seconds": round(seconds, 1),
        **counts,
        "failed_titles": failed,
        "skipped_titles": skipped,
    }


def walk(suites: list, path: list[str]):
    for suite in suites:
        here = [*path, suite.get("title", "")] if suite.get("title") else path
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                title = " > ".join([*here, spec.get("title", ""), test.get("projectName", "")])
                yield title, test.get("status")
        yield from walk(suite.get("suites", []), here)


def build_manifest() -> tuple[list[str], list[str]]:
    """Fold this run's shot records into manifest.json.

    Returns (policy violations, warnings). An entry whose file no longer has its recorded
    sha256 was overwritten by a later or concurrent run: it is dropped with a warning, and
    that run records its own entry.
    """
    records = SCREENS / f"manifest-{RUN}.jsonl"
    manifest = SCREENS / "manifest.json"
    entries: dict[tuple, dict] = {}
    if manifest.exists():
        try:
            for entry in json.loads(manifest.read_text(encoding="utf-8")):
                entries[(entry["spec"], entry["step"], entry["project"], entry["mode"])] = entry
        except (ValueError, KeyError, TypeError):
            entries = {}
    if records.exists():
        for line in records.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                entries[(entry["spec"], entry["step"], entry["project"], entry["mode"])] = entry
        records.unlink()
    problems, warnings, kept = [], [], []
    for entry in entries.values():
        path = SCREENS / entry["path"]
        if not path.exists():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry.get("sha256"):
            warnings.append(f"overwritten since recorded, dropped: {entry['path']}")
            continue
        test_only = entry["path"].startswith("test-only/")
        if entry["mode"] != "scripted" and not (entry["not_for_materials"] and test_only):
            problems.append(f"{entry['mode']} screenshot outside test-only: {entry['path']}")
        if test_only and not entry["not_for_materials"]:
            problems.append(f"test-only screenshot not marked not_for_materials: {entry['path']}")
        kept.append(entry)
    kept.sort(key=lambda item: (item["path"],))
    SCREENS.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(kept, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return problems, warnings


def run(mode: str, grep: str | None, project: str | None, features: str = "") -> list[dict]:
    """Run each selected mode; a single named mode must select at least one spec."""
    node, modules = find_node(), find_node_modules()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    selected = MODES if mode == "all" else (mode,)
    results = []
    for current in selected:
        pattern = grep_for(current, grep)
        if current == "fixtures":
            results.append(run_playwright(current, node, modules, pattern, project, {}))
            continue
        ready = RUN_DIR / f"ready-{current}.json"
        harness_mode, lane_features = harness_for(current, features)
        process, info = start_harness(harness_mode, ready, lane_features, lane=current)
        try:
            extra = {"CORRIDOR_WEB_URL": info["url"], "CORRIDOR_E2E_READY": str(ready)}
            if "host_rules" in info:
                extra["CORRIDOR_E2E_HOST_RULES"] = info["host_rules"]
            item = run_playwright(current, node, modules, pattern, project, extra)
            item["features"] = info.get("features", [])
            results.append(item)
        finally:
            stop_harness(process)
            ready.unlink(missing_ok=True)
    if mode != "all":
        for item in results:
            if item["no_tests"]:
                item["ok"] = False
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=(*MODES, "all"), default="scripted")
    parser.add_argument("--grep", default=None)
    parser.add_argument("--project", choices=("desktop", "mobile"), default=None)
    parser.add_argument("--features", default="", help="extra capabilities, test server only")
    args = parser.parse_args(argv)
    if args.grep is not None:
        re.compile(args.grep)
    try:
        results = run(args.mode, args.grep, args.project, args.features)
    except RunnerError as exc:
        print(f"run_e2e: {exc}", file=sys.stderr)
        return 2
    problems, warnings = build_manifest()
    ok = all(item["ok"] for item in results) and not problems
    summary = {
        "mode": args.mode,
        "grep": args.grep,
        "project": args.project,
        "extra_features": args.features,
        "ok": ok,
        "results": results,
        "manifest_problems": problems,
        "manifest_warnings": warnings,
    }
    (RUN_DIR / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUT / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for item in results:
        print(
            f"[{item['mode']}] passed={item['passed']} failed={item['failed']} "
            f"skipped={item['skipped']} flaky={item['flaky']} exit={item['exit']} "
            f"({item['seconds']} s)"
        )
        for title in item["skipped_titles"]:
            print(f"  skipped: {title}")
        for title in item["failed_titles"]:
            print(f"  FAILED: {title}")
    for problem in problems:
        print(f"manifest: {problem}")
    for warning in warnings:
        print(f"manifest (warning): {warning}")
    print(f"reports: {RUN_DIR}")
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
