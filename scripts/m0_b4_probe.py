"""Run the already-reviewed PG/model probe under a fresh B4 allocation.

This wrapper changes only the in-process experimental profile/ledger identity;
it does not read credentials or expose provider-private response fields.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import replace
from pathlib import Path

from scripts import m0_pg_live_probe as probe
from scripts.m0_environment import round02


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("first", "second"))
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--allocation", default="m0-05-b4-20260912")
    parser.add_argument("--reservation", type=float, default=1.0)
    parser.add_argument("--crash-after", action="store_true")
    args = parser.parse_args()
    if args.crash_after and args.stage != "first":
        parser.error("--crash-after requires first stage")
    round02.PROFILE = replace(
        round02.PROFILE,
        allocation=args.allocation,
        deadline=args.deadline,
        reservation_cny=args.reservation,
    )
    probe.LEDGER = args.ledger
    result = probe.execute(args.stage, args.record)
    if args.crash_after:
        os._exit(17)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
