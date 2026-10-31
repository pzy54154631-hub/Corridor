"""Run one equivalence flow with one driver in a fresh process and write its stages.

Usage:
  python -m tests.web.flow_runner --driver streamlit|web --flow <name> --out <json>

A process per flow and driver isolates the global SMS outbox, environment variables,
context variables and the simulated clock. Scripted planner only; any socket that is not
loopback is refused, so the run makes no network request.
"""

import argparse
import json
import os
import socket
import sys
import time
import traceback
from pathlib import Path

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
ENV_PREFIXES = ("LLM_", "CORRIDOR_LIVE", "LIVE_")


def isolate(locale="zh-CN"):
    """Clear live-model variables, pin the language and view, and refuse every lookup or
    connection that is not loopback (the same closed set as the web tests' fixture)."""
    for key in [key for key in os.environ if key.startswith(ENV_PREFIXES)]:
        del os.environ[key]
    os.environ["CORRIDOR_LOCALE"] = locale
    os.environ["CORRIDOR_DEFAULT_VIEW"] = "judge"
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    lookup = socket.getaddrinfo

    def allowed(address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if host not in LOOPBACK:
            raise OSError(f"network disabled in equivalence runs: {host!r}")

    def guarded_connect(self, address):
        allowed(address)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        allowed(address)
        return connect_ex(self, address)

    def guarded_lookup(host, *args, **kwargs):
        allowed((host,))
        return lookup(host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.getaddrinfo = guarded_lookup


def run_flow(flow, driver_name):
    from tests.web.drivers import CallProbe, StreamlitDriver, WebDriver, run_step
    from tests.web.reference_flows import ReferenceDriver
    from tests.web.state_probe import normalized

    drivers = {"streamlit": StreamlitDriver, "web": WebDriver, "reference": ReferenceDriver}

    stages = []
    driver = None
    try:
        driver = drivers[driver_name](features=flow.features)
        probe = CallProbe(driver.controller)

        def observe(stage):
            return {"stage": stage, **normalized(driver.observe()), "calls": probe.snapshot()}

        stages.append(observe("login"))
        for step in flow.steps:
            run_step(driver, step)
            stages.append(observe(step.stage))
    except Exception as error:  # noqa: BLE001 - a crash is itself an observation
        stages.append({
            "stage": "error",
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(limit=8),
        })
    finally:
        if driver is not None:
            driver.close()
    return stages


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--driver", choices=("streamlit", "web", "reference"), required=True)
    parser.add_argument("--flow", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    isolate()
    import corridor
    from tests.web.flows import FLOWS

    root = Path(__file__).resolve().parents[2]
    if not Path(corridor.__file__).resolve().is_relative_to(root):
        raise SystemExit(f"corridor is imported from another tree: {corridor.__file__}")
    started = time.perf_counter()
    stages = run_flow(FLOWS[args.flow], args.driver)
    result = {
        "flow": args.flow,
        "driver": args.driver,
        "corridor_file": corridor.__file__,
        "seconds": round(time.perf_counter() - started, 2),
        "stages": stages,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    print(args.flow, args.driver, "->", [stage["stage"] for stage in stages], result["seconds"])
    return 0 if stages and stages[-1]["stage"] != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
