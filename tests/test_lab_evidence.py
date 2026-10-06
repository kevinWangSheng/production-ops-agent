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


def test_find_root_run_filters_by_run_id_metadata_and_polls_boundedly():
    client = FakeClient()
    sleeps: list[float] = []
    assert (
        lab_evidence.find_root_run(
            client,
            project_name="opspilot-lab-r1",
            run_id="abc",
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
    assert (
        kwargs["filter"] == 'and(eq(metadata_key, "run_id"), eq(metadata_value, "abc"))'
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


def test_review_feedback_cli_writes_reads_back_and_records_in_summary(tmp_path, capsys):
    run_id = "00000000-0000-0000-d2aa-402ad330ab4a"
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({"trace": {"langsmith_run_id": run_id}}))
    client = FakeClient()
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
