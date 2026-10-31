"""``python -m corridor.web.ledger_setup``: start or check a public site's spend ledger.

A public site never creates its ledger itself (``SpendLedger(create=False)``): if the file
disappears, live access closes instead of a new budget appearing. This tool is the one way
to start a ledger, run as the service account:

    --spent-cny AMOUNT  creates the ledger with AMOUNT yuan already spent. ``enable_live.sh``
                        passes 0 on the first run. After a loss the operator passes the
                        total reconciled against the provider's bill, so the cumulative
                        budget carries on. An existing file is never touched: move it
                        away first, it may be evidence.
    --check             exits 0 when the ledger is there and answers, 1 otherwise.

The directory is an argument so that the tool works under a test root; the site itself
only ever uses ``corridor.web.public.JOURNAL_PATH``. The tool reads no credential.
"""

import argparse
import re
import sys
from fractions import Fraction
from pathlib import Path

from corridor.ui.live_runtime import BudgetExceeded, SpendLedger
from corridor.web.public import DEPLOYMENTS

CURRENCY = "CNY"
AMOUNT = re.compile(r"[0-9]{1,9}(\.[0-9]{1,6})?")


def amount(text: str) -> Fraction:
    if not AMOUNT.fullmatch(text):
        raise argparse.ArgumentTypeError("the amount is yuan as a plain decimal, for example 37.5")
    return Fraction(text)


def check(directory: Path) -> int:
    try:
        SpendLedger(directory, create=False).unreconciled()
    except BudgetExceeded:
        sys.stderr.write("The ledger is missing or unreadable.\n")
        return 1
    return 0


def start(directory: Path, spent: Fraction) -> int:
    ledger = SpendLedger(directory)
    if ledger.path.exists():
        sys.stderr.write("A ledger file is already there; this tool never replaces one.\n")
        return 1
    try:
        ledger.initialize(CURRENCY, next(iter(DEPLOYMENTS)), spent)
    except BudgetExceeded:
        sys.stderr.write("The ledger could not be started.\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m corridor.web.ledger_setup",
        description="Start a public site's spend ledger, or check the one that is there.",
    )
    parser.add_argument("--journal-dir", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--spent-cny", type=amount, help="yuan already spent; 0 on a first run")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.journal_dir)
    return start(args.journal_dir, args.spent_cny)


if __name__ == "__main__":
    sys.exit(main())
