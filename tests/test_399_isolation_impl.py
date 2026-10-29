"""#399 Holmes isolation checklist: the pure ``evaluate`` rules and the probe
container shape. No docker, kind or network (the live gate is
``test_399_isolation_live_impl.py``)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from scripts.holmes_baseline import container, isolation
from scripts.holmes_baseline.isolation import CHECK_IDS, FORBIDDEN_PATHS, evaluate

GOOD_LOG = [
    "2026-10-28T18:42:06+00:00 holmes GET /api/v1/query 200 251 705 0.981",
    "2026-10-28T18:42:10+00:00 holmes GET /api/v1/label/__name__/values 200 4 2 0.9",
    "2026-10-28T18:42:19+00:00 holmes GET /api/v1/series 200 357 3039 1.498",
    "2026-10-28T18:42:19+00:00 holmes GET /api/v1/labels 200 357 172 2.469",
    "2026-10-28T18:42:25+00:00 holmes POST /api/v1/query 200 473 582 2.256",
    "2026-10-28T18:43:46+00:00 holmes POST /api/v1/query_range 200 987 93 2.298",
    "2026-10-28T18:43:47+00:00 holmes POST /api/v1/query 422 300 90 0.001",
]


def good() -> dict[str, Any]:
    return {
        "uid": 65534,
        "paths_present": [],
        "env_names": sorted(
            [*container.ENV_ALLOWLIST, "PATH", "HOSTNAME", "LANG", "PYTHONPATH"]
        ),
        "reach": {
            "kube_api": False,
            "prometheus_backend": False,
            "internet": False,
            "model_direct": False,
            "model_via_proxy": True,
        },
        "proxy_log_lines": list(GOOD_LOG),
    }


def check(result: dict[str, Any], check_id: str) -> dict[str, Any]:
    [row] = [c for c in result["checks"] if c["id"] == check_id]
    return row


def test_all_checks_pass_in_order() -> None:
    result = evaluate(good())
    assert [c["id"] for c in result["checks"]] == list(CHECK_IDS)
    assert all(c["passed"] for c in result["checks"]), result
    assert result["passed"] is True
    assert all(isinstance(c["detail"], str) for c in result["checks"])


def test_evaluate_does_not_mutate_its_input() -> None:
    observed = good()
    before = copy.deepcopy(observed)
    evaluate(observed)
    assert observed == before


@pytest.mark.parametrize("uid", [0, "65534", None, True])
def test_root_or_non_int_uid_fails(uid: object) -> None:
    observed = good()
    observed["uid"] = uid
    result = evaluate(observed)
    assert check(result, "uid_non_root")["passed"] is False
    assert result["passed"] is False


@pytest.mark.parametrize("check_id,key", [(c, k) for c, k in isolation.CHECK_KEYS])
def test_missing_key_fails_its_check(check_id: str, key: str) -> None:
    observed = good()
    del observed[key]
    result = evaluate(observed)
    assert check(result, check_id)["passed"] is False
    assert result["passed"] is False


def test_empty_observation_passes_nothing() -> None:
    result = evaluate({})
    assert not any(c["passed"] for c in result["checks"])


def test_present_forbidden_path_fails() -> None:
    observed = good()
    observed["paths_present"] = [FORBIDDEN_PATHS[0]]
    row = check(evaluate(observed), "forbidden_paths_absent")
    assert row["passed"] is False and FORBIDDEN_PATHS[0] in row["detail"]


def test_forbidden_paths_cover_repo_env_auth_and_kube() -> None:
    joined = " ".join(FORBIDDEN_PATHS)
    for needle in ("production-ops-agent", ".env", "prometheus-auth.json", ".kube"):
        assert needle in joined
    assert str(isolation.ROOT) in FORBIDDEN_PATHS
    assert "/var/run/docker.sock" in FORBIDDEN_PATHS


def test_unlisted_env_name_fails_and_values_never_reach_detail() -> None:
    observed = good()
    observed["env_names"] = [*observed["env_names"], "AWS_SECRET_ACCESS_KEY"]
    row = check(evaluate(observed), "env_names_allowlisted")
    assert row["passed"] is False and "AWS_SECRET_ACCESS_KEY" in row["detail"]

    observed["env_names"] = ["PATH", "LEAKED=sk-value-should-not-appear"]
    row = check(evaluate(observed), "env_names_allowlisted")
    assert row["passed"] is False
    assert "sk-value-should-not-appear" not in json.dumps(evaluate(observed))


def test_image_env_names_are_explicit_and_not_secrets() -> None:
    assert "PATH" in isolation.IMAGE_ENV_NAMES
    assert "HOSTNAME" in isolation.IMAGE_ENV_NAMES
    assert not set(isolation.IMAGE_ENV_NAMES) & set(container.SECRET_ENV)


@pytest.mark.parametrize(
    "check_id,key,value",
    [
        ("kube_api_unreachable", "kube_api", True),
        ("prometheus_backend_unreachable", "prometheus_backend", True),
        ("internet_unreachable", "internet", True),
        ("model_only_via_proxy", "model_direct", True),
        ("model_only_via_proxy", "model_via_proxy", False),
    ],
)
def test_reachability_rules(check_id: str, key: str, value: bool) -> None:
    observed = good()
    observed["reach"][key] = value
    result = evaluate(observed)
    assert check(result, check_id)["passed"] is False
    assert result["passed"] is False


@pytest.mark.parametrize(
    "key",
    ["kube_api", "prometheus_backend", "internet", "model_direct", "model_via_proxy"],
)
def test_missing_or_non_bool_reach_fails(key: str) -> None:
    by_key = {
        "kube_api": "kube_api_unreachable",
        "prometheus_backend": "prometheus_backend_unreachable",
        "internet": "internet_unreachable",
        "model_direct": "model_only_via_proxy",
        "model_via_proxy": "model_only_via_proxy",
    }
    observed = good()
    del observed["reach"][key]
    assert check(evaluate(observed), by_key[key])["passed"] is False
    observed = good()
    observed["reach"][key] = None
    assert check(evaluate(observed), by_key[key])["passed"] is False


@pytest.mark.parametrize(
    "line",
    [
        "2026-10-28T12:00:07+00:00 holmes GET /api/v1/metadata 403 300 0 0.000",
        "2026-10-28T12:00:07+00:00 holmes GET /api/v1/rules 200 300 0 0.000",
        "2026-10-28T12:00:07+00:00 holmes POST /api/v1/series 200 300 0 0.000",
        "2026-10-28T12:00:07+00:00 holmes DELETE /api/v1/query 200 300 0 0.000",
        "2026-10-28T12:00:07+00:00 holmes GET /api/v1/query 403 300 0 0.000",
        "2026-10-28T12:00:07+00:00 holmes GET /api/v1/query?query=up 200 3 0 0.0",
        "2026-10-28T12:00:07+00:00 holmes GET /api/v1/label/a/../../x/values 200 3 0 0",
        "2026-10-28T12:00:07+00:00 holmes GET",
    ],
)
def test_disallowed_proxy_log_line_fails(line: str) -> None:
    observed = good()
    observed["proxy_log_lines"] = [*GOOD_LOG, line]
    row = check(evaluate(observed), "proxy_log_allowed_paths")
    assert row["passed"] is False


def test_other_accounts_lines_are_ignored_but_no_holmes_line_fails() -> None:
    other = "2026-10-28T12:00:09+00:00 investigator GET /api/v1/metadata 403 3 0 0.0"
    observed = good()
    observed["proxy_log_lines"] = [*GOOD_LOG, other]
    assert check(evaluate(observed), "proxy_log_allowed_paths")["passed"] is True
    observed["proxy_log_lines"] = [other]
    assert check(evaluate(observed), "proxy_log_allowed_paths")["passed"] is False
    observed["proxy_log_lines"] = []
    assert check(evaluate(observed), "proxy_log_allowed_paths")["passed"] is False


def test_recorded_run_log_passes_the_path_rule() -> None:
    lines = isolation.RECORDED_PROXY_LOG.read_text().splitlines()
    observed = good()
    observed["proxy_log_lines"] = lines
    assert check(evaluate(observed), "proxy_log_allowed_paths")["passed"] is True


# --- probe container shape


def test_probe_container_matches_the_run_shape_except_entrypoint() -> None:
    kw: dict[str, Any] = dict(
        network="f14-holmes-internal",
        egress_proxy="http://f14-egress:3128",
        config_path="/abs/config.yaml",
        output_dir="/abs/out",
        no_proxy="f14-lab-relay",
        name="f14-holmes",
    )
    real = container.run_args("img", **kw, command=["ask", "q"])
    probe = isolation.probe_args("img", **kw, targets={"a": 1})
    image_at = probe.index("img")
    assert probe[image_at - 2 : image_at] == ["--entrypoint", "python"]
    assert probe[: image_at - 2] == real[: real.index("img")]
    assert probe[image_at + 1] == "-c"
    assert json.loads(probe[-1]) == {"a": 1}


def test_run_args_without_entrypoint_is_unchanged() -> None:
    a = container.run_args(
        "img",
        network="n",
        egress_proxy="http://e:3128",
        config_path="/c",
        output_dir="/o",
    )
    assert "--entrypoint" not in a and a[-1] == "img"


def test_probe_script_prints_names_not_values() -> None:
    script = isolation.PROBE_SCRIPT
    assert "sorted(os.environ)" in script
    assert "os.environ[" not in script and "os.environ.get" not in script
    assert "environ.items" not in script and "environ.values" not in script
    compile(script, "<probe>", "exec")


UNREACH = {"ok": False, "error": "OSError", "errno": "ENETUNREACH"}
REFUSED = {"ok": False, "error": "ConnectionRefusedError", "errno": "ECONNREFUSED"}
NO_DNS = {"ok": False, "error": "gaierror", "errno": -3}
TIMEOUT = {"ok": False, "error": "TimeoutError", "errno": None}
BOOM = {"ok": False, "error": "RuntimeError", "errno": None}


def raw_probe(**over: Any) -> dict[str, Any]:
    """The live run's shape: every forbidden target failed definitively and
    every positive control succeeded."""
    raw: dict[str, Any] = {
        "uid": 65534,
        "env_names": ["PATH"],
        "paths": {"/a": False},
        "tcp": {
            "allowed": [
                {"target": "f14-lab-relay:9090", "ok": True},
                {"target": "f14-lab-relay:16686", "ok": True},
                {"target": "f14-egress:3128", "ok": True},
            ],
            "kube_api": [
                {"target": "n:6443", **NO_DNS},
                {"target": "r:6443", **REFUSED},
            ],
            "prometheus_backend": [{"target": "10.0.0.9:9090", **UNREACH}],
            "internet": [{"target": "1.1.1.1:443", **UNREACH}],
            "model_direct": [
                {"target": "api.deepseek.com:443", **NO_DNS},
                {"target": "198.18.0.75:443", **UNREACH},
            ],
        },
        "connect": {
            "internet": [{"target": "example.com:443", "status": 403}],
            "model_via_proxy": [{"target": "api.deepseek.com:443", "status": 200}],
        },
    }
    raw.update(over)
    return raw


GOOD_REACH = {
    "kube_api": False,
    "prometheus_backend": False,
    "internet": False,
    "model_direct": False,
    "model_via_proxy": True,
}
UNREACHABLE_CHECKS = (
    "kube_api_unreachable",
    "prometheus_backend_unreachable",
    "internet_unreachable",
    "model_only_via_proxy",
)


def evaluated(raw: dict[str, Any]) -> dict[str, Any]:
    observed = isolation.observed_from_raw(raw, GOOD_LOG)
    return {c["id"]: c["passed"] for c in evaluate(observed)["checks"]}


def test_definitive_failures_with_good_controls_pass() -> None:
    observed = isolation.observed_from_raw(raw_probe(), GOOD_LOG)
    assert observed["paths_present"] == []
    assert observed["reach"] == GOOD_REACH
    assert evaluate(observed)["passed"] is True


def test_timeouts_everywhere_with_good_controls_do_not_pass() -> None:
    raw = raw_probe()
    for key in ("kube_api", "prometheus_backend", "internet", "model_direct"):
        raw["tcp"][key] = [{"target": "x:1", **TIMEOUT}]
    raw["connect"]["internet"] = [{"target": "example.com:443", "status": None}]
    observed = isolation.observed_from_raw(raw, GOOD_LOG)
    for key in ("kube_api", "prometheus_backend", "internet", "model_direct"):
        assert key not in observed["reach"]
    checks = evaluated(raw)
    assert not any(checks[c] for c in UNREACHABLE_CHECKS)
    assert evaluate(observed)["passed"] is False


@pytest.mark.parametrize(
    "key,check_id",
    [
        ("kube_api", "kube_api_unreachable"),
        ("prometheus_backend", "prometheus_backend_unreachable"),
        ("internet", "internet_unreachable"),
        ("model_direct", "model_only_via_proxy"),
    ],
)
@pytest.mark.parametrize("failure", [TIMEOUT, BOOM, {"ok": False}])
def test_one_indeterminate_attempt_fails_its_check(
    key: str, check_id: str, failure: dict[str, Any]
) -> None:
    raw = raw_probe()
    raw["tcp"][key] = [*raw["tcp"][key], {"target": "y:1", **failure}]
    observed = isolation.observed_from_raw(raw, GOOD_LOG)
    assert key not in observed["reach"]
    assert "y:1" in json.dumps(observed["probe"]["reach_basis"][key])
    assert evaluated(raw)[check_id] is False


@pytest.mark.parametrize("status", [None, 200, 502, 407])
def test_proxy_connect_must_be_refused_with_403(status: int | None) -> None:
    raw = raw_probe()
    raw["connect"]["internet"] = [{"target": "example.com:443", "status": status}]
    observed = isolation.observed_from_raw(raw, GOOD_LOG)
    assert observed["reach"].get("internet") is (True if status == 200 else None)
    assert evaluated(raw)["internet_unreachable"] is False


@pytest.mark.parametrize(
    "broken",
    [
        lambda r: r["tcp"]["allowed"].__setitem__(0, {"target": "relay", **TIMEOUT}),
        lambda r: r["tcp"].__setitem__("allowed", []),
        lambda r: r["connect"]["model_via_proxy"][0].__setitem__("status", 403),
        lambda r: r["connect"].pop("model_via_proxy"),
    ],
)
def test_failed_controls_fail_every_unreachable_check(broken: Any) -> None:
    raw = raw_probe()
    broken(raw)
    observed = isolation.observed_from_raw(raw, GOOD_LOG)
    assert observed["probe"]["controls_passed"] is False
    for key in ("kube_api", "prometheus_backend", "internet", "model_direct"):
        assert key not in observed["reach"]
    checks = evaluated(raw)
    assert not any(checks[c] for c in UNREACHABLE_CHECKS)


def test_reachable_target_is_recorded_reachable() -> None:
    raw = raw_probe()
    raw["tcp"]["model_direct"].append({"target": "1.2.3.4:443", "ok": True})
    observed = isolation.observed_from_raw(raw, GOOD_LOG)
    assert observed["reach"]["model_direct"] is True
    assert evaluated(raw)["model_only_via_proxy"] is False


@pytest.mark.parametrize("raw", [{}, {"uid": 65534}, raw_probe(tcp=None)])
def test_missing_probe_output_passes_no_reach_check(raw: dict[str, Any]) -> None:
    checks = evaluated(raw)
    assert not any(checks[c] for c in UNREACHABLE_CHECKS)


def test_probe_script_records_the_errno_name() -> None:
    assert "errno.errorcode" in isolation.PROBE_SCRIPT


def test_unreadable_path_is_present_unless_the_image_check_shows_it_absent() -> None:
    raw = {
        "paths": {"/root/.kube": None, "/out/.env": None, "/repo": False},
        "mounts": ["/", "/etc/holmes/config.yaml", "/out", "/tmp"],
    }
    # fail closed: an unreadable path counts as present
    assert isolation.observed_from_raw(raw, [])["paths_present"] == [
        "/root/.kube",
        "/out/.env",
    ]
    # the image (root, no network) has no /root/.kube and nothing mounts over
    # it; /out is a host mount, so the image cannot speak for it
    image = {"/root/.kube": False, "/out/.env": False}
    observed = isolation.observed_from_raw(raw, [], image_paths=image)
    assert observed["paths_present"] == ["/out/.env"]
    # the image check itself cannot tell -> still present
    image = {"/root/.kube": None}
    observed = isolation.observed_from_raw(raw, [], image_paths=image)
    assert "/root/.kube" in observed["paths_present"]


def test_probe_script_reports_unknown_not_absent_on_permission_errors() -> None:
    assert "return None" in isolation.PROBE_SCRIPT
    assert "IMAGE_PATHS_SCRIPT" in dir(isolation)
    compile(isolation.IMAGE_PATHS_SCRIPT, "<image>", "exec")


def test_image_check_runs_without_network_or_mounts() -> None:
    a = isolation.image_check_args("img", ["/root/.kube"])
    assert a[a.index("--network") + 1] == "none"
    assert "-v" not in a and "--mount" not in a
    assert a[a.index("--cap-drop") + 1] == "ALL"
    assert "--read-only" in a
    assert json.loads(a[-1]) == ["/root/.kube"]


GOOD_CONTROLS = [
    {"target": "f14-lab-relay:9090", "ok": True},
    {"target": "f14-lab-relay:16686", "ok": True},
    {"target": "f14-egress:3128", "ok": True},
]


def test_expected_controls_are_the_probe_targets() -> None:
    assert isolation.CONTROL_TCP == tuple(c["target"] for c in GOOD_CONTROLS)
    assert isolation.CONTROL_MODEL == "api.deepseek.com:443"


@pytest.mark.parametrize(
    "controls",
    [
        GOOD_CONTROLS[:1],  # truncated
        GOOD_CONTROLS[1:],
        [{"target": "wrong:1", "ok": True}, *GOOD_CONTROLS[1:]],  # substituted
        [*GOOD_CONTROLS, {"target": "f14-lab-relay:9090", **TIMEOUT}],  # dup fails
        [*GOOD_CONTROLS, {"target": "f14-lab-relay:9090", "ok": True}],  # dup ok
        [*GOOD_CONTROLS, {"target": "wrong:1", "ok": True}],  # extra
        [{**c, "ok": "yes"} for c in GOOD_CONTROLS],
    ],
)
def test_tcp_controls_must_be_exactly_the_expected_set(controls: list[Any]) -> None:
    raw = raw_probe()
    raw["tcp"]["allowed"] = controls
    observed = isolation.observed_from_raw(raw, GOOD_LOG)
    assert observed["probe"]["controls_passed"] is False
    checks = evaluated(raw)
    assert not any(checks[c] for c in UNREACHABLE_CHECKS)


@pytest.mark.parametrize(
    "via",
    [
        [{"target": "wrong:443", "status": 200}],
        [{"target": "api.deepseek.com:80", "status": 200}],
        [
            {"target": "api.deepseek.com:443", "status": 200},
            {"target": "api.deepseek.com:443", "status": 403},
        ],
        [
            {"target": "api.deepseek.com:443", "status": 200},
            {"target": "wrong:443", "status": 200},
        ],
        [],
    ],
)
def test_model_control_must_be_exactly_connect_to_the_model_host(
    via: list[Any],
) -> None:
    raw = raw_probe()
    raw["connect"]["model_via_proxy"] = via
    observed = isolation.observed_from_raw(raw, GOOD_LOG)
    assert observed["probe"]["controls_passed"] is False
    assert "model_via_proxy" not in observed["reach"]
    checks = evaluated(raw)
    assert not any(checks[c] for c in UNREACHABLE_CHECKS)


@pytest.mark.parametrize(
    "key,check_id",
    [
        ("kube_api", "kube_api_unreachable"),
        ("prometheus_backend", "prometheus_backend_unreachable"),
        ("internet", "internet_unreachable"),
        ("model_direct", "model_only_via_proxy"),
    ],
)
def test_dns_failure_alone_is_indeterminate(key: str, check_id: str) -> None:
    raw = raw_probe()
    raw["tcp"][key] = [{"target": "name.example:443", **NO_DNS}]
    raw["connect"].pop("internet")
    observed = isolation.observed_from_raw(raw, GOOD_LOG)
    assert key not in observed["reach"]
    assert evaluated(raw)[check_id] is False
    # with an IP-level definitive failure in the same category it counts
    raw["tcp"][key].append({"target": "10.0.0.1:443", **UNREACH})
    assert isolation.observed_from_raw(raw, GOOD_LOG)["reach"][key] is False


def test_proxy_403_is_not_an_ip_level_failure_for_dns() -> None:
    raw = raw_probe()
    raw["tcp"]["internet"] = [{"target": "example.com:443", **NO_DNS}]
    assert "internet" not in isolation.observed_from_raw(raw, GOOD_LOG)["reach"]
