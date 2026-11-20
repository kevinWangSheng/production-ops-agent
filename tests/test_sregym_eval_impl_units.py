"""Implementer unit tests for #667 PR-A (the contract tests are separate)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.sregym_eval import question, upstream
from scripts.sregym_eval.conductor import AppInfo, ConductorClient, ConductorError
from scripts.sregym_eval.fake_conductor import FakeConductor, serve
from scripts.sregym_eval.results import read_attempt
from scripts.sregym_eval.summary import (
    Submission,
    category_of,
    scores,
    trial_block,
    trial_record,
)

APP = {
    "app_name": "Hotel Reservation",
    "namespace": "hotel-reservation",
    "namespaces": ["hotel-reservation"],
    "descriptions": "A hotel booking microservice demo.",
}


def test_template_hash_matches_pin() -> None:
    assert question.template_sha256() == question.QUESTION_TEMPLATE_SHA256


def test_render_single_and_multi_namespace() -> None:
    single = question.render_question(
        AppInfo("A", "ns1", ("ns1",), "desc", ("diagnosis",))
    )
    assert "Namespace: ns1\n" in single and "desc" in single
    multi = question.render_question(
        AppInfo("A", "ns1", ("ns1", "ns2"), "d", ("diagnosis",))
    )
    assert "Namespaces: ns1, ns2\n(This scenario spans" in multi
    with pytest.raises(question.QuestionError, match="QUESTION_TOO_LONG"):
        question.render_question(AppInfo("A", "n", ("n",), "x" * 9000, ("diagnosis",)))


def test_problem_id_leak_check() -> None:
    pid = upstream.LITE_PROBLEMS[2]
    key = question.idempotency_key("r1", upstream.problem_seq(pid), 1)
    assert key == "sregym-r1-p03-t1"
    question.check_no_problem_id(pid, {"key": key, "app": "Social Network"})
    with pytest.raises(question.AnswerLeak, match="PROBLEM_ID_IN:env"):
        question.check_no_problem_id(pid, {"env": "x" + pid.upper()})


def test_lite_list_parse_and_live_checkout() -> None:
    text = "X = 1\nSREGYM_LITE_PROBLEMS = (\n    \"a\",  # c\n    'b',\n)\n"
    assert upstream.read_lite_problems(text) == ("a", "b")
    root = Path(__file__).resolve().parents[1] / "tmp/upstream/sregym-ac60c64"
    if root.is_dir():  # local only; the checkout is git-ignored
        upstream.verify_upstream(root)


def test_conductor_round_trip() -> None:
    fake = FakeConductor(APP)
    server = serve(fake)
    try:
        client = ConductorClient(f"http://127.0.0.1:{server.server_address[1]}")
        assert client.status().stage == "setup"
        fake.advance("diagnosis")
        assert client.wait_for_diagnosis(timeout_s=1, poll_s=0).stage == "diagnosis"
        app = client.get_app()
        assert app.namespaces == ("hotel-reservation",)
        assert client.submit_diagnosis("") == 200
        assert fake.requests[-1].body == {"stage": "diagnosis", "solution": ""}
        with pytest.raises(ConductorError, match="ALREADY_SUBMITTED"):
            client.submit_diagnosis("again")
        other = ConductorClient(f"http://localhost:{server.server_address[1]}")
        with pytest.raises(ConductorError, match="SUBMIT_REJECTED"):
            other.submit_diagnosis("late")
        fake.advance("done")
        assert client.wait_for_done(timeout_s=1, poll_s=0).stage == "done"
    finally:
        server.shutdown()
    with pytest.raises(ConductorError, match="CONDUCTOR_NOT_LOOPBACK"):
        ConductorClient("http://10.0.0.1:8000")


def _csv(path: Path, row: dict[str, str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted(row))
        writer.writeheader()
        writer.writerow(row)


def test_results_whitelist(tmp_path: Path) -> None:
    canary = "CANARY-ROOT-CAUSE"
    path = tmp_path / "p_opspilot_results.csv"
    _csv(
        path,
        {
            "attempt": "1",
            "problem_id": "p",
            "Diagnosis.success": "True",
            "Diagnosis.accuracy": "81.5",
            "Diagnosis.reasoning": canary,
            "Diagnosis.checklist": f"[{{'evidence': '{canary}'}}]",
            "Diagnosis.dimensions": "{'D1': {'name': 'Comp "
            + canary
            + "', 'score': 0.9}}",
            "Diagnosis.unscored_reason": canary,
            "TTL": "42.0",
            "phase.deploy.duration_s": "10.5",
            f"phase.hook:{canary}.duration_s": "1",
        },
    )
    (tmp_path / "phases_attempt1.jsonl").write_text(
        json.dumps(
            {
                "phase": "inject_fault",
                "event": "end",
                "ts": "2026-11-19T01:02:03Z",
                "root_cause": canary,
            }
        )
        + "\n"
    )
    outcome = read_attempt(path)
    assert outcome.success is True and outcome.accuracy == 81.5
    assert outcome.dimensions == {"D1": 0.9}
    assert outcome.unscored_reason == "other"
    assert outcome.phase_seconds == {"deploy": 10.5}
    assert outcome.fault_injected_at == "2026-11-19T01:02:03Z"
    sub = Submission(True, False, 200, None, "h", 3)
    block = trial_block(
        round="r1",
        problem_id=upstream.LITE_PROBLEMS[0],
        trial=1,
        question="q",
        cluster_path="direct_readonly",
        submission=sub,
        conversion_reason=None,
        outcome=outcome,
    )
    assert canary not in json.dumps(block)
    assert block["category"] == "ok" and block["passed"] is True
    assert "VISIBILITY_WIDER_THAN_OTHER_AGENTS" in block["disclosures"]
    result = scores([trial_record(block)])
    assert result["main"]["denominator"] == 51 and result["main"]["passes"] == 1
    assert "multi_cluster" not in result


def test_categories() -> None:
    empty = Submission(True, True, 200, None, None, 0)
    from scripts.sregym_eval.results import AttemptOutcome

    assert category_of(None, empty) == ("env_broken", False)
    assert category_of(AttemptOutcome(success=False), empty) == ("no_report", False)
    done = Submission(True, False, 200, None, "h", 5)
    assert category_of(AttemptOutcome(), done) == ("judge_exhausted", False)
    assert category_of(AttemptOutcome(deploy_failed=True), done) == (
        "env_broken",
        False,
    )


def test_problem_id_variants_and_no_false_positive() -> None:
    pid = "network_policy_block"
    for variant in (
        "network-policy-block",
        "Network Policy Block",
        "NETWORKPOLICYBLOCK",
    ):
        with pytest.raises(question.AnswerLeak, match="PROBLEM_ID_IN:q"):
            question.check_no_problem_id(pid, {"q": f"see {variant} now"})
    other = "duplicate_pvc_mounts_social_network"
    question.check_no_problem_id(
        other, {"q": "Application: social_network, Social Network"}
    )


@pytest.mark.parametrize(
    "url", ["file://localhost/etc/passwd", "https://localhost:8000", "ftp://127.0.0.1"]
)
def test_conductor_http_only(url: str) -> None:
    with pytest.raises(ConductorError, match="CONDUCTOR_NOT_LOOPBACK"):
        ConductorClient(url)


def test_aborted_is_terminal() -> None:
    fake = FakeConductor(APP)
    server = serve(fake)
    calls: list[float] = []
    try:
        client = ConductorClient(f"http://127.0.0.1:{server.server_address[1]}")
        fake.advance("aborted")
        for wait in (client.wait_for_diagnosis, client.wait_for_done):
            with pytest.raises(ConductorError, match="CONDUCTOR_ABORTED"):
                wait(timeout_s=60, poll_s=0, sleep=calls.append)
        assert calls == []  # no polling round before giving up
    finally:
        server.shutdown()
