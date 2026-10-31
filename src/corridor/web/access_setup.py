"""``python -m corridor.web.access_setup``: draw the judges' access code, print its verifier.

The code is drawn here from ``secrets`` (``token_urlsafe(24)``: 192 bits, more than the 128
RW-8 item 8 asks for) and is never accepted from the operator: the tool takes no argument
and reads nothing. It is shown exactly once, on standard error, for the operator to hand to
the judges through the submission form's remarks and write nowhere else (RW-7 item 5).

Standard output holds the two configuration lines and nothing else, so the operator may
append it straight to the environment file (``>> /etc/corridor/web-public.env``) without
the code ever reaching that file:

    CORRIDOR_WEB_ACCESS_KDF=pbkdf2-sha256$200000$<salt hex>
    LIVE_ACCESS_CODE=<verifier: PBKDF2-HMAC-SHA256 of the code, 64 hex digits>

The verifier is what the server compares against and what the browser derives from the
typed code (``static/js/kdf.js``); on the protocol it is equivalent to the code, so the
environment file is for root and the service account only (RW-8 item 11). A new run draws
a new code, a new salt and a new verifier; replace both lines together.
"""

import argparse
import secrets
import sys

from corridor.ui.live_config import access_code_strong
from corridor.web.public import KDF_ENV, KDF_SALT_BYTES, VERIFIER_ENV, Kdf

CODE_BYTES = 24


def draw_code() -> str:
    """A fresh random code the product layer accepts as strong (it always is, in practice;
    the loop only guards the one-in-many draw with too few distinct characters)."""
    while True:
        code = secrets.token_urlsafe(CODE_BYTES)
        if access_code_strong(code):
            return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m corridor.web.access_setup",
        description=(
            "Draw the judges' access code at random and print the two configuration lines "
            "(the derivation parameters and the verifier) on standard output; the code "
            "itself appears once on standard error. No argument sets the code."
        ),
    )
    parser.parse_args(argv)
    kdf = Kdf(salt=secrets.token_hex(KDF_SALT_BYTES))
    code = draw_code()
    sys.stdout.write(f"{KDF_ENV}={kdf.text()}\n{VERIFIER_ENV}={kdf.verifier(code)}\n")
    sys.stdout.flush()
    sys.stderr.write(
        f"Access code, shown this once (hand it to the judges; write it nowhere):\n{code}\n"
    )
    sys.stderr.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
