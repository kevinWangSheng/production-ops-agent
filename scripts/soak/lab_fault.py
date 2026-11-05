"""Rental-host side of the soak's fault key (S-R2), called by ``fault.sh``.

``flag <name> on|off <item>`` moves one of the soak's flags (``plan.FLAGS``)
between ``off`` and its one fault variant in the lab's ``flagd-config``
ConfigMap and verifies the live bytes, like ``kind_lab.py fault`` (whose
whitelist stays the three eval flags). Both directions are idempotent: a
flag already in the wanted state is reported and left alone, so a retried
step never fails on its own earlier success. ``collector-restart`` is the
S-R13 environment action; ``alerts`` reads the lab Alertmanager. Every call appends a line to
``kind_lab.LAB_DIR/soak-fault.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone

from scripts import kind_lab

#: flag -> its fault variant; equal to ``plan.FLAGS`` (test-pinned).
SOAK_FLAGS = {
    "paymentFailure": "100%",
    "adManualGc": "on",
    "loadGeneratorFloodHomepage": "on",
}
ITEM = re.compile(r"[a-z0-9]{1,8}")
LOG = kind_lab.LAB_DIR / "soak-fault.jsonl"


def moved(raw: bytes, flag: str, variant: str) -> bytes | None:
    """``raw`` with ``flag`` set to ``variant``; ``None`` when it already is.
    Refuses a flag the file lacks, an unknown variant, or a flag found in a
    third state (neither ``off`` nor its fault variant)."""
    data = json.loads(raw)
    entry = data["flags"].get(flag)
    if not isinstance(entry, dict) or variant not in entry.get("variants", {}):
        raise SystemExit(f"{flag}/{variant} is not in {kind_lab.FLAG_FILE}; refusing")
    current = entry.get("defaultVariant")
    if current == variant:
        return None
    if current not in {kind_lab.NORMAL_VARIANT, SOAK_FLAGS[flag]}:
        raise SystemExit(
            f"{flag} is {current!r}, not off or its fault variant; refusing"
        )
    entry["defaultVariant"] = variant
    return (json.dumps(data, indent=2) + "\n").encode()


def _log(record: dict[str, object]) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as handle:
        handle.write(
            json.dumps({"at": datetime.now(timezone.utc).isoformat(), **record}) + "\n"
        )


def flag(name: str, state: str, item: str) -> int:
    if name not in SOAK_FLAGS or state not in {"on", "off"} or not ITEM.fullmatch(item):
        raise SystemExit("usage: flag <soak flag> on|off <item>")
    variant = SOAK_FLAGS[name] if state == "on" else kind_lab.NORMAL_VARIANT
    raw = kind_lab.read_live_flags()
    if raw is None:
        return 1
    payload = moved(raw, name, variant)
    if payload is None:
        _log({"item": item, "flag": name, "state": state, "changed": False})
        print(f"{name} already {variant}")
        return 0
    patch = json.dumps({"data": {kind_lab.FLAG_FILE: payload.decode()}})
    proc = kind_lab.capture(
        kind_lab.kubectl(
            "-n",
            kind_lab.NAMESPACE,
            "patch",
            "configmap",
            "flagd-config",
            "--type=merge",
            "-p",
            patch,
        ),
        timeout=60,
    )
    live = kind_lab.read_live_flags() if proc.returncode == 0 else None
    expected = hashlib.sha256(payload).hexdigest()
    verified = live is not None and hashlib.sha256(live).hexdigest() == expected
    _log(
        {
            "item": item,
            "flag": name,
            "state": state,
            "changed": True,
            "verified": verified,
            "after_sha256": expected,
        }
    )
    print(f"{name} -> {variant} verified={verified}")
    return 0 if verified else 2


def collector_restart() -> int:
    proc = kind_lab.capture(
        kind_lab.kubectl(
            "-n", kind_lab.NAMESPACE, "rollout", "restart", "deployment/otel-collector"
        ),
        timeout=120,
    )
    _log({"action": "collector-restart", "exit": proc.returncode})
    print(proc.stdout.strip() or proc.stderr.strip())
    return proc.returncode


ALERTS_PATH = (
    f"/api/v1/namespaces/{kind_lab.NAMESPACE}/services/"
    "alertmanager:9093/proxy/api/v2/alerts?active=true&silenced=false&inhibited=false"
)


def alerts() -> int:
    """The lab Alertmanager's active alerts (read-only, through the API
    server's service proxy): a JSON list of fingerprint, startsAt, labels."""
    proc = kind_lab.capture(kind_lab.kubectl("get", "--raw", ALERTS_PATH), timeout=60)
    if proc.returncode != 0:
        print(proc.stderr.strip()[-300:], file=sys.stderr)
        return proc.returncode or 1
    rows = [
        {
            "fingerprint": alert.get("fingerprint"),
            "startsAt": alert.get("startsAt"),
            "labels": alert.get("labels") or {},
        }
        for alert in json.loads(proc.stdout or "[]")
    ]
    print(json.dumps(rows, sort_keys=True))
    return 0


def main(argv: list[str]) -> int:
    if argv == ["alerts"]:
        return alerts()
    if len(argv) == 4 and argv[0] == "flag":
        return flag(argv[1], argv[2], argv[3])
    if argv == ["collector-restart"]:
        return collector_restart()
    raise SystemExit("usage: flag <name> on|off <item> | collector-restart")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
