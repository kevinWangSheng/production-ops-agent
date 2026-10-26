"""F11 PR-5c kind acceptance window: engineer-only lab helpers.

Run as ``python -m scripts.f11_pr5c_lab <command>`` from the worktree. Every
release reaches the product only over HTTP: the harness webhook for intake,
and the workbench page route ``POST /version-changes/{id}/control`` (Basic
Auth, Origin, ``expected_control_generation``, idempotency key) for pause and
resume. Nothing here calls a storage primitive or writes the cluster; the
snapshot command only reads it.

Raw outputs go to the shared ledger root (``tmp/lab-ledgers/<batch>/``);
credentials are read from private files named by environment variables and
never printed:

* ``F11_5C_UI_CRED``  JSON ``{"user": ..., "password": ...}`` (private file)
* ``F11_5C_DSN``      read-only DSN for DB readouts (owner on the one-off PG)
* ``OPSPILOT_ENV_FILE`` private ``KEY=value`` file with ``DEEPSEEK_API_KEY``
  (balance command only)

Commands: ``readings``, ``snap``, ``diff``, ``page``, ``control``,
``pause-on-authorize``, ``wait``, ``exec-env``, ``balance``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts import kind_lab
from scripts.f11_u2_harness import resolve_ledger_root

WEB = "http://127.0.0.1:8080"
ORIGIN = "http://127.0.0.1:8080"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: The 23 policy-surface kinds of the PR-3 / PR-4b U5 snapshot (same list).
KINDS = [
    "deployments.apps",
    "statefulsets.apps",
    "daemonsets.apps",
    "replicasets.apps",
    "controllerrevisions.apps",
    "horizontalpodautoscalers.autoscaling",
    "poddisruptionbudgets.policy",
    "networkpolicies.networking.k8s.io",
    "ingresses.networking.k8s.io",
    "ingressclasses.networking.k8s.io",
    "services",
    "configmaps",
    "cronjobs.batch",
    "jobs.batch",
    "validatingwebhookconfigurations.admissionregistration.k8s.io",
    "mutatingwebhookconfigurations.admissionregistration.k8s.io",
    "validatingadmissionpolicies.admissionregistration.k8s.io",
    "validatingadmissionpolicybindings.admissionregistration.k8s.io",
    "customresourcedefinitions.apiextensions.k8s.io",
    "roles.rbac.authorization.k8s.io",
    "rolebindings.rbac.authorization.k8s.io",
    "clusterroles.rbac.authorization.k8s.io",
    "clusterrolebindings.rbac.authorization.k8s.io",
]
EXCLUDED_PREFIXES = ("deployments.apps/", "replicasets.apps/")
EXCLUDED_KEYS = ("configmaps/otel-demo/flagd-config",)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ledger(batch: str) -> Path:
    path = resolve_ledger_root() / batch
    path.mkdir(parents=True, exist_ok=True)
    return path


def append(batch: str, record: dict[str, Any]) -> None:
    with (ledger(batch) / "timeline.jsonl").open("a") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def h(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


# -- cluster (read-only) ----------------------------------------------------


def snap(batch: str, name: str) -> dict[str, Any]:
    out: dict[str, Any] = {"taken_at": now(), "objects": {}}
    for kind in KINDS:
        raw = subprocess.check_output(
            kind_lab.kubectl("get", kind, "-A", "-o", "json", "--show-managed-fields"),
            timeout=60,
        )
        for item in json.loads(raw)["items"]:
            meta = item["metadata"]
            body = {k: v for k, v in item.items() if k not in ("metadata", "status")}
            key = f"{kind}/{meta.get('namespace', '')}/{meta['name']}"
            out["objects"][key] = dict(
                uid=meta["uid"],
                generation=meta.get("generation"),
                body_sha256=h(body),
                labels_sha256=h(meta.get("labels")),
                annotations_sha256=h(meta.get("annotations")),
                managers=sorted(
                    [
                        f.get("manager"),
                        f.get("operation"),
                        f.get("time"),
                        f.get("subresource") or "",
                    ]
                    for f in meta.get("managedFields", [])
                    if f.get("subresource") != "status"
                ),
            )
    out["taken_until"] = now()
    path = ledger(batch) / "snapshots" / f"{name}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1, sort_keys=True))
    append(
        batch,
        dict(
            kind="snapshot", name=name, at=out["taken_at"], objects=len(out["objects"])
        ),
    )
    return dict(name=name, objects=len(out["objects"]), taken_at=out["taken_at"])


def diff(batch: str, first: str, second: str) -> dict[str, Any]:
    root = ledger(batch) / "snapshots"
    a = json.loads((root / f"{first}.json").read_text())["objects"]
    b = json.loads((root / f"{second}.json").read_text())["objects"]
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = {
        k: sorted(f for f in a[k] if a[k][f] != b[k][f])
        for k in sorted(set(a) & set(b))
        if a[k] != b[k]
    }

    def outside(key: str) -> bool:
        return not key.startswith(EXCLUDED_PREFIXES) and key not in EXCLUDED_KEYS

    touched = added + removed + list(changed)
    managers = {
        k: [m for m in b[k]["managers"] if m not in a[k]["managers"]]
        for k in changed
        if "managers" in changed[k]
    }
    return dict(
        first=first,
        second=second,
        kinds=len(KINDS),
        objects_compared=len(set(a) & set(b)),
        added=added,
        removed=removed,
        changed=changed,
        new_manager_entries=managers,
        changed_outside_deployment_replicaset_flagd=sorted(filter(outside, touched)),
    )


# -- workbench (HTTP only) --------------------------------------------------


def _auth() -> str:
    cred = json.loads(Path(os.environ["F11_5C_UI_CRED"]).read_text())
    raw = f"{cred['user']}:{cred['password']}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def http(
    method: str,
    path: str,
    *,
    form: dict[str, str] | None = None,
    accept: str = "application/json",
) -> tuple[int, bytes, dict[str, str]]:
    headers = {"Authorization": _auth(), "Accept": accept}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = ORIGIN
    request = urllib.request.Request(
        WEB + path, data=data, method=method, headers=headers
    )
    try:
        with OPENER.open(request, timeout=20) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def page(observation_id: str) -> dict[str, Any]:
    status, raw, _ = http("GET", f"/version-changes/{observation_id}")
    if status != 200:
        raise SystemExit(f"page {observation_id}: HTTP {status} {raw[:200]!r}")
    return json.loads(raw)


def brief(p: dict[str, Any]) -> dict[str, Any]:
    return dict(
        status=p["status"],
        verdict=p["verdict"],
        paused=p["paused"],
        ended_reason=p["ended_reason"],
        hold=p["authorization_hold"],
        control_generation=p["control_generation"],
        observation_generation=p["observation_generation"],
        used_samples=p["used_sample_count"],
        sessions=[
            (s.get("session_id", "")[:8], s.get("state"), s.get("ended_reason"))
            for s in p["sessions"]
        ],
        incident_id=p["incident_id"],
        audit=[
            (a.get("action"), a.get("outcome"), a.get("reason")) for a in p["audit"]
        ],
    )


def control(batch: str, observation_id: str, action: str, label: str) -> dict[str, Any]:
    before = page(observation_id)
    form = dict(
        action=action,
        expected_control_generation=str(before["control_generation"]),
        idempotency_key=label,
    )
    sent = now()
    status, raw, _ = http(
        "POST", f"/version-changes/{observation_id}/control", form=form
    )
    received = now()
    after = page(observation_id)
    record = dict(
        kind="page_control",
        observation_id=observation_id,
        action=action,
        idempotency_label=label,
        expected_control_generation=before["control_generation"],
        origin=ORIGIN,
        request_before=sent,
        request_after=received,
        http_status=status,
        response=json.loads(raw)
        if raw[:1] == b"{"
        else raw[:300].decode(errors="replace"),
        before=brief(before),
        after=brief(after),
    )
    append(batch, record)
    (ledger(batch) / "controls").mkdir(exist_ok=True)
    (ledger(batch) / "controls" / f"{label}.json").write_text(
        json.dumps(record, indent=1, sort_keys=True)
    )
    return record


def pause_on_authorize(
    batch: str, observation_id: str, label: str, limit: float
) -> dict[str, Any]:
    """Wait until the web process's automatic pass authorized the release,
    then pause it through the page route before the first sample (due 60 s
    after authorization)."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        p = page(observation_id)
        live = [s for s in p["sessions"] if s.get("state") == "authorized"]
        if p["status"] == "observing" and live:
            seen = now()
            result = control(batch, observation_id, "pause", label)
            result["authorization_seen_at"] = seen
            return result
        if p["status"] not in ("pending", "observing"):
            raise SystemExit(f"release ended before authorization: {brief(p)}")
        time.sleep(1.0)
    raise SystemExit("not authorized within limit")


