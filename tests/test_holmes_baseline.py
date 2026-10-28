"""F14 step 1 (#396) harness: offline checks of the pinned HolmesGPT config,
container arguments and outcome projection. No docker, kind or network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.holmes_baseline import config as hconfig
from scripts.holmes_baseline import container
from scripts.holmes_baseline.config import holmes_config
from scripts.holmes_baseline.container import run_args
from scripts.holmes_baseline.project import project_outcome

ROOT = Path(__file__).resolve().parents[1]
PROM = "http://f14-lab-relay:9090"
JAEGER = "http://f14-lab-relay:16686/jaeger/ui"


# --- config


def test_only_metrics_and_get_only_jaeger_are_enabled() -> None:
    toolsets = holmes_config(PROM, JAEGER)["toolsets"]
    enabled = {name for name, ts in toolsets.items() if ts.get("enabled")}
    assert enabled == {"prometheus/metrics", "jaeger"}
    for name in ("bash", "internet", "kubernetes/core", "kubernetes/logs", "robusta"):
        assert toolsets[name] == {"enabled": False}
    # every builtin of the pinned image is named, so enable_all cannot add one
    assert set(hconfig.BUILTIN_TOOLSETS) - {"prometheus/metrics"} <= set(toolsets)


def test_prometheus_goes_through_the_proxy_without_a_secret_in_the_file() -> None:
    prom = holmes_config(PROM, JAEGER)["toolsets"]["prometheus/metrics"]["config"]
    assert prom["prometheus_url"] == PROM
    assert prom["additional_headers"] == {
        "Authorization": "{{ env.HOLMES_PROMETHEUS_AUTHORIZATION }}"
    }
    assert prom["query_timeout_seconds_hard_max"] == 30
    assert "Basic " not in json.dumps(holmes_config(PROM, JAEGER))


@pytest.mark.parametrize(
    "url,paths",
    [
        (JAEGER, ["/jaeger/ui/api/*"]),
        ("http://jaeger:16686", ["/api/*"]),
        ("http://jaeger:16686/", ["/api/*"]),
    ],
)
def test_jaeger_endpoint_is_get_only_on_its_api(url: str, paths: list[str]) -> None:
    jaeger = holmes_config(PROM, url)["toolsets"]["jaeger"]
    assert jaeger["type"] == "http"
    [endpoint] = jaeger["config"]["endpoints"]
    host = url.split("//", 1)[1].split("/", 1)[0]
    assert endpoint["hosts"] == [host]
    assert endpoint["paths"] == paths
    assert endpoint["methods"] == ["GET"]
    assert "auth" not in endpoint


def test_model_and_budgets_match_opspilot_by_configuration() -> None:
    cfg = holmes_config(PROM, JAEGER)
    assert cfg["model"] == hconfig.MODEL and cfg["max_steps"] == 100
    env = hconfig.MODEL_ENV
    assert json.loads(env["THINKING"]) == {"type": "enabled"}
    assert env["REASONING_EFFORT"] == "high"
    # MAX_OUTPUT_TOKEN_RESERVATION is read but unused at a0009195; the output
    # budget the request carries is OVERRIDE_MAX_OUTPUT_TOKEN (holmes/core/llm.py)
    assert env["OVERRIDE_MAX_OUTPUT_TOKEN"] == "65536"
    assert env["OVERRIDE_MAX_CONTENT_SIZE"] == "1000000"
    assert env["LLM_REQUEST_TIMEOUT"] == "360"
    assert env["TEMPERATURE"] == "none"
    assert hconfig.RUN_WALL_SECONDS == 7200
    # no trace export (U8)
    assert not any(k.startswith(("OTEL_", "BRAINTRUST", "SENTRY")) for k in env)


# --- container


def args() -> list[str]:
    return run_args(
        "opspilot-f14/holmesgpt:a0009195",
        network="f14-holmes-internal",
        egress_proxy="http://f14-egress:3128",
        config_path="/abs/config.yaml",
        output_dir="/abs/out",
    )


def test_container_is_non_root_read_only_and_capless() -> None:
    a = args()
    assert a[:2] == ["docker", "run"]
    user = a[a.index("--user") + 1]
    assert not user.startswith("0") and user.split(":")[0] != "root"
    for flag in ("--read-only",):
        assert flag in a
    assert a[a.index("--cap-drop") + 1] == "ALL"
    assert a[a.index("--security-opt") + 1] == "no-new-privileges"
    assert a[a.index("--network") + 1] == "f14-holmes-internal"
    assert "--privileged" not in a


def test_only_config_and_output_are_mounted() -> None:
    a = args()
    mounts = [a[i + 1] for i, x in enumerate(a) if x in ("-v", "--volume", "--mount")]
    assert mounts == ["/abs/config.yaml:/etc/holmes/config.yaml:ro", "/abs/out:/out:rw"]
    joined = " ".join(a)
    for forbidden in ("docker.sock", "kube", ".env", "/Users/", "production-ops-agent"):
        assert forbidden not in joined


def test_env_names_are_allowlisted_and_secrets_never_on_argv() -> None:
    a = args()
    names = [a[i + 1].split("=", 1)[0] for i, x in enumerate(a) if x in ("-e", "--env")]
    assert set(names) <= set(container.ENV_ALLOWLIST)
    assert "--env-file" not in a
    pairs = dict(
        a[i + 1].split("=", 1) for i, x in enumerate(a) if x == "-e" and "=" in a[i + 1]
    )
    assert pairs["HTTPS_PROXY"] == "http://f14-egress:3128"
    for secret in container.SECRET_ENV:
        assert secret in names and secret not in pairs
    assert not any(
        n.startswith(("OTEL_", "BRAINTRUST", "SENTRY", "KUBE")) for n in names
    )


# --- projection

LOG = [
    "2026-10-28T12:00:00+00:00 holmes GET /api/v1/query 200 312 120 0.004",
    "2026-10-28T12:00:05+00:00 holmes POST /api/v1/query_range 200 512 9000 0.020",
    "2026-10-28T12:00:06+00:00 holmes POST /api/v1/query 200 500 800 0.010",
    "2026-10-28T12:00:07+00:00 holmes GET /api/v1/metadata 403 300 0 0.000",
    "2026-10-28T12:00:08+00:00 holmes POST /api/v1/query 422 300 90 0.001",
    "2026-10-28T12:00:09+00:00 investigator GET /api/v1/query 200 300 90 0.001",
    "not an access log line",
]


def completed() -> dict[str, object]:
    return {
        "result": "checkout errors come from payment",
        "num_llm_calls": 4,
        "tool_calls": [{"tool_name": "execute_prometheus_range_query"}] * 3,
    }


def test_completed_run_projection() -> None:
    out = project_outcome(completed(), LOG)
    assert out == {
        "final_state": "completed",
        "report_available": True,
        "tool_calls": 3,
        "proxied_queries_ok": 3,
        "model_calls_ok": 4,
        "handoff_reasons": [],
    }


@pytest.mark.parametrize(
    "extra,state",
    [
        ({"timed_out": True}, "budget_exhausted"),
        (
            {
                "exit_code": 1,
                "error": "Too many LLM calls - exceeded max_steps: 100/100",
            },
            "budget_exhausted",
        ),
        ({"exit_code": 1, "error": "boom"}, "failed"),
        ({"result": None}, "failed"),
        ({"result": "  "}, "failed"),
    ],
)
def test_terminal_states(extra: dict[str, object], state: str) -> None:
    out = project_outcome({**completed(), **extra}, [])
    assert out["final_state"] == state
    assert out["handoff_reasons"] == []
    assert out["proxied_queries_ok"] == 0
    if state != "completed":
        assert out["report_available"] is bool(
            isinstance(extra.get("result", "x"), str)
            and extra.get("result", "x").strip()
        )


def test_empty_output_is_failed() -> None:
    assert project_outcome({}, []) == {
        "final_state": "failed",
        "report_available": False,
        "tool_calls": 0,
        "proxied_queries_ok": 0,
        "model_calls_ok": 0,
        "handoff_reasons": [],
    }


def test_pin_record_is_marked_non_reproducible() -> None:
    pin = json.loads((ROOT / "docs/evidence/f14-holmes-baseline/pin.json").read_text())
    assert pin["reproducible"] is False
    assert pin["upstream"]["commit"].startswith("a0009195")
    for key in ("image_id", "archive_sha256", "base_images", "license"):
        assert pin[key]
    assert pin["model"]["name"] == hconfig.MODEL
    assert pin["budgets"]["max_steps"] == 100


# --- relay / capture helpers


def test_egress_tunnels_only_the_allowlisted_target() -> None:
    from scripts.holmes_baseline.relay import parse_connect

    allow = frozenset({"api.deepseek.com:443"})
    connect = b"CONNECT api.deepseek.com:443 HTTP/1.1\r\nHost: x\r\n\r\n"
    assert parse_connect(connect, allow) == ("api.deepseek.com:443", "allowlisted")
    for head in (
        b"CONNECT api.deepseek.com:80 HTTP/1.1\r\n\r\n",
        b"CONNECT example.com:443 HTTP/1.1\r\n\r\n",
        b"GET http://api.deepseek.com/ HTTP/1.1\r\n\r\n",
        b"\xff\xfe\r\n\r\n",
    ):
        target, _ = parse_connect(head, allow)
        assert target is None


def test_request_check_requires_thinking_and_effort() -> None:
    from scripts.holmes_baseline.capture import request_check

    body = {
        "model": "deepseek-flash",
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": 65536,
    }
    assert request_check(body)["ok"] is True
    nested = {"model": "m", "extra_body": {"thinking": {"type": "enabled"}}}
    assert request_check({**nested, "reasoning_effort": "high"})["ok"] is True
    assert request_check({"model": "m", "reasoning_effort": "high"})["ok"] is False
    assert request_check({"model": "m", "thinking": {"type": "enabled"}})["ok"] is False
