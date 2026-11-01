"""Test-only web server for the Playwright journeys (never imported by product code).

    python -m tests.web_e2e.harness --port 0
        --mode <scripted|live-fake|suggest-fake|fail-once|fail-unknown|public>
        --ready <file>

Binds 127.0.0.1 only (port 0 picks a free one), then writes ``{"url", "port", "mode", ...}``
to the ready file. Every mode clears inherited model and live settings and refuses
non-loopback sockets, so no paid or external request can leave the process.

- ``scripted``: the default environment (live switch off).
- ``live-fake``: S7 variables point at a synthetic credential file under ``.tmp`` (a test
  sentinel key and access code), the transport is ``tests.unit.test_ui_live_mode.FakeModel``,
  and every deployment label gains a "test fake model" suffix so no screenshot can pass
  for a real model.
- ``suggest-fake``: ``live-fake`` whose fake model also answers the chat's suggestion step
  with one fixed reading (a transfer to the trusted contact CONTACT_CHEN of the amount 500),
  so a journey can show, click and complete a suggestion; every check still applies.
- ``fail-once`` / ``fail-unknown``: the first ``DeviceWorkflow.confirm_transfer`` call per
  workflow instance raises ``RuntimeError`` (an expected failure) or ``TypeError`` (outcome
  unknown). This keeps independent sessions' injected failures isolated in the shared mode.
- ``public``: public mode for ``demo.example`` behind a TLS terminator on a free loopback
  port, with a certificate made for this run: the part Caddy plays on a real site. The ready
  file holds ``https://demo.example`` as the URL and the ``host_rules`` that map it there.

``--features`` adds capabilities to this test server only (``live-fake`` and
``suggest-fake`` always add ``live``), so an ext item can be checked in the browser before
it joins ``FEATURES``.

A second loopback server (``probe_url`` in the ready file) answers ``GET /probe`` with call
counts the journeys assert on, such as ``Device.complete_mfa`` (always 0: INV-2.2). It is
separate so the app's own routes and Host checks stay exactly as in production.
"""

import argparse
import asyncio
import dataclasses
import datetime
import ipaddress
import json
import os
import secrets
import socket
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLEARED_PREFIXES = ("LLM_", "CORRIDOR_LIVE", "LIVE_")
FAKE_KEY = "sk-test-only-e2e-key-0123456789abcdef"
# "（测试假模型）": appended to every deployment label in live-fake mode (O-M6).
FAKE_SUFFIX = "（测试假模型）"
# Modes that switch on the synthetic live environment with the fake transport.
LIVE_MODES = ("live-fake", "suggest-fake")
PUBLIC_HOST = "demo.example"
COUNTS: dict[str, int] = {}


def clean_environment() -> None:
    for name in list(os.environ):
        if name.startswith(CLEARED_PREFIXES):
            del os.environ[name]
    os.environ["CORRIDOR_LOCALE"] = "zh-CN"
    os.environ["CORRIDOR_DEFAULT_VIEW"] = "customer"


