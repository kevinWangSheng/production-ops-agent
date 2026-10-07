"""Experiment evidence after ADR-0006 (``scripts/lab_evidence.py``,
``scripts/lab_review_feedback.py``): project per round at longest retention,
frozen summary only in the repository, review verdicts as feedback.

No network: the LangSmith client is a recording fake.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from opspilot.tracing import LAB_PROJECT_PREFIX, check_lab_target
from scripts import lab_evidence, lab_review_feedback


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    """Records every call; serves projects, runs and feedback from memory."""

    _host_url = "https://smith.langchain.com"

    def __init__(self, *, runs=None, tenant="11111111-2222-3333-4444-555555555555"):
        self.calls: list[tuple] = []
        self.projects: dict[str, dict] = {}
        self.runs = list(runs or [])
        self.feedback: dict[str, SimpleNamespace] = {}
        self.tenant = tenant

    def create_project(self, name, *, upsert=False):
        self.calls.append(("create_project", name, upsert))
        project = self.projects.setdefault(
            name, {"id": str(uuid.uuid4()), "name": name, "trace_tier": None}
        )
        return SimpleNamespace(id=project["id"], name=name)

    def request_with_retries(self, method, pathname, *, request_kwargs=None, **_):
        self.calls.append((method, pathname, request_kwargs))
        project_id = pathname.rsplit("/", 1)[-1]
        project = next(p for p in self.projects.values() if p["id"] == project_id)
        if method == "PATCH":
            project["trace_tier"] = request_kwargs["json"]["trace_tier"]
        return FakeResponse(dict(project))

    def list_runs(self, **kwargs):
        self.calls.append(("list_runs", kwargs))
        return iter(self.runs)

    def _get_optional_tenant_id(self):
        return self.tenant

    def read_run(self, run_id):
        self.calls.append(("read_run", run_id))
        for run in self.runs:
            if str(run.id) == str(run_id):
                return run
        raise LookupError(run_id)

    def read_project(self, *, project_id=None, project_name=None):
        self.calls.append(("read_project", project_id, project_name))
        for project in self.projects.values():
            if project["id"] == project_id or project["name"] == project_name:
                return SimpleNamespace(id=project["id"], name=project["name"])
        raise LookupError(project_id or project_name)

    def create_feedback(self, run_id, key, **kwargs):
        self.calls.append(("create_feedback", run_id, key, kwargs))
        item = SimpleNamespace(
            id=uuid.uuid4(), run_id=uuid.UUID(run_id), key=key, **kwargs
        )
        self.feedback[str(item.id)] = item
        return item

    def read_feedback(self, feedback_id):
        self.calls.append(("read_feedback", feedback_id))
        return self.feedback[str(feedback_id)]


# -- project per round --------------------------------------------------------


def test_lab_project_name_carries_the_fail_closed_prefix_and_is_validated():
    assert lab_evidence.lab_project("r12") == f"{LAB_PROJECT_PREFIX}r12"
    for bad in ("", "R12", "round 1", "a/b", "x" * 64):
        with pytest.raises(ValueError):
            lab_evidence.lab_project(bad)


def test_configure_lab_round_satisfies_the_tracing_project_check():
    env = {
        "OPSPILOT_TRACE": "lab",
        "LANGSMITH_API_KEY": "k" * 12,
    }
    assert "PROJECT_NOT_LAB" in check_lab_target(env)
    assert (
        lab_evidence.configure_lab_round(env, "trace-pr2") == "opspilot-lab-trace-pr2"
    )
    assert env["LANGSMITH_PROJECT"] == "opspilot-lab-trace-pr2"
    assert check_lab_target(env) == ()


def test_ensure_longlived_project_creates_then_patches_trace_tier_and_reads_back():
    client = FakeClient()
    record = lab_evidence.ensure_longlived_project(client, "opspilot-lab-r1")
    assert record == {
        "project": "opspilot-lab-r1",
        "project_id": client.projects["opspilot-lab-r1"]["id"],
        "trace_tier": "longlived",
    }
    methods = [c[0] for c in client.calls]
    assert methods == ["create_project", "PATCH", "GET"]
    assert client.calls[0][2] is True  # upsert: a second round reuses the project
    assert client.calls[1][2] == {"json": {"trace_tier": "longlived"}}


# -- trace read-back ----------------------------------------------------------


def test_find_root_run_filters_by_otel_trace_id_and_polls_boundedly():
    client = FakeClient()
    sleeps: list[float] = []
    assert (
        lab_evidence.find_root_run(
            client,
            project_name="opspilot-lab-r1",
            otel_trace_id="abc",
            attempts=3,
            delay_seconds=0.5,
            sleep=sleeps.append,
        )
        is None
    )
    assert sleeps == [0.5, 0.5]  # no sleep after the last attempt
    kwargs = client.calls[0][1]
    assert kwargs["is_root"] is True
    assert kwargs["project_name"] == "opspilot-lab-r1"
    assert kwargs["filter"] == (
        'and(eq(metadata_key, "OTEL_TRACE_ID"), eq(metadata_value, "abc"))'
    )


def test_trace_evidence_records_link_and_retention_or_the_reason_it_could_not():
    root = SimpleNamespace(
        id="00000000-0000-0000-d2aa-402ad330ab4a", name="opspilot.run"
    )
    client = FakeClient(runs=[root])
    project = {
        "project": "opspilot-lab-r1",
        "project_id": "p-1",
        "trace_tier": "longlived",
    }
    found = lab_evidence.trace_evidence(
        {"mode": "lab", "trace_id": "f" * 32, "dropped_spans": 0},
        run_id="run-1",
        project=project,
        client_factory=lambda: client,
    )
    assert found["read_back"] == "found"
    assert found["langsmith_run_id"] == root.id
    assert found["trace_tier"] == "longlived"
    assert found["url"] == (
        "https://smith.langchain.com/o/11111111-2222-3333-4444-555555555555"
        f"/projects/p/p-1/r/{root.id}?poll=true"
    )

    off = lab_evidence.trace_evidence(
        {"mode": "off", "trace_id": None, "dropped_spans": 0},
        run_id="run-1",
        project=None,
        client_factory=lambda: pytest.fail("off mode must not touch LangSmith"),
    )
    assert off["read_back"] == "not_exported" and off["url"] is None

    missing = lab_evidence.trace_evidence(
        {"mode": "lab", "trace_id": "f" * 32, "dropped_spans": 0},
        run_id="run-1",
        project=project,
        client_factory=lambda: FakeClient(runs=[]),
        attempts=1,
        delay_seconds=0,
    )
    assert missing["read_back"] == "root_run_not_found"
    assert missing["url"] is None and missing["langsmith_run_id"] is None


# -- frozen summary -----------------------------------------------------------


def test_freeze_writes_only_summary_json_into_evidence_and_hashes_the_raw_ledger(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(lab_evidence.LEDGER_DIR_ENV, str(tmp_path / "ledgers"))
    evidence_dir = tmp_path / "evidence" / "live-runs" / "run-1"
    ledger = {"attempts_http": [{"usage": {"prompt_tokens": 3}}], "events": ["x"]}
    frozen, path = lab_evidence.freeze(
        ledger,
        experiment="exp",
        run_id="run-1",
        evidence_dir=evidence_dir,
        summary={"verdicts": {"status": "published"}, "report": {"k": "v"}},
    )
    assert path == evidence_dir / "summary.json"
    assert [p.name for p in evidence_dir.iterdir()] == ["summary.json"]
    ledger_path = tmp_path / "ledgers" / "exp" / "run-1" / "ledger.json"
    assert ledger_path.is_file()
    assert (
        frozen["raw_ledger"]["sha256"]
        == hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    )
    assert frozen["raw_ledger"]["in_repository"] is False
    written = json.loads(path.read_text())
    assert written["verdicts"] == {"status": "published"}
    assert written["report"] == {"k": "v"}
    assert written["raw_ledger"]["sha256"] == frozen["raw_ledger"]["sha256"]
    assert "attempts_http" not in written and "events" not in written


def test_default_ledger_dir_is_under_the_ignored_tmp_tree(monkeypatch):
    monkeypatch.delenv(lab_evidence.LEDGER_DIR_ENV, raising=False)
    target = lab_evidence.ledger_dir("exp", "run-1")
    assert target == lab_evidence.ROOT / "tmp" / "lab-ledgers" / "exp" / "run-1"
    assert "docs" not in target.relative_to(lab_evidence.ROOT).parts
    ignored = (lab_evidence.ROOT / ".gitignore").read_text().splitlines()
    assert "tmp/" in ignored


def test_parse_report_keeps_json_or_text():
    assert lab_evidence.parse_report(None) is None
    assert lab_evidence.parse_report('{"a": 1}') == {"a": 1}
    assert lab_evidence.parse_report("not json") == "not json"


# -- review feedback ----------------------------------------------------------


def test_review_feedback_cli_writes_reads_back_and_records_in_summary(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("LANGSMITH_API_KEY", "k" * 12)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    run_id = "00000000-0000-0000-d2aa-402ad330ab4a"
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({"trace": {"langsmith_run_id": run_id}}))
    client = FakeClient()
    _project_with_root(client, "opspilot-lab-r1", run_id)
    code = lab_review_feedback.main(
        [
            "--summary",
            str(summary_path),
            "--key",
            "review_p1",
            "--verdict",
            "pass",
            "--review-url",
            "https://example.test/pr/1",
            "--comment",
            "report contract held",
            "--record",
        ],
        client_factory=lambda: client,
    )
    assert code == 0
    create = next(c for c in client.calls if c[0] == "create_feedback")
    assert create[1:3] == (run_id, "review_p1")
    assert create[3]["score"] == 1 and create[3]["value"] == "pass"
    assert create[3]["comment"].endswith("review record: https://example.test/pr/1")
    assert create[3]["source_info"] == {"review_record": "https://example.test/pr/1"}
    view = json.loads(capsys.readouterr().out)
    assert view["read_back_matches"] is True
    recorded = json.loads(summary_path.read_text())["review_feedback"]
    assert recorded == [
        {k: view[k] for k in ("feedback_id", "key", "score", "value", "comment")}
    ]


def test_review_feedback_cli_rejects_foreign_keys_and_missing_run_id(tmp_path):
    with pytest.raises(SystemExit):
        lab_review_feedback.main(
            [
                "--run-id",
                "x",
                "--key",
                "score",
                "--verdict",
                "pass",
                "--review-url",
                "u",
            ],
            client_factory=lambda: pytest.fail("must not reach LangSmith"),
        )
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({"trace": {"langsmith_run_id": None}}))
    with pytest.raises(SystemExit):
        lab_review_feedback.main(
            [
                "--summary",
                str(summary_path),
                "--key",
                "review_p1",
                "--verdict",
                "fail",
                "--review-url",
                "u",
            ],
            client_factory=lambda: pytest.fail("must not reach LangSmith"),
        )


def test_insufficient_verdict_has_no_score():
    client = FakeClient()
    view = lab_review_feedback.write_and_read_back(
        client,
        run_id="00000000-0000-0000-d2aa-402ad330ab4a",
        key="review_p2",
        verdict="insufficient",
        review_url="u",
        comment=None,
    )
    assert view["score"] is None and view["value"] == "insufficient"
    assert view["comment"] == "review record: u"
    assert view["read_back_matches"] is True


# -- review findings (PR #110) --------------------------------------------------

LAB_ENV = {
    "OPSPILOT_TRACE": "lab",
    "LANGSMITH_API_KEY": "k" * 12,
    "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
}


def test_finding_a1_no_langsmith_call_before_the_lab_check_passes():
    """Astra A1: a disallowed endpoint must stop the project setup before any
    request carries the API key; the tracing check is reused, not copied."""
    env = {**LAB_ENV, "LANGSMITH_ENDPOINT": "https://evil.example/otel"}
    with pytest.raises(SystemExit) as raised:
        lab_evidence.prepare_lab_project(
            env,
            "r1",
            client_factory=lambda: pytest.fail("LangSmith reached before check"),
        )
    assert "LANGSMITH_ENDPOINT_NOT_ALLOWED" in str(raised.value)
    # Off mode: nothing to do, no client.
    assert (
        lab_evidence.prepare_lab_project(
            {"OPSPILOT_TRACE": "off"}, "r1", client_factory=lambda: pytest.fail("x")
        )
        is None
    )
    # Lab mode and a passing check: project named, created and set longlived.
    client = FakeClient()
    env = dict(LAB_ENV)
    record = lab_evidence.prepare_lab_project(env, "r1", client_factory=lambda: client)
    assert env["LANGSMITH_PROJECT"] == "opspilot-lab-r1"
    assert record["trace_tier"] == "longlived"
    with pytest.raises(SystemExit):
        lab_evidence.prepare_lab_project(
            dict(LAB_ENV), None, client_factory=lambda: client
        )


def _project_with_root(client, name, run_id, otel_trace_id="a" * 32):
    project = client.create_project(name, upsert=True)
    root = SimpleNamespace(
        id=run_id,
        name="opspilot.run",
        session_id=project.id,
        trace_id=run_id,
        extra={"metadata": {"OTEL_TRACE_ID": otel_trace_id, "run_id": "biz-run"}},
    )
    client.runs.append(root)
    return project


def test_finding_a2_feedback_cli_refuses_non_lab_targets_without_writing(monkeypatch):
    """Astra A2: endpoint/key are validated with the tracing check, the run is
    read first and must belong to an ``opspilot-lab-*`` project."""
    run_id = "00000000-0000-0000-d2aa-402ad330ab4a"
    base = [
        "--run-id",
        run_id,
        "--key",
        "review_p1",
        "--verdict",
        "pass",
        "--review-url",
        "u",
    ]
    for name in (
        "OPSPILOT_TRACE",
        "LANGSMITH_API_KEY",
        "LANGSMITH_ENDPOINT",
        "LANGSMITH_PROJECT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGSMITH_API_KEY", "k" * 12)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://evil.example")
    with pytest.raises(SystemExit) as raised:
        lab_review_feedback.main(base, client_factory=lambda: pytest.fail("no client"))
    assert "LANGSMITH_ENDPOINT_NOT_ALLOWED" in str(raised.value)

    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    # Run in a non-lab project: read, refused, nothing written.
    client = FakeClient()
    _project_with_root(client, "production-traces", run_id)
    with pytest.raises(SystemExit) as raised:
        lab_review_feedback.main(base, client_factory=lambda: client)
    assert "RUN_NOT_IN_LAB_PROJECT" in str(raised.value)
    assert not any(c[0] == "create_feedback" for c in client.calls)
    # Unknown run: refused before any write.
    with pytest.raises(SystemExit) as raised:
        lab_review_feedback.main(
            [*base[:1], "00000000-0000-0000-0000-000000000001", *base[2:]],
            client_factory=lambda: client,
        )
    assert "RUN_NOT_FOUND" in str(raised.value)
    assert not any(c[0] == "create_feedback" for c in client.calls)
    # Lab project: written and read back.
    lab = FakeClient()
    _project_with_root(lab, "opspilot-lab-r1", run_id)
    assert lab_review_feedback.main(base, client_factory=lambda: lab) == 0
    assert [
        c[0]
        for c in lab.calls
        if c[0] in ("read_run", "read_project", "create_feedback")
    ] == [
        "read_run",
        "read_project",
        "create_feedback",
    ]


def test_finding_b1_report_digest_and_evidence_follow_the_final_conclusion():
    """Codex B1: with a follow-up, the report is the final conclusion; its
    sha256 and evidence ids must come from the same attempt, not the first."""
    from scripts.m1_live_runner import final_loop

    first = {
        "label": "first",
        "outcome": {"loop": {"report_content_sha256": "a", "evidence_ids": ["e1"]}},
    }
    renewed = {
        "label": "after-follow-up",
        "outcome": {"loop": {"report_content_sha256": "b", "evidence_ids": ["e2"]}},
    }
    sweep = {"label": "sweep", "outcome": {"loop": None}}
    assert final_loop([first, renewed, sweep]) == renewed["outcome"]["loop"]
    assert final_loop([first]) == first["outcome"]["loop"]
    assert final_loop([sweep]) is None


def test_finding_b2_root_run_is_selected_by_the_final_otel_trace_id():
    """Codex B2: each attempt is its own root with the same business run id and
    a renewed Run carries another; the recorded final trace id is unique."""
    client = FakeClient()
    other = SimpleNamespace(
        id="00000000-0000-0000-0000-00000000000a", name="opspilot.run"
    )
    final = SimpleNamespace(
        id="00000000-0000-0000-0000-00000000000b", name="opspilot.run"
    )
    client.runs = [final]
    found = lab_evidence.find_root_run(
        client,
        project_name="opspilot-lab-r1",
        otel_trace_id="f" * 32,
        attempts=1,
        delay_seconds=0,
    )
    assert found is final and found is not other
    kwargs = client.calls[-1][1]
    assert kwargs["filter"] == (
        f'and(eq(metadata_key, "OTEL_TRACE_ID"), eq(metadata_value, "{"f" * 32}"))'
    )
    assert kwargs["is_root"] is True


def test_finding_b3_read_back_errors_are_recorded_not_raised():
    """Codex B3: a transient LangSmith failure must not lose the frozen
    evidence; the trace block says so explicitly."""
    project = {
        "project": "opspilot-lab-r1",
        "project_id": "p-1",
        "trace_tier": "longlived",
    }

    def boom():
        raise ConnectionError("transient")

    evidence = lab_evidence.trace_evidence(
        {"mode": "lab", "trace_id": "f" * 32, "dropped_spans": 0},
        run_id="run-1",
        project=project,
        client_factory=boom,
    )
    assert evidence["read_back"] == "error:ConnectionError"
    assert evidence["url"] is None and evidence["project"] == "opspilot-lab-r1"

    class Flaky(FakeClient):
        def list_runs(self, **kwargs):
            raise TimeoutError("slow")

    evidence = lab_evidence.trace_evidence(
        {"mode": "lab", "trace_id": "f" * 32, "dropped_spans": 0},
        run_id="run-1",
        project=project,
        client_factory=Flaky,
        attempts=1,
        delay_seconds=0,
    )
    assert evidence["read_back"] == "error:TimeoutError"


def test_finding_r2_1_client_endpoint_comes_only_from_validated_langsmith_endpoint(
    monkeypatch,
):
    """Astra round 2 #1: ``langsmith.Client()`` also honours LANGCHAIN_ENDPOINT
    and other ambient SDK config; the client must be built from the validated
    ``LANGSMITH_ENDPOINT`` (or the canonical default) and the named key only."""
    for name in ("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "https://attacker.example")
    monkeypatch.setenv("LANGSMITH_API_KEY", "k" * 12)
    client = lab_evidence.langsmith_client()
    assert client.api_url == "https://api.smith.langchain.com"
    # An explicit but disallowed LANGSMITH_ENDPOINT refuses before construction.
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://attacker.example")
    with pytest.raises(SystemExit) as raised:
        lab_evidence.langsmith_client()
    assert "LANGSMITH_ENDPOINT_NOT_ALLOWED" in str(raised.value)
    # A missing key refuses as well; the SDK must not fall back to LANGCHAIN_API_KEY.
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://eu.api.smith.langchain.com")
    monkeypatch.delenv("LANGSMITH_API_KEY")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "k" * 12)
    with pytest.raises(SystemExit) as raised:
        lab_evidence.langsmith_client()
    assert "LANGSMITH_KEY_MISSING" in str(raised.value)
    monkeypatch.setenv("LANGSMITH_API_KEY", "k" * 12)
    assert (
        lab_evidence.langsmith_client().api_url == "https://eu.api.smith.langchain.com"
    )


def test_finding_r2_2_run_id_and_summary_must_agree_before_any_write(
    tmp_path, monkeypatch
):
    """Astra round 2 #2: ``--run-id`` naming run B with ``--summary`` of run A
    is refused (no write), so ``--record`` can never point A's summary at B."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "k" * 12)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    run_a = "00000000-0000-0000-d2aa-402ad330ab4a"
    run_b = "00000000-0000-0000-0000-00000000000b"
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({"trace": {"langsmith_run_id": run_a}}))
    client = FakeClient()
    _project_with_root(client, "opspilot-lab-r1", run_a)
    _project_with_root(client, "opspilot-lab-r1", run_b)
    args = [
        "--summary",
        str(summary_path),
        "--run-id",
        run_b,
        "--key",
        "review_p1",
        "--verdict",
        "pass",
        "--review-url",
        "u",
        "--record",
    ]
    with pytest.raises(SystemExit) as raised:
        lab_review_feedback.main(args, client_factory=lambda: client)
    assert "RUN_ID_MISMATCH" in str(raised.value)
    assert not any(c[0] == "create_feedback" for c in client.calls)
    assert "review_feedback" not in json.loads(summary_path.read_text())
    # Matching ids are accepted.
    args[3] = run_a
    assert lab_review_feedback.main(args, client_factory=lambda: client) == 0
    assert (
        json.loads(summary_path.read_text())["review_feedback"][0]["key"] == "review_p1"
    )
