"""Read-only calibration probe for the shipped checkout HealthProfile (#88).

Engineer script, not product code. Issue #88 asks, before the F6 real-lab
acceptance, for evidence that the two signals added by #122 without lab
evidence (``deployment_ready_replicas`` on
``kube_deployment_status_replicas_ready`` and
``deployment_available_replicas_min_in_window`` on ``min_over_time``) read
correctly on the kind lab: the raw series and their label shape, the
coverage count inside the evaluation window and the three instant answers
the Observer would get (``query`` / ``coverage_query`` / ``freshness_query``
at the same evaluation time). Every signal of the profile is probed the same
way so the whole profile is calibrated against one lab state. Uses the
``lab`` account (``kind_lab.lab_authorization``); never writes to the lab.

    .venv/bin/python docs/evidence/m1-02-live/probe_signals.py \
        --out docs/evidence/m1-02-live/calibration/probe.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from opspilot.observer import PROFILE_DIRECTORY, load_health_profile  # noqa: E402
from scripts.kind_lab import OPENER, lab_authorization  # noqa: E402

PROFILE_PATH = PROFILE_DIRECTORY / "otel-demo-checkout.json"
SELECTOR = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*\{[^}]*\}")


def get(base: str, path: str, params: dict[str, object]) -> tuple[int, dict | None]:
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    authorization = lab_authorization()
    headers = {} if authorization is None else {"Authorization": authorization}
    request = urllib.request.Request(url, headers=headers)
    try:
        with OPENER.open(request, timeout=20) as response:
            return response.status, json.loads(response.read(2 * 1024 * 1024))
    except urllib.error.HTTPError as error:
        return error.code, None


def instant(base: str, expr: str, at: int) -> dict:
    status, payload = get(base, "/api/v1/query", {"query": expr, "time": at})
    result = [] if payload is None else payload.get("data", {}).get("result", [])
    return {
        "expr": expr,
        "http_status": status,
        "result_type": None if payload is None else payload["data"]["resultType"],
        "series": len(result),
        "result": result,
    }


def selectors(profile) -> dict[str, str]:
    """Every distinct ``metric{labels}`` selector the profile's queries use."""
    found: dict[str, str] = {}
    for signal in profile.signals:
        for expr in (signal.query, signal.coverage_query, signal.freshness_query):
            for match in SELECTOR.findall(expr):
                found.setdefault(match, signal.name)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", default="http://127.0.0.1:19090")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--range-minutes", type=int, default=10)
    args = parser.parse_args(argv)
    profile = load_health_profile(PROFILE_PATH)
    at = int(time.time())
    window = profile.evaluation_window_seconds
    document: dict[str, object] = {
        "probed_at": datetime.fromtimestamp(at, tz=timezone.utc).isoformat(),
        "profile_revision": profile.revision,
        "evaluation_window_seconds": window,
        "freshness_seconds": profile.freshness_seconds,
        "signals": [],
        "raw_series": [],
    }
    for signal in profile.signals:
        document["signals"].append(  # type: ignore[union-attr]
            {
                "name": signal.name,
                "required": signal.required,
                "traffic_dependent": signal.traffic_dependent,
                "minimum_samples": signal.minimum_samples,
                "healthy": signal.healthy.model_dump(mode="json"),
                "query": instant(args.prometheus, signal.query, at),
                "coverage_query": instant(args.prometheus, signal.coverage_query, at),
                "freshness_query": instant(args.prometheus, signal.freshness_query, at),
            }
        )
    # the selectors themselves: label shape, raw points inside the window
    # (count and timestamps), and the last ``range-minutes`` of values
    for selector, owner in selectors(profile).items():
        status, payload = get(
            args.prometheus,
            "/api/v1/query_range",
            {
                "query": selector,
                "start": at - args.range_minutes * 60,
                "end": at,
                "step": 15,
            },
        )
        rows = [] if payload is None else payload["data"]["result"]
        document["raw_series"].append(  # type: ignore[union-attr]
            {
                "selector": selector,
                "first_signal": owner,
                "http_status": status,
                "series": [
                    {
                        "labels": row["metric"],
                        "points_in_range": len(row["values"]),
                        "first": row["values"][0] if row["values"] else None,
                        "last": row["values"][-1] if row["values"] else None,
                        "distinct_values": sorted({v for _, v in row["values"]}),
                    }
                    for row in rows
                ],
                "raw_points_in_window": instant(
                    args.prometheus, f"count_over_time({selector}[{window}s])", at
                ),
            }
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    for item in document["signals"]:  # type: ignore[union-attr]
        q, c, f = item["query"], item["coverage_query"], item["freshness_query"]
        value = q["result"][0]["value"][1] if q["result"] else None
        coverage = c["result"][0]["value"][1] if c["result"] else None
        fresh = f["result"][0]["value"][1] if f["result"] else None
        age = None if fresh is None else round(at - float(fresh), 1)
        print(
            f"{item['name']:45} value={value!s:>12} coverage={coverage!s:>4} "
            f"freshness_age_s={age!s:>6} healthy={item['healthy']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
