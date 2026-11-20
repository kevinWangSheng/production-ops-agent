"""#667 PR-A contract, groups 8-10 on the summary side: categories, the frozen
trial block, disclosures by cluster path (S-U2), and scores reusing #666
``scoring.score`` over the Lite 17 (R8-5, S-7).
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json

import pytest

from scripts.holmes_evals import scoring
from tests.sregym_eval_support import LITE_17, PINNED_COMMIT, TEMPLATE_SHA256, mod

ALWAYS = {
    "JUDGE_ZAI_GLM",
    "DIAGNOSIS_STAGE_ONLY",
    "INTERNET_OPEN",
    "NON_CONTAINER_AGENT",
    "QUESTION_TEMPLATE_OURS",
    "OBSERVABILITY_DIRECT_UNFILTERED",
    "NO_SECRET_READ",
    "SELF_RUN_NOT_LEADERBOARD",
}
BY_PATH = {
    "filter_proxy_readonly": "VISIBILITY_SAME_FILTER_PROXY",
    "direct_readonly": "VISIBILITY_WIDER_THAN_OTHER_AGENTS",
}
BLOCK_KEYS = {
    "schema",
    "track",
    "upstream_commit",
    "round",
    "problem_id",
    "problem_seq",
    "trial",
    "question_template_sha256",
    "question_sha256",
    "idempotency_key",
    "cluster_path",
    "submission",
    "conversion_reason",
    "result",
    "category",
    "passed",
    "disclosures",
}
CANARY = "zzcanaryanswer"
LEAK_CANARY = "zz-synthetic-judge-cred-0000"


def _outcome(**over):
    r = mod("results")
    base = dict(
        success=True,
        accuracy=0.9,
        composite_score=0.8,
        dimensions={"D1": 1.0},
        unscored_reason=None,
        multi_cause=False,
        timed_out=False,
        deploy_failed=False,
        deploy_attempts=1,
        run_status="complete",
        incomplete_reason=None,
        ttl_s=300.0,
        phase_seconds={"deploy": 60.0},
        fault_injected_at="2026-11-19T10:02:07Z",
    )
    base.update(over)
    return r.AttemptOutcome(**base)


def _sub(
    submitted=True, empty=False, status_code=200, error_code=None, text="the diagnosis"
):
    s = mod("summary")
    return s.Submission(
        submitted=submitted,
        empty=empty,
        status_code=status_code,
        error_code=error_code,
        text_sha256=hashlib.sha256(text.encode()).hexdigest() if submitted else None,
        chars=len(text) if submitted else 0,
    )


# --- constants and disclosures -------------------------------------------------


def test_schema_and_track():
    s = mod("summary")
    assert s.SUMMARY_SCHEMA == "sregym-eval-trial-1"
    assert s.TRACK == "SREGym Lite 诊断段（中立第三方现场题，自测）"
    assert set(s.ALWAYS_DISCLOSED) == ALWAYS


@pytest.mark.parametrize("path", sorted(BY_PATH))
def test_disclosures_per_cluster_path(path):
    s = mod("summary")
    got = s.disclosures(path)
    assert set(got) == ALWAYS | {BY_PATH[path]}
    other = next(v for k, v in BY_PATH.items() if k != path)
    assert other not in got


# --- category_of -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome_kw", "sub_kw", "want"),
    [
        (None, {}, ("env_broken", False)),
        (None, {"submitted": False, "status_code": None}, ("env_broken", False)),
        ({"deploy_failed": True, "success": None}, {}, ("env_broken", False)),
        ({"deploy_failed": True, "success": True}, {}, ("env_broken", False)),
        ({}, {"submitted": False, "status_code": None}, ("no_report", False)),
        ({"success": False}, {"empty": True, "text": ""}, ("no_report", False)),
        ({"success": True}, {"empty": True, "text": ""}, ("no_report", False)),
        (
            {"timed_out": True, "success": False},
            {"submitted": False, "status_code": None},
            ("no_report", False),
        ),
        ({"success": None}, {}, ("judge_exhausted", False)),
        ({"success": True}, {}, ("ok", True)),
        ({"success": False}, {}, ("ok", False)),
    ],
)
def test_category_of(outcome_kw, sub_kw, want):
    s = mod("summary")
    outcome = None if outcome_kw is None else _outcome(**outcome_kw)
    assert tuple(s.category_of(outcome, _sub(**sub_kw))) == want


# --- trial_block -------------------------------------------------------------------


def _block(**over):
    s = mod("summary")
    q = mod("question")
    kw = dict(
        round="r1",
        problem_id=LITE_17[4],
        trial=2,
        question=f"Synthetic question text {CANARY}",
        cluster_path="filter_proxy_readonly",
        submission=_sub(text=f"diagnosis {CANARY}"),
        conversion_reason=None,
        outcome=_outcome(),
    )
    kw.update(over)
    block = s.trial_block(**kw)
    return block, kw, q


def test_trial_block_keys_and_values():
    s = mod("summary")
    up = mod("upstream")
    block, kw, q = _block()
    assert set(block) == BLOCK_KEYS
    assert block["schema"] == "sregym-eval-trial-1"
    assert block["track"] == s.TRACK
    assert block["upstream_commit"] == PINNED_COMMIT
    assert block["round"] == "r1"
    assert block["problem_id"] == LITE_17[4]
    assert block["problem_seq"] == 5 == up.problem_seq(LITE_17[4])
    assert block["trial"] == 2
    assert block["question_template_sha256"] == TEMPLATE_SHA256
    # ASSUMPTION: hex sha256 of the UTF-8 question.
    assert (
        block["question_sha256"]
        == hashlib.sha256(kw["question"].encode("utf-8")).hexdigest()
    )
    assert (
        block["idempotency_key"] == q.idempotency_key("r1", 5, 2) == "sregym-r1-p05-t2"
    )
    assert block["cluster_path"] == "filter_proxy_readonly"
    assert block["category"] == "ok"
    assert block["passed"] is True
    assert set(block["disclosures"]) == ALWAYS | {"VISIBILITY_SAME_FILTER_PROXY"}
    assert block["conversion_reason"] is None


def test_trial_block_is_json_and_carries_submission_and_result_fields():
    block, kw, _ = _block()
    round_trip = json.loads(json.dumps(block))
    assert round_trip["submission"] == {
        "submitted": True,
        "empty": False,
        "status_code": 200,
        "error_code": None,
        "text_sha256": kw["submission"].text_sha256,
        "chars": kw["submission"].chars,
    }
    assert set(round_trip["result"]) == {
        f.name for f in dataclasses.fields(mod("results").AttemptOutcome)
    }
    assert round_trip["result"]["dimensions"] == {"D1": 1.0}
    assert round_trip["result"]["fault_injected_at"] == "2026-11-19T10:02:07Z"


def test_trial_block_without_text_of_question_or_diagnosis():
    block, _, _ = _block()
    blob = json.dumps(block, ensure_ascii=False)
    assert CANARY not in blob


def test_trial_block_no_result_is_env_broken():
    block, _, _ = _block(outcome=None, cluster_path="direct_readonly")
    assert block["result"] is None
    assert block["category"] == "env_broken"
    assert block["passed"] is False
    assert "VISIBILITY_WIDER_THAN_OTHER_AGENTS" in block["disclosures"]
    assert "VISIBILITY_SAME_FILTER_PROXY" not in block["disclosures"]


def test_no_report_block_records_conversion_reason_and_empty_submission():
    sub = _sub(empty=True, text="")
    block, _, _ = _block(
        submission=sub,
        conversion_reason="REPORT_REJECTED_BY_PRODUCT",
        outcome=_outcome(success=False),
    )
    assert block["category"] == "no_report"
    assert block["passed"] is False
    assert block["conversion_reason"] == "REPORT_REJECTED_BY_PRODUCT"
    assert block["submission"]["empty"] is True
    assert block["submission"]["chars"] == 0


def test_timeout_without_submission_is_no_report_and_flagged():
    block, _, _ = _block(
        submission=_sub(submitted=False, status_code=None, error_code="STAGE_PASSED"),
        outcome=_outcome(success=False, timed_out=True),
    )
    assert block["category"] == "no_report"
    assert block["result"]["timed_out"] is True


def test_pipeline_from_csv_keeps_answers_and_secrets_out(tmp_path, monkeypatch):
    """Whitelisted read -> block: canaries in non-whitelisted columns never surface."""
    r = mod("results")
    monkeypatch.setenv("JUDGE_API_KEY", LEAK_CANARY)
    pid = LITE_17[0]
    p = tmp_path / "t" / "opspilot" / pid / f"{pid}_opspilot_results.csv"
    p.parent.mkdir(parents=True)
    row = {
        "problem_id": pid,
        "attempt": "1",
        "Diagnosis.success": "False",
        "Diagnosis.accuracy": "0.3",
        "Diagnosis.composite_score": "0.4",
        "Diagnosis.dimensions": repr([{"id": "D1", "name": CANARY, "score": 0.0}]),
        "Diagnosis.unscored_reason": "",
        "Diagnosis.oracles": repr([{"reason": CANARY}]),
        "Diagnosis.reasoning": CANARY,
        "Diagnosis.root_cause": CANARY,
        "Diagnosis.judge_api_key": LEAK_CANARY,
        "submission": CANARY,
        "timed_out": "False",
        "deploy_failed": "False",
        "deploy_attempts": "1",
        "run_status": "complete",
        "incomplete_reason": "",
        "TTL": "100",
    }
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sorted(row))
        w.writeheader()
        w.writerow(row)
    outcome = r.read_attempt(p)
    block, _, _ = _block(problem_id=pid, outcome=outcome)
    blob = json.dumps(block, ensure_ascii=False)
    assert CANARY not in blob
    assert LEAK_CANARY not in blob
    assert block["result"]["multi_cause"] is True
    assert block["category"] == "ok" and block["passed"] is False


# --- trial_record and scores ------------------------------------------------------


def test_trial_record_maps_block_to_scoring_trial():
    s = mod("summary")
    block, _, _ = _block()
    t = s.trial_record(block)
    assert isinstance(t, scoring.Trial)
    assert (t.qid, t.index, t.passed, t.category) == (LITE_17[4], 2, True, "ok")
    assert t.time_box_limited is False


def _trials():
    s = mod("summary")
    outs = []
    for i, pid in enumerate(LITE_17):
        for trial in (1, 2, 3):
            if i == 0:
                outcome, sub = _outcome(success=True), _sub()
            elif i == 1:  # swings 1/3, with a rerun
                outcome, sub = _outcome(success=(trial == 1)), _sub()
            elif i == 2:
                outcome, sub = None, _sub()
            elif i == 3:
                outcome, sub = _outcome(success=None), _sub()
            elif i == 4:
                outcome, sub = (
                    _outcome(timed_out=True),
                    _sub(submitted=False, status_code=None),
                )
            elif i == 5 and trial == 3:
                continue  # missing trial
            else:
                outcome, sub = _outcome(success=False), _sub()
            block, _, _ = _block(
                problem_id=pid, trial=trial, outcome=outcome, submission=sub
            )
            outs.append(s.trial_record(block))
    block, _, _ = _block(problem_id=LITE_17[1], trial=4, outcome=_outcome(success=True))
    outs.append(s.trial_record(block))
    return outs


def test_scores_match_holmes_scoring_minus_two_groups():
    s = mod("summary")
    trials = _trials()
    got = s.scores(trials)
    want = scoring.score(trials, LITE_17)
    want.pop("multi_cluster")
    want.pop("time_box_limited")
    want["track"] = s.TRACK
    assert got == want


def test_scores_main_denominator_and_buckets():
    s = mod("summary")
    got = s.scores(_trials())
    assert "multi_cluster" not in got and "time_box_limited" not in got
    assert got["track"] == s.TRACK
    assert got["main"]["denominator"] == 51
    assert got["main"]["passes"] == 3 + 1
    assert got["swing_questions"] == [LITE_17[1]]
    assert got["rerun"]["in_main_score"] is False and got["rerun"]["passes"] == 1
    assert got["missing_trials"] == [f"{LITE_17[5]}#3"]
    assert got["zero_scored"]["env_broken"] == [f"{LITE_17[2]}#{i}" for i in (1, 2, 3)]
    assert got["zero_scored"]["judge_exhausted"] == [
        f"{LITE_17[3]}#{i}" for i in (1, 2, 3)
    ]
    assert got["zero_scored"]["no_report"] == [f"{LITE_17[4]}#{i}" for i in (1, 2, 3)]
    assert got["excluding_env_broken"]["excluded"] == [LITE_17[2]]
    assert got["excluding_env_broken"]["denominator"] == 48


def test_scores_reject_non_lite_question():
    s = mod("summary")
    with pytest.raises(ValueError):
        s.scores(
            [scoring.Trial(qid="zz_not_in_lite", index=1, passed=True, category="ok")]
        )