def loopback_only() -> None:
    """Refuse every socket connection that does not target a loopback address."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection

    def loopback(address) -> bool:
        if not isinstance(address, tuple):
            return True
        host = address[0]
        if host in ("localhost",):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def connect(self, address):
        if not loopback(address):
            raise OSError("e2e harness: non-loopback connection refused")
        return original_connect(self, address)

    def connect_ex(self, address):
        if not loopback(address):
            raise OSError("e2e harness: non-loopback connection refused")
        return original_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        if not loopback(address):
            raise OSError("e2e harness: non-loopback connection refused")
        return original_create(address, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection


def count(owner, name: str, key: str) -> None:
    original = getattr(owner, name)
    COUNTS.setdefault(key, 0)

    def counted(*args, **kwargs):
        COUNTS[key] += 1
        return original(*args, **kwargs)

    setattr(owner, name, counted)


def install_probes() -> None:
    from corridor.sim.device import Device
    from corridor.sim.device_workflow import DeviceWorkflow

    count(Device, "complete_mfa", "device_complete_mfa")
    count(Device, "approve", "device_approve")
    count(DeviceWorkflow, "confirm_transfer", "workflow_confirm_transfer")


def fail_first_confirm(error: type[BaseException]) -> None:
    from corridor.sim.device_workflow import DeviceWorkflow

    original = DeviceWorkflow.confirm_transfer
    failed_workflows = set()

    def confirm_transfer(self, *args, **kwargs):
        if self not in failed_workflows:
            failed_workflows.add(self)
            raise error("e2e harness: injected failure")
        return original(self, *args, **kwargs)

    DeviceWorkflow.confirm_transfer = confirm_transfer


def live_fake(scratch: Path, *, suggest: bool = False) -> dict:
    """Synthetic S7 environment with the fake transport and relabelled deployments."""
    import pytest

    from corridor.ui import live_config
    from tests.suggest_fakes import TRANSFER, suggest_rule
    from tests.unit.test_ui_live_mode import FakeModel

    access_code = "e2e-" + secrets.token_urlsafe(24)
    assert live_config.access_code_strong(access_code), "synthetic access code must meet C3"
    credentials = scratch / "creds"
    credentials.mkdir(parents=True, exist_ok=True)
    lines = [
        "# synthetic credential file for the e2e harness; never a real key",
        f"LLM_API_KEY={FAKE_KEY}",
        "LLM_MAX_USD=1",
        f"{live_config.ACCESS_CODE}={access_code}",
    ]
    # One synthetic file under every name a deployment reads, so each can be selected.
    for name in sorted({item.env_file for item in live_config.DEPLOYMENTS}):
        (credentials / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[live_config.SWITCH] = "1"
    os.environ[live_config.ENV_DIR] = str(credentials)
    os.environ[live_config.JOURNAL_DIR] = str(scratch / "journal")
    patcher = pytest.MonkeyPatch()
    FakeModel(patcher, rule=suggest_rule(TRANSFER) if suggest else None)
    relabel(live_config)
    return {"access_code": access_code, "_patcher": patcher}


def relabel(live_config) -> None:
    original = live_config.DEPLOYMENTS
    renamed = tuple(dataclasses.replace(item, label=item.label + FAKE_SUFFIX) for item in original)
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("corridor."):
            continue
        if getattr(module, "DEPLOYMENTS", None) is original:
            module.DEPLOYMENTS = renamed
    if hasattr(live_config, "_BY_ID"):
        live_config._BY_ID = {item.id: item for item in renamed}


def tls_context(scratch: Path) -> ssl.SSLContext:
    """A certificate for ``PUBLIC_HOST`` made for this run; the browser is told to accept it."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, PUBLIC_HOST)])
    certificate = (
        x509
        .CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2026, 1, 1))
        .not_valid_after(datetime.datetime(2036, 1, 1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(PUBLIC_HOST)]), False)
        .sign(key, hashes.SHA256())
    )
    chain = scratch / "chain.pem"
    secret = scratch / "key.pem"
    chain.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    secret.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain, secret)
    return context


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def terminate_tls(port: int, context: ssl.SSLContext) -> tuple[asyncio.Server, int]:
    """TLS on a free loopback port, each connection piped to the app on ``port``."""

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))

    server = await asyncio.start_server(connection, "127.0.0.1", 0, ssl=context)
    return server, server.sockets[0].getsockname()[1]


def probe_handler():
    import tornado.web

    class Probe(tornado.web.RequestHandler):
        def get(self):
            self.set_header("Cache-Control", "no-store")
            self.set_header("Content-Type", "application/json")
            self.finish(json.dumps({"ok": True, "counts": dict(COUNTS)}))

    return Probe


