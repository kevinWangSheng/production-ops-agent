"""Engineer-only reversible flagd fault for the held-out scenarios (phase B).

Same safety rules as ``scripts/m0_environment/development_fault.py`` (left
untouched): the original bytes are captured before the edit, a repeat
injection into an existing experiment is refused, a restore refuses to
overwrite a file that no longer matches the injected snapshot, and every
action is appended to a per-experiment timeline with before/after SHA256.
Unlike that script the flag and variant are parameters, so the held-out
faults (``productCatalogFailure``, ``cartFailure``) can be driven without
editing the M0 hook.

usage: OPSPILOT_OTEL_LAB=<lab dir> python heldout_fault.py inject|restore \\
           --experiment-id <id> --flag <flagd flag> [--variant on]

The lab directory is the one ``scripts/otel_demo_lab.py`` mounts; the flags
file is ``src/flagd/demo.flagd.json`` of the pinned demo checkout inside it.
The investigator and the model have no entry to this script.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path

DEMO = "opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["inject", "restore"])
    ap.add_argument("--experiment-id", required=True)
    ap.add_argument("--flag", required=True)
    ap.add_argument("--variant", default="on")
    args = ap.parse_args()
    if not args.experiment_id.replace("-", "").isalnum():
        raise SystemExit("invalid experiment identity")
    lab = Path(os.environ["OPSPILOT_OTEL_LAB"])
    flags = lab / DEMO / "src/flagd/demo.flagd.json"
    history = lab / "engineer-only" / args.experiment_id
    history.mkdir(parents=True, exist_ok=True)
    original = history / "flags-original.json"
    changed = history / "flags-injected.json"
    raw = flags.read_bytes()
    if args.action == "inject":
        if original.exists() or changed.exists():
            raise SystemExit("Existing experiment history; refuse repeat injection.")
        data = json.loads(raw)
        flag = data["flags"].get(args.flag)
        if flag is None or args.variant not in flag["variants"]:
            raise SystemExit(f"flag {args.flag} / variant {args.variant} not in flagd file")
        if flag["defaultVariant"] != "off":
            raise SystemExit("Expected normal flag state not found.")
        for name, other in data["flags"].items():
            if name != args.flag and other["defaultVariant"] != "off":
                raise SystemExit(f"another flag is already active: {name}")
        flag["defaultVariant"] = args.variant
        payload = (json.dumps(data, indent=2) + "\n").encode()
        original.write_bytes(raw)
        changed.write_bytes(payload)
    else:
        if raw != changed.read_bytes():
            raise SystemExit("Current flags differ from injected snapshot; refuse overwrite.")
        payload = original.read_bytes()
    flags.write_bytes(payload)
    entry = {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "action": args.action,
        "flag": args.flag,
        "variant": args.variant,
        "before_sha256": hashlib.sha256(raw).hexdigest(),
        "after_sha256": hashlib.sha256(payload).hexdigest(),
    }
    with (history / "fault-timeline.jsonl").open("a") as stream:
        stream.write(json.dumps(entry) + "\n")
    print(json.dumps(entry))


if __name__ == "__main__":
    main()