def wait(
    observation_id: str, statuses: list[str], limit: float, every: float
) -> dict[str, Any]:
    deadline = time.monotonic() + limit
    while True:
        p = page(observation_id)
        b = brief(p)
        print(now(), json.dumps(b, default=str), flush=True)
        if p["status"] in statuses or time.monotonic() + every > deadline:
            return b
        time.sleep(every)


# -- host and Prometheus readings ------------------------------------------


def prom(query: str) -> Any:
    url = "http://127.0.0.1:19090/api/v1/query?" + urllib.parse.urlencode(
        {"query": query}
    )
    status, body = kind_lab.get_json(url, authorization=kind_lab.lab_authorization())
    if status != 200 or not isinstance(body, dict):
        return f"HTTP {status}"
    return [(r["metric"], r["value"][1]) for r in body["data"]["result"]]


def readings() -> dict[str, Any]:
    swap = subprocess.run(
        ["sysctl", "vm.swapusage"], capture_output=True, text=True
    ).stdout.strip()
    return dict(
        at=now(),
        swap=swap,
        checkout_p95_ms=prom(
            'histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m])))'
        ),
        checkout_error_ratio=prom(
            '(sum(rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder",status_code="STATUS_CODE_ERROR"}[5m])) or vector(0)) / sum(rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m]))'
        ),
        frontend_span_rate=prom(
            'sum(rate(traces_span_metrics_calls_total{service_name="frontend"}[5m]))'
        ),
        alerts=prom('ALERTS{alertname="CheckoutPlaceOrderErrorRatioHigh"}'),
    )