async def serve(port: int, mode: str, ready: Path, extra_features: frozenset[str]) -> None:
    import tornado.httpserver
    import tornado.netutil
    import tornado.web

    import corridor

    if not Path(corridor.__file__).resolve().is_relative_to(ROOT / "src"):
        # Never serve another tree's code under this tree's journeys.
        print(f"harness: corridor is imported from {corridor.__file__}", file=sys.stderr)
        raise SystemExit(3)
    try:
        from corridor.web import FEATURES
        from corridor.web.app import make_app
    except ImportError as exc:
        print(f"harness: corridor.web.app is not available yet ({exc})", file=sys.stderr)
        raise SystemExit(3) from exc
    import corridor

    if not Path(corridor.__file__).resolve().is_relative_to(ROOT / "src"):
        # The journeys must exercise this work tree, never another tree's install.
        print(f"harness: corridor imported from {corridor.__file__}", file=sys.stderr)
        raise SystemExit(4)

    extra: dict = {}
    if mode in LIVE_MODES:
        scratch = ROOT / ".tmp" / "web-e2e" / f"harness-{mode}-{secrets.token_hex(4)}"
        scratch.mkdir(parents=True, exist_ok=True)
        extra = live_fake(scratch, suggest=mode == "suggest-fake")
    elif mode == "fail-once":
        fail_first_confirm(RuntimeError)
    elif mode == "fail-unknown":
        fail_first_confirm(TypeError)
    install_probes()

    sockets = tornado.netutil.bind_sockets(port, address="127.0.0.1")
    actual = sockets[0].getsockname()[1]
    features = FEATURES | extra_features | ({"live"} if mode in LIVE_MODES else set())
    settings = {"port": actual, "features": features}
    if mode == "public":
        from corridor.web.public import RATES, PublicConfig, Rate

        # All public journeys share one address and leave their sessions in the table.
        # Give this suite room for both new sessions and retained sessions; production
        # rate/capacity behavior is still exercised with defaults in tests/web/.
        settings["public"] = PublicConfig(
            host=PUBLIC_HOST, per_ip_sessions=128, rates={**RATES, "session": Rate(1000, 1.0)}
        )
    app = make_app(settings)
    server = tornado.httpserver.HTTPServer(app)
    server.add_sockets(sockets)
    probe_sockets = tornado.netutil.bind_sockets(0, address="127.0.0.1")
    probe = tornado.httpserver.HTTPServer(tornado.web.Application([(r"/probe", probe_handler())]))
    probe.add_sockets(probe_sockets)
    info = {
        "url": f"http://127.0.0.1:{actual}",
        "port": actual,
        "mode": mode,
        "probe_url": f"http://127.0.0.1:{probe_sockets[0].getsockname()[1]}/probe",
        "features": sorted(features),
    }
    info.update({key: value for key, value in extra.items() if not key.startswith("_")})
    if mode == "public":
        scratch = ROOT / ".tmp" / "web-e2e" / f"harness-public-{secrets.token_hex(4)}"
        scratch.mkdir(parents=True, exist_ok=True)
        _, tls_port = await terminate_tls(actual, tls_context(scratch))
        info["url"] = f"https://{PUBLIC_HOST}"
        info["host_rules"] = f"MAP {PUBLIC_HOST}:443 127.0.0.1:{tls_port}"
    temporary = ready.with_suffix(".partial")
    temporary.write_text(json.dumps(info), encoding="utf-8")
    temporary.replace(ready)
    print(f"harness: {mode} on {info['url']}", flush=True)
    await asyncio.Event().wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corridor web e2e harness (test-only)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--mode",
        choices=("scripted", *LIVE_MODES, "fail-once", "fail-unknown", "public"),
        required=True,
    )
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--features", default="", help="comma-separated extra capabilities")
    args = parser.parse_args(argv)
    extra = frozenset(item for item in args.features.split(",") if item)
    clean_environment()
    loopback_only()
    try:
        asyncio.run(serve(args.port, args.mode, args.ready, extra))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
