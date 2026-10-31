"""Isolated E4 worker entry point; never starts a matrix by default."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from run_live_zh import worker  # noqa: E402

from corridor.bench.recording import network_disabled  # noqa: E402
from corridor.schemas.base import loads_exact  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env",
        help="Read live credentials only from this UTF-8 file.",
    )
    args = parser.parse_args()
    spec = loads_exact(args.worker.read_bytes())
    if args.strict:
        with network_disabled():
            worker(spec, args.output, "strict", env_file=args.env_file)
    else:
        worker(spec, args.output, "record", env_file=args.env_file)