def balance() -> Any:
    key = ""
    for line in Path(os.environ["OPSPILOT_ENV_FILE"]).read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "DEEPSEEK_API_KEY":
            key = value.strip().strip("'\"")
    request = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.loads(response.read())
    return dict(at=now(), balance_infos=body.get("balance_infos"))


def exec_env(out: str, argv: list[str]) -> None:
    """Record the sorted environment-variable NAMES (never values) this
    process starts with, then exec the product entry point in place."""
    Path(out).write_text(
        json.dumps(dict(at=now(), argv=argv, names=sorted(os.environ)), indent=1)
    )
    os.execvp(argv[0], argv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="f11_pr5c_lab")
    parser.add_argument("--batch", default="f11-5c-live")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("readings")
    s = sub.add_parser("snap")
    s.add_argument("name")
    d = sub.add_parser("diff")
    d.add_argument("first")
    d.add_argument("second")
    p = sub.add_parser("page")
    p.add_argument("oid")
    p.add_argument("--full", action="store_true")
    c = sub.add_parser("control")
    c.add_argument("oid")
    c.add_argument("action")
    c.add_argument("--label", required=True)
    a = sub.add_parser("pause-on-authorize")
    a.add_argument("oid")
    a.add_argument("--label", required=True)
    a.add_argument("--limit", type=float, default=120)
    w = sub.add_parser("wait")
    w.add_argument("oid")
    w.add_argument("--status", nargs="+", required=True)
    w.add_argument("--limit", type=float, default=540)
    w.add_argument("--every", type=float, default=60)
    e = sub.add_parser("exec-env")
    e.add_argument("out")
    e.add_argument("argv", nargs=argparse.REMAINDER)
    sub.add_parser("balance")
    args = parser.parse_args(argv)
    if args.cmd == "exec-env":
        exec_env(args.out, args.argv[1:] if args.argv[:1] == ["--"] else args.argv)
        return 0
    if args.cmd == "readings":
        result: Any = readings()
    elif args.cmd == "snap":
        result = snap(args.batch, args.name)
    elif args.cmd == "diff":
        result = diff(args.batch, args.first, args.second)
    elif args.cmd == "page":
        full = page(args.oid)
        result = full if args.full else brief(full)
    elif args.cmd == "control":
        result = control(args.batch, args.oid, args.action, args.label)
    elif args.cmd == "pause-on-authorize":
        result = pause_on_authorize(args.batch, args.oid, args.label, args.limit)
    elif args.cmd == "wait":
        result = wait(args.oid, args.status, args.limit, args.every)
    else:
        result = balance()
    print(json.dumps(result, indent=1, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
