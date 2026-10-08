"""Engineer-side probe: the investigation side's read-only Prometheus tool
against the authenticated lab (M1-02 step 4, D3).

Not product code and not a model Run: it drives the product transport
(``opspilot.tools.otel_demo.OtelDemoTransport``, the one seam the
investigation gateway uses) exactly as the worker would configure it from
``OPSPILOT_OTEL_*``, once with the investigator credential (expect 200 and a
parsed range result), once anonymously and once with the Observer's account
variables placed where the investigation side would have to read them if it
fell back (expect 401 / no credential). Writes a summary without any
credential value.

    set -a; . tmp/m1-kind-lab/prometheus-investigator.env; set +a
    .venv/bin/python docs/evidence/m1-02-observer/investigator_probe.py --out <summary.json>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from opspilot.tools.executor import TransportRequest  # noqa: E402
from opspilot.tools.otel_demo import (  # noqa: E402
    CREDENTIAL_REF,
    METRICS_TOOL,
    SOURCE,
    TARGET_ID,
    OtelDemoConfig,
    OtelDemoTransport,
)
from opspilot.tools.outcomes import Window  # noqa: E402

EXPR = 'sum(rate(traces_span_metrics_calls_total{service_name="checkout",span_kind="SPAN_KIND_SERVER"}[5m]))'


def _request(config: OtelDemoConfig, end: datetime) -> TransportRequest:
    return TransportRequest(
        operation_id="probe-1",
        source=SOURCE,
        verb="query",
        endpoint=config.prometheus_url,
        selector={
            "opspilot.integration.id": TARGET_ID,
            "traces_endpoint": config.jaeger_url,
        },
        params={"expr": EXPR, "step_seconds": 15},
        window=Window(start=end - timedelta(minutes=10), end=end),
        timeout_seconds=20.0,
        max_result_bytes=1_048_576,
        credential_ref=CREDENTIAL_REF,
        tool=METRICS_TOOL,
    )


def _probe(config: OtelDemoConfig, end: datetime) -> dict[str, object]:
    pair = config.prometheus_basic_auth
    transport = OtelDemoTransport(
        credentials={CREDENTIAL_REF: config.token},
        basic_auth={} if pair is None else {CREDENTIAL_REF: pair},
    )
    response = transport.fetch(_request(config, end))
    points = None
    if response.source_status is None:
        try:
            payload = json.loads(response.body)
            series = payload["data"]["result"]
            points = sum(len(item.get("values", [])) for item in series)
        except (ValueError, KeyError, TypeError):
            points = None
    return {
        "account": None if pair is None else pair[0],
        "source_status": response.source_status,
        "body_bytes": len(response.body),
        "range_points": points,
        "data_as_of": None
        if response.data_as_of is None
        else response.data_as_of.isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    end = datetime.now(timezone.utc)  # noqa: TID251 - engineer script
    env = dict(os.environ)
    configured = OtelDemoConfig.from_env(env)
    anonymous = OtelDemoConfig.from_env(
        {k: v for k, v in env.items() if not k.startswith("OPSPILOT_OTEL_PROMETHEUS_")}
    )
    # the Observer's variables, present in the environment, are not read by
    # the investigation side: the resulting config has no credential
    observer_only = OtelDemoConfig.from_env(
        {
            **{
                k: v
                for k, v in env.items()
                if not k.startswith("OPSPILOT_OTEL_PROMETHEUS_")
            },
            "OPSPILOT_OBSERVER_PROMETHEUS_USERNAME": "observer",
            "OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD": "not-read-here",
        }
    )
    summary = {
        "probed_at": end.isoformat(),
        "prometheus_url": configured.prometheus_url,
        "expr": EXPR,
        "investigator": _probe(configured, end),
        "anonymous": _probe(anonymous, end),
        "observer_variables_only": {
            "config_has_credential": observer_only.prometheus_basic_auth is not None,
            **_probe(observer_only, end),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
