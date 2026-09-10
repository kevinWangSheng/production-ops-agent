"""Engineer-only reversible development fault; never mount into investigator.

This is a visible development case, not a held-out evaluation. Captured original
bytes are preserved; restoration refuses to overwrite an unexpected edit.
"""

import argparse
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "tmp/m0-environment"
FLAGS = (
    LAB
    / "opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad/src/flagd/demo.flagd.json"
)
HISTORY = LAB / "engineer-only"
parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["inject", "restore"])
args = parser.parse_args()
HISTORY.mkdir(exist_ok=True)
original = HISTORY / "flags-original.json"
changed = HISTORY / "flags-injected.json"
raw = FLAGS.read_bytes()
if args.action == "inject":
    if original.exists() or changed.exists():
        raise SystemExit("Existing experiment history; refuse repeat injection.")
    data = json.loads(raw)
    if data["flags"]["paymentFailure"]["defaultVariant"] != "off":
        raise SystemExit("Expected normal flag state not found.")
    data["flags"]["paymentFailure"]["defaultVariant"] = "100%"
    payload = (json.dumps(data, indent=2) + "\n").encode()
    original.write_bytes(raw)
    changed.write_bytes(payload)
else:
    if raw != changed.read_bytes():
        raise SystemExit(
            "Current flags differ from injected snapshot; refuse overwrite."
        )
    payload = original.read_bytes()
FLAGS.write_bytes(payload)
with (HISTORY / "fault-timeline.jsonl").open("a") as stream:
    stream.write(
        json.dumps(
            {
                "at": datetime.datetime.now(datetime.UTC).isoformat(),
                "action": args.action,
                "before_sha256": hashlib.sha256(raw).hexdigest(),
                "after_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
        + "\n"
    )
print(args.action + " completed in the dedicated developer workload only")
