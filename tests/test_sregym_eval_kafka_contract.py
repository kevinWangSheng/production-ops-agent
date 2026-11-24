"""#667 contract tests: SREGym Kafka wiring (the Kafka read-only tools in SREGym passes).

Written from docs/tasks/2026-11-19-sregym-667.md "Kafka 接入接口", the C3 Kafka
record (docs/tasks/2026-11-20-tool-expansion-c3.md: ``OPSPILOT_KAFKA_*``
settings, anonymous PLAINTEXT is valid) and r11 C3-U1 = a, without reading the
implementation branch. Reuses the PR-B stand-ins: the entry ``Harness`` (fake
conductor plus recording ``EntryHost``) and the round ``Round`` (recording
driver ``Host``). Fixed clocks; no real service is started.

Answer isolation: Kafka is on for every problem. The product environment, the
tool face and its revision must be the same whatever the problem or app is,
and whether the forward came up or not; neither entry nor driver branches on
the problem ID.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.sregym_eval_support import LITE_17, code_of, mod
from tests.test_sregym_eval_b_entry_contract import (
    ADMIN_KUBECONFIG,
    APP,
    ENGINEER_TOKEN,
    JUDGE_KEY,
    Setup,
    _record,
    _run,
    _setup,
    _submits,
)
from tests.test_sregym_eval_b_round_contract import PID, Round, _round_abort

KAFKA_ADDR = "127.0.0.1:9092"
UNREACHABLE_ADDR = "127.0.0.1:29599"
CREDENTIAL_KEYS = (
    "OPSPILOT_KAFKA_USERNAME",
    "OPSPILOT_KAFKA_PASSWORD",
    "OPSPILOT_KAFKA_SASL_MECHANISM",
    "OPSPILOT_KAFKA_CA_FILE",
)
ASTRONOMY = {
    "app_name": "OpenTelemetry Demo Astronomy Shop",
    "namespace": "astronomy-shop",
    "namespaces": ["astronomy-shop"],
    "descriptions": "An online shop for astronomy equipment built from microservices.",
    "stages": ["diagnosis"],
}


# --- helpers ---------------------------------------------------------------------------------


@pytest.fixture
def entry(tmp_path: Path):  # noqa: ANN201
    made: list[Setup] = []

    def _make(name: str = "a", **kw: Any) -> Setup:
        d = tmp_path / name
        d.mkdir()
        s = _setup(d, **kw)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.harness.server.shutdown()
        s.harness.server.server_close()


def _go(s: Setup) -> int:
    s.harness.fake.advance("diagnosis")
    return _run(s)


def _forward_of(raw: str) -> Mapping[str, Any]:
    args, kwargs = json.loads(raw)
    if isinstance(kwargs.get("forward"), Mapping):
        return kwargs["forward"]
    for a in args:
        if isinstance(a, Mapping) and "service" in a:
            return a
    raise AssertionError(f"no forward mapping in {raw}")


def _kafka_forwards(s: Setup) -> list[tuple[Mapping[str, Any], str]]:
    out = []
    for raw in s.harness.forwards_raw:
        fw = _forward_of(raw)
        if str(fw["service"]).removeprefix("svc/") == "kafka":
            out.append((fw, raw))
    return out


def _kafka_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k.startswith("OPSPILOT_KAFKA_")}


def _versions(env: Mapping[str, str]) -> Any:
    profiles = __import__("opspilot.tools.profiles", fromlist=["select_profile"])
    return profiles.select_profile(dict(env)).versions()


def _web_worker(s: Setup) -> tuple[dict[str, str], dict[str, str]]:
    h = s.harness
    assert len(h.web_envs) == 1 and len(h.worker_envs) == 1
    return h.web_envs[0], h.worker_envs[0]


# --- ports -------------------------------------------------------------------------------------


def test_kafka_port_is_9092_and_must_be_free_every_pass() -> None:
    ports = mod("ports").Ports()
    assert ports.kafka == 9092
    per_trial = ports.per_trial()
    assert per_trial.get("kafka") == 9092


# --- entry: every pass sets the address and tries the forward ---------------------------------------------


def test_every_pass_forwards_svc_kafka_in_the_app_namespace(entry) -> None:  # noqa: ANN001
    s = entry()
    assert _go(s) == 0
    ((fw, raw),) = _kafka_forwards(s)
    assert fw["namespace"] == APP["namespaces"][0]
    assert fw["namespace"] != "observe"
    assert int(fw["local_port"]) == 9092 and int(fw["remote_port"]) == 9092
    assert str(s.engineer_kubeconfig) in raw
    assert ADMIN_KUBECONFIG not in raw
    others = [_forward_of(r) for r in s.harness.forwards_raw if r != raw]
    assert len(others) == 3
    assert all(f["namespace"] == "observe" for f in others)
    assert _record(s)["observability"]["kafka"] == "forwarded"


def test_forward_namespace_is_the_first_of_many(entry) -> None:  # noqa: ANN001
    app = dict(ASTRONOMY, namespaces=["astronomy-shop", "astronomy-shop-db"])
    s = entry(app=app)
    assert _go(s) == 0
    ((fw, _raw),) = _kafka_forwards(s)
    assert fw["namespace"] == "astronomy-shop"
    _web, worker = _web_worker(s)
    assert worker["OPSPILOT_K8S_NAMESPACE"] == fw["namespace"]


def test_web_and_worker_both_get_the_anonymous_plaintext_address(entry) -> None:  # noqa: ANN001
    s = entry()
    assert _go(s) == 0
    web, worker = _web_worker(s)
    for role, env in (("web", web), ("worker", worker)):
        assert env["OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"] == KAFKA_ADDR, role
        assert env["OPSPILOT_KAFKA_SECURITY_PROTOCOL"] == "PLAINTEXT", role
        for key in CREDENTIAL_KEYS:
            assert key not in env, (role, key)
        assert set(_kafka_env(env)) == {
            "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS",
            "OPSPILOT_KAFKA_SECURITY_PROTOCOL",
        }, role
    assert _kafka_env(web) == _kafka_env(worker)


def test_kafka_settings_are_a_valid_anonymous_product_config(entry) -> None:  # noqa: ANN001
    s = entry()
    assert _go(s) == 0
    kafka_admin = __import__("opspilot.tools.kafka_admin", fromlist=["KafkaConfig"])
    for env in _web_worker(s):
        assert env["OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"] == KAFKA_ADDR
        kafka_admin.KafkaConfig.from_env(env)  # no KAFKA_CONFIG_* / KAFKA_NOT_LAB


def test_forward_failure_points_kafka_at_unreachable_only(entry) -> None:  # noqa: ANN001
    ok = entry("ok")
    assert _go(ok) == 0
    bad = entry("bad")
    bad.harness.forward_fail = {"kafka"}
    assert _go(bad) == 0
    assert _record(bad)["observability"]["kafka"] == "missing"
    assert len(_submits(bad)) == 1
    web, worker = _web_worker(bad)
    ok_web, ok_worker = _web_worker(ok)
    for env, ok_env in ((web, ok_web), (worker, ok_worker)):
        assert env["OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"] == UNREACHABLE_ADDR
        assert env["OPSPILOT_KAFKA_SECURITY_PROTOCOL"] == "PLAINTEXT"
        # everything else in the product environment is untouched
        rest = {k: v for k, v in env.items() if k != "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"}
        ok_rest = {
            k: v for k, v in ok_env.items() if k != "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"
        }
        assert set(rest) == set(ok_rest)
        for k in rest:
            if k.startswith(
                ("OPSPILOT_PROMETHEUS", "OPSPILOT_JAEGER", "OPSPILOT_LOKI")
            ):
                assert rest[k] == ok_rest[k], k
    other = _record(bad)["observability"]
    for name in ("prometheus", "jaeger", "loki"):
        assert other[name] == _record(ok)["observability"][name], name
        assert other[name] != "missing", name


def test_forward_failure_keeps_the_tool_face_and_revision(entry) -> None:  # noqa: ANN001
    ok = entry("ok")
    assert _go(ok) == 0
    bad = entry("bad")
    bad.harness.forward_fail = {"kafka"}
    assert _go(bad) == 0
    for i in (0, 1):
        assert (
            _web_worker(bad)[i]["OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"] == UNREACHABLE_ADDR
        )
        assert _versions(_web_worker(bad)[i]) == _versions(_web_worker(ok)[i])


def test_kafka_tools_are_on_and_web_and_worker_agree(entry) -> None:  # noqa: ANN001
    s = entry()
    assert _go(s) == 0
    web, worker = _web_worker(s)
    assert _versions(web) == _versions(worker)
    without = {k: v for k, v in worker.items() if not k.startswith("OPSPILOT_KAFKA_")}
    assert _versions(without) != _versions(worker)


# --- answer isolation: no branching on the problem ---------------------------------------------


def test_product_env_and_revision_do_not_depend_on_the_problem(entry) -> None:  # noqa: ANN001
    boundary = mod("boundary")
    runs = []
    for i, (pid, app) in enumerate(
        ((LITE_17[2], APP), (LITE_17[6], ASTRONOMY), (LITE_17[0], APP))
    ):
        s = entry(f"p{i}", app=app, problem_digest=boundary.problem_id_digest(pid))
        assert _go(s) == 0
        runs.append(s)
    kafka_envs = [tuple(_kafka_env(e) for e in _web_worker(s)) for s in runs]
    assert all(k == kafka_envs[0] for k in kafka_envs)
    versions = [tuple(_versions(e) for e in _web_worker(s)) for s in runs]
    assert all(v == versions[0] for v in versions)
    # same app, different problem: the product gets the same set of settings
    assert [set(e) for e in _web_worker(runs[0])] == [
        set(e) for e in _web_worker(runs[2])
    ]
    for s in runs:
        assert len(_kafka_forwards(s)) == 1


# --- check_product_env still refuses engineer credentials ------------------------------------------


def _product_env(role: str, **extra: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "OPSPILOT_TOOL_PROFILE": "otel-demo-ext",
        "OPSPILOT_LOKI_URL": "http://127.0.0.1:29532",
        "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": KAFKA_ADDR,
        "OPSPILOT_KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
    }
    if role == "worker":
        env["OPSPILOT_K8S_TOKEN"] = "readonly-sa-token"
    env.update(extra)
    return env


def _check_env(env: dict[str, str], role: str) -> None:
    mod("boundary").check_product_env(
        env,
        role=role,
        forbidden_values=(ENGINEER_TOKEN, JUDGE_KEY, "/home/eng/engineer.kubeconfig"),
        backend_addresses=("127.0.0.1:29531",),
    )


@pytest.mark.parametrize("role", ["web", "worker"])
def test_kafka_settings_pass_the_product_env_check(role: str) -> None:
    _check_env(_product_env(role), role)


@pytest.mark.parametrize("role", ["web", "worker"])
@pytest.mark.parametrize("value", [ENGINEER_TOKEN, JUDGE_KEY])
def test_engineer_credentials_in_kafka_settings_are_still_refused(
    role: str, value: str
) -> None:
    env = _product_env(role, OPSPILOT_KAFKA_PASSWORD=value)
    with pytest.raises(mod("boundary").BoundaryError) as info:
        _check_env(env, role)
    assert code_of(info.value) == f"ENV_ISOLATION:{role}:OPSPILOT_KAFKA_PASSWORD"


@pytest.mark.parametrize("role", ["web", "worker"])
@pytest.mark.parametrize("key", ["KUBECONFIG", "JUDGE_API_KEY", "ZAI_API_KEY"])
def test_forbidden_keys_beside_kafka_settings_are_still_refused(
    role: str, key: str
) -> None:
    env = _product_env(role, **{key: "x"})
    with pytest.raises(mod("boundary").BoundaryError) as info:
        _check_env(env, role)
    assert code_of(info.value) == f"ENV_ISOLATION:{role}:{key}"


# --- kafka_hosts_missing: hosts file syntax ---------------------------------------------------------


def _missing(text: str, names: tuple[str, ...] = ("kafka",)) -> list[str]:
    return list(mod("driver").kafka_hosts_missing(text, names))


@pytest.mark.parametrize(
    "text",
    [
        "127.0.0.1 kafka\n",
        "127.0.0.1 kafka",  # no trailing newline
        "127.0.0.1\tkafka\n",
        "   127.0.0.1    kafka   \n",
        "127.0.0.1 localhost kafka broker\n",
        "127.0.0.1 localhost\n127.0.0.1 kafka\n",
        "127.0.0.1 KAFKA\n",
        "127.0.0.1 Kafka # the SREGym broker\n",
        "127.0.0.1 kafka\r\n",
        "# local names\n\n127.0.0.1 localhost\n::1 localhost\n127.0.0.1 kafka\n",
        "10.0.0.5 kafka\n127.0.0.1 kafka\n",  # one loopback mapping is enough
    ],
)
def test_hosts_entry_present(text: str) -> None:
    assert _missing(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n",
        "# 127.0.0.1 kafka\n",
        "   # 127.0.0.1 kafka\n",
        "127.0.0.1 localhost # kafka\n",
        "127.0.0.1 localhost #kafka\n",
        "10.0.0.5 kafka\n",
        "127.0.0.2 kafka\n",
        "::1 kafka\n",
        "fe80::1%lo0 kafka\n",
        "0.0.0.0 kafka\n",
        "kafka 127.0.0.1\n",  # address must be the first column
        "kafka\n",
        "127.0.0.1 kafka-0\n",
        "127.0.0.1 my-kafka\n",
        "127.0.0.1 kafka.local\n",
        "127.0.0.1 localhost\n",
    ],
)
def test_hosts_entry_missing(text: str) -> None:
    assert _missing(text) == ["kafka"]


def test_names_are_case_insensitive_both_ways() -> None:
    assert _missing("127.0.0.1 kafka\n", ("KAFKA",)) == []
    assert _missing("127.0.0.1 KaFkA\n", ("Kafka",)) == []


def test_missing_names_keep_the_given_order() -> None:
    names = ("zeta", "kafka", "alpha")
    assert _missing("", names) == ["zeta", "kafka", "alpha"]
    assert _missing("127.0.0.1 kafka\n", names) == ["zeta", "alpha"]
    assert _missing("127.0.0.1 alpha zeta kafka\n", names) == []
    assert _missing("127.0.0.1 alpha\n10.1.1.1 zeta\n", names) == ["zeta", "kafka"]


def test_missing_returns_a_list() -> None:
    assert isinstance(mod("driver").kafka_hosts_missing("", ("kafka",)), list)


def test_config_defaults_are_etc_hosts_and_kafka(tmp_path: Path) -> None:
    cfg = mod("driver").Config(
        round="r1", upstream=tmp_path / "up", kubeconfig=tmp_path / "kc"
    )
    assert Path(cfg.hosts_file) == Path("/etc/hosts")
    assert tuple(cfg.kafka_hosts) == ("kafka",)


# --- prepare_round / prepare: read-only hosts check ---------------------------------------------


def _stamp(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), os.stat(path).st_mtime_ns


def _round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosts: str | None, **cfg: Any
) -> Round:
    r = Round(tmp_path, monkeypatch, **cfg)
    if hosts is None:
        r.hosts_file.unlink()
    else:
        r.hosts_file.write_text(hosts)
        os.utime(
            r.hosts_file, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000)
        )
    return r


def test_prepare_round_reads_but_never_writes_the_hosts_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 localhost kafka\n")
    before = _stamp(r.hosts_file)
    r.prepare()
    r.trial(1)
    assert _stamp(r.hosts_file) == before
    assert "gateway" in r.fake.events


@pytest.mark.parametrize(
    "hosts", ["127.0.0.1 localhost\n", "10.0.0.5 kafka\n", "# 127.0.0.1 kafka\n"]
)
def test_prepare_round_missing_entry_aborts_before_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosts: str
) -> None:
    r = _round(tmp_path, monkeypatch, hosts)
    before = _stamp(r.hosts_file)
    with pytest.raises(_round_abort()) as info:
        r.prepare()
    assert code_of(info.value) == "KAFKA_HOSTS_ENTRY_MISSING"
    assert getattr(info.value, "detail", None) == "kafka"
    assert "gateway" not in r.fake.events
    assert r.fake.main_calls == []
    assert _stamp(r.hosts_file) == before


def test_prepare_round_unreadable_hosts_file_counts_as_all_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, None, kafka_hosts=("kafka", "broker-0"))
    with pytest.raises(_round_abort()) as info:
        r.prepare()
    assert code_of(info.value) == "KAFKA_HOSTS_ENTRY_MISSING"
    assert getattr(info.value, "detail", None) == "kafka,broker-0"
    assert "gateway" not in r.fake.events
    assert not r.hosts_file.exists()  # not created either


def test_prepare_round_lists_only_the_missing_names_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(
        tmp_path,
        monkeypatch,
        "127.0.0.1 broker-1\n",
        kafka_hosts=("broker-2", "broker-1", "broker-0"),
    )
    with pytest.raises(_round_abort()) as info:
        r.prepare()
    assert getattr(info.value, "detail", None) == "broker-2,broker-0"


def _applies(r: Round) -> list[list[str]]:
    return [argv for argv, _env in r.fake.kubectl_calls if "apply" in argv]


def test_prepare_applies_the_identity_when_the_entry_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 kafka\n")
    before = _stamp(r.hosts_file)
    mod("driver").prepare(r.cfg, r.host)
    assert len(_applies(r)) == 1
    assert _stamp(r.hosts_file) == before


@pytest.mark.parametrize("hosts", ["127.0.0.1 localhost\n", None])
def test_prepare_missing_entry_aborts_before_apply_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosts: str | None
) -> None:
    r = _round(tmp_path, monkeypatch, hosts)
    before = _stamp(r.hosts_file) if hosts is not None else None
    with pytest.raises(_round_abort()) as info:
        mod("driver").prepare(r.cfg, r.host)
    assert code_of(info.value) == "KAFKA_HOSTS_ENTRY_MISSING"
    assert getattr(info.value, "detail", None) == "kafka"
    assert _applies(r) == []
    if before is not None:
        assert _stamp(r.hosts_file) == before
    else:
        assert not r.hosts_file.exists()


def test_busy_kafka_port_aborts_before_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 kafka\n")
    r.fake.busy_ports = {9092}
    r.prepare()
    with pytest.raises(_round_abort()) as info:
        r.trial(1)
    assert "PORT_BUSY" in f"{code_of(info.value)} {getattr(info.value, 'detail', '')}"
    assert r.fake.main_calls == []
    assert r.summaries() == []


# --- summary: data plane and disclosure ------------------------------------------------------------


def _summary_of(r: Round, seq: int, trial: int = 1) -> dict[str, Any]:
    hits = [
        p
        for p in r.summaries()
        if p.parts[-3:] == (f"p{seq:02d}", f"trial-{trial}", "summary.json")
    ]
    assert len(hits) == 1, r.summaries()
    return json.loads(hits[0].read_text())["sregym_eval"]


def test_summary_records_the_kafka_data_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 kafka\n")
    r.prepare()
    r.trial(1)
    plane = r.summary(1)["sregym_eval"]["data_plane"]
    assert plane["kafka_bootstrap"] == KAFKA_ADDR
    assert plane["kafka_security_protocol"] == "PLAINTEXT"
    assert plane["kafka_auth"] == "none"
    assert list(plane["kafka_advertised_hosts"]) == ["kafka"]
    # the Loki gateway entries stay
    assert "29532" in json.dumps(plane)


def test_summary_advertised_hosts_follow_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(
        tmp_path,
        monkeypatch,
        "127.0.0.1 kafka broker-0\n",
        kafka_hosts=("kafka", "broker-0"),
    )
    r.prepare()
    r.trial(1)
    plane = r.summary(1)["sregym_eval"]["data_plane"]
    assert list(plane["kafka_advertised_hosts"]) == ["kafka", "broker-0"]


@pytest.mark.parametrize("cluster_path", ["filter_proxy_readonly", "direct_readonly"])
def test_every_pass_discloses_kafka_without_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cluster_path: str
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 kafka\n", cluster_path=cluster_path)
    r.prepare()
    r.trial(1)
    r.trial(2)
    for trial in (1, 2):
        s = r.summary(trial)["sregym_eval"]
        assert "KAFKA_NO_AUTH_TOOL_LAYER_ONLY" in s["disclosures"]
        assert s["disclosures"].count("KAFKA_NO_AUTH_TOOL_LAYER_ONLY") == 1


def test_always_disclosed_set_is_unchanged() -> None:
    s = mod("summary")
    assert "KAFKA_NO_AUTH_TOOL_LAYER_ONLY" not in set(s.ALWAYS_DISCLOSED)


def test_summary_keeps_the_entry_kafka_forward_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 kafka\n")
    original = r.fake.run_main

    def run_main(argv, cwd, env, log_path, timeout):  # noqa: ANN001, ANN202
        code = original(argv, cwd, env, log_path, timeout)
        job = json.loads(Path(env["SREGYM_EVAL_JOB"]).read_text())
        rec_path = Path(job["work_dir"]) / "entry-record.json"
        rec = json.loads(rec_path.read_text())
        rec["observability"]["kafka"] = "missing"
        rec_path.write_text(json.dumps(rec))
        return code

    r.fake.run_main = run_main  # type: ignore[method-assign]
    r.host = r.fake.as_host()
    r.prepare()
    r.trial(1)
    s = r.summary(1)["sregym_eval"]
    assert s["entry"]["observability"]["kafka"] == "missing"
    assert "KAFKA_NO_AUTH_TOOL_LAYER_ONLY" in s["disclosures"]


def test_driver_wiring_does_not_depend_on_the_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 kafka\n")
    r.prepare()
    others = (LITE_17[6], LITE_17[0])  # seq 7 and 1
    assert PID not in others
    for pid in (PID, *others):
        mod("driver").run_trial(r.cfg, r.host, pid, 1)
    planes = [_summary_of(r, seq)["data_plane"] for seq in (3, 7, 1)]
    assert all(p == planes[0] for p in planes)
    disclosures = [set(_summary_of(r, seq)["disclosures"]) for seq in (3, 7, 1)]
    assert all(d == disclosures[0] for d in disclosures)
    jobs = [json.loads(c["job_text"]) for c in r.fake.main_calls]
    assert len(jobs) == 3
    assert all(j["ports"] == jobs[0]["ports"] for j in jobs)
