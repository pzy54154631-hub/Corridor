"""``python -m corridor.web --port 8601``: serve the web demo on a loopback address only.

Any other ``--host`` is refused, and after listening every bound socket is checked to be
a loopback address; otherwise the process exits with status 2.

``--public-host <name>`` (or ``CORRIDOR_WEB_PUBLIC_HOST``) opens public mode (RW-7, RW-8).
The process still binds the loopback address only; a reverse proxy in front of it does
TLS and forwards to it. The service then accepts the public name as Host and ``https://``
as Origin, sets the ``__Host-`` cookie, limits requests and sessions by client address,
reaps idle sessions once a minute and prints the public URL as a third line. The capacity
options apply in public mode only; given without a public host, the entry refuses them.
With ``CORRIDOR_LIVE=1`` a public site also needs the access verifier pair printed by
``python -m corridor.web.access_setup`` and live caps within the public bounds, or it does
not start (``PublicConfig.from_args``).
"""

import argparse
import asyncio
import ipaddress
import logging
import os
import sys
from typing import Any

from tornado.httpserver import HTTPServer
from tornado.ioloop import PeriodicCallback
from tornado.netutil import bind_sockets

from corridor.web.app import SERVER_OPTIONS, make_app
from corridor.web.public import PublicConfig, quiet_client_logs, tick

LOOPBACK = ("127.0.0.1", "::1", "localhost")
URL_HOST = {"127.0.0.1": "127.0.0.1", "::1": "[::1]", "localhost": "localhost"}
TICK_MS = 60_000


def loopback_only(sockets: list) -> bool:
    for sock in sockets:
        address = sock.getsockname()[0].split("%")[0]
        if not ipaddress.ip_address(address).is_loopback:
            return False
    return bool(sockets)


async def serve(
    args: argparse.Namespace, public: PublicConfig | None = None, *, periodic: Any = None
) -> int:
    sockets = bind_sockets(args.port, address=args.host)
    if not loopback_only(sockets):
        for sock in sockets:
            sock.close()
        sys.stderr.write("refusing to serve: a bound socket is not a loopback address\n")
        return 2
    port = sockets[0].getsockname()[1]
    app = make_app({"port": port, "max_sessions": args.max_sessions, "public": public})
    server = HTTPServer(app, **SERVER_OPTIONS)
    server.add_sockets(sockets)
    base = f"http://{URL_HOST[args.host]}:{port}/"
    lines = [base, base + "?view=judge"]
    callback = None
    if public is not None:
        lines.append(f"https://{public.host}/")
        callback = (periodic or PeriodicCallback)(lambda: tick(app), TICK_MS)
        callback.start()
    sys.stdout.write("".join(line + "\n" for line in lines))
    sys.stdout.flush()
    try:
        await asyncio.Event().wait()
    finally:
        if callback is not None:
            callback.stop()
        server.stop()
        app.settings["executor"].shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m corridor.web")
    parser.add_argument("--host", default="127.0.0.1", choices=LOOPBACK)
    parser.add_argument("--port", type=int, default=8601)
    parser.add_argument("--max-sessions", type=int, default=None)
    public_options = parser.add_argument_group("public mode")
    public_options.add_argument(
        "--public-host",
        default=None,
        help="the public DNS name; opens public mode (also CORRIDOR_WEB_PUBLIC_HOST)",
    )
    public_options.add_argument("--idle-seconds", type=float, default=None)
    public_options.add_argument("--evict-idle-after", type=float, default=None)
    public_options.add_argument("--per-ip-sessions", type=int, default=None)
    public_options.add_argument("--per-ip-new-sessions", default=None, metavar="COUNT/SECONDS")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        public = PublicConfig.from_args(args, os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    if args.max_sessions is None:
        args.max_sessions = 8 if public is None else public.max_sessions
    if args.max_sessions < 1:
        parser.error("--max-sessions must be at least 1")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # Tornado's own parser messages may quote request bytes; keep only its warnings.
    logging.getLogger("tornado.general").setLevel(logging.WARNING)
    if public is not None:
        # The model client's libraries would log request bodies at DEBUG: warnings only.
        quiet_client_logs()
    try:
        return asyncio.run(serve(args, public))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
