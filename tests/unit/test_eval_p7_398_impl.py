"""#398 tuning round: formal-shape `tune`, scorer, round counter, triage; fake transports."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))

from eval_b3_support import false_pass, http  # noqa: E402
from eval_p7_support import DriverHarness, snapshot  # noqa: E402

from opspilot.evaluation import judge  # noqa: E402
from opspilot.evaluation.calibration_flow import final_labels  # noqa: E402
from opspilot.evaluation.codec import decode, parse  # noqa: E402
from opspilot.evaluation.judge import JudgeError  # noqa: E402
from opspilot.evaluation.tuning import (  # noqa: E402
    TuningScore,
    round_outcome,
    schedule,
    score_tuning,
)
from scripts import eval_p7  # noqa: E402

PROMPT_HASH = hashlib.sha256(judge.PROMPT.encode("utf-8")).hexdigest()


@pytest.fixture
def harness(tmp_path, monkeypatch, capsys):
    return DriverHarness(tmp_path, monkeypatch, capsys, eval_p7).init()


@pytest.fixture
def exposed(harness):
    """Frozen pack, debug precheck and one formal run (validation now exposed)."""
    h = harness.ready()
    h.formal()
    h.calls.clear()
    return h


def tune(h, session, round_number, *extra, code=0, reason=None, costs=True):
    if costs:
        h.costs(session)
    return h.cli(
        "tune",
        "--session",
        session,
        "--round",
        round_number,
        "--providers",
        "zai,openai,deepseek",
        "--judge-env",
        h.env,
        *extra,
        code=code,
        reason=reason,
    )


def rows(h):
    return [
        json.loads(line)
        for line in (h.private / "exposure.jsonl").read_bytes().splitlines()
    ]


def records(h, session, provider):
    paths = sorted((h.ws / "debug" / session).glob(f"{provider}-*.json"))
    return [decode(eval_p7.DebugRecord, parse(p.read_bytes())) for p in paths]


def triage(h, session):
    return json.loads((h.ws / "tuning" / session / "triage.json").read_bytes())


def score(h, session):
    return json.loads((h.ws / "tuning" / session / "score.json").read_bytes())


def negative(h, *, split, normal=True):
    labels = {label.item_id: label for label in final_labels(h.pack).values()}
    return next(
        i
        for i in h.pack.items
        if i.split == split
        and not labels[i.item_id].verdict
        and i.scenario_id.startswith("eval-normal") is normal
    )


def failing(h, targets):
    """Responder: ``targets`` maps (provider, report_raw) to a reply factory."""
    seen = {}

    def respond(payload, provider, label):
        key = (provider, payload["report_raw"])
        factory = targets.get(key)
        if factory is None:
            return None
        seen[key] = seen.get(key, 0) + 1
        return factory(label, provider)

    h.responder = respond
    return seen


# ------------------------------------------------------------------- tune shape


def test_tune_sends_formal_shape_and_scores_a_clean_round(exposed):
    h = exposed
    result = tune(h, "tune-1", 1)
    assert result["outcome"] == "passed" and result["status"] == "ok"
    assert len(h.calls) == 180
    planned = [(i.item_id, r) for i, r in schedule(h.pack)]
    assert len(planned) == 60
    for provider in ("zai", "openai", "deepseek"):
        recs = records(h, "tune-1", provider)
        assert [r.item_id for r in recs] == [i for i, _ in planned]
        assert {r.provider for r in recs} == {provider}
    value = score(h, "tune-1")
    clean = {
        "agreement": 48,
        "invalid": 0,
        "missed_diagnoses": 0,
        "false_passes": 0,
        "started": 60,
        "passed": True,
    }
    assert value == {
        "round": 1,
        "session": "tune-1",
        "rerun_of": None,
        "prompt_hash": PROMPT_HASH,
        "label_pack_hash": h.pack_hash,
        "providers": {p: clean for p in ("zai", "openai", "deepseek")},
        "outcome": "passed",
    }
    assert triage(h, "tune-1")["rows"] == []
    row = rows(h)[-1]
    assert (row["kind"], row["tuning"], row["round"], row["session"]) == (
        "debug",
        True,
        1,
        "tune-1",
    )
    assert "rerun_of" not in row
    cost = h.cli("cost", "--session", "tune-1")["cost"]["providers"]
    assert cost["zai"]["calls"] == 60 and cost["deepseek"]["status"] == "measured"


def test_tune_refuses_unexposed_validation_and_changed_labels(harness):
    h = harness.ready()  # frozen, but no formal run exposed validation
    before = snapshot(h.root)
    tune(h, "tune-1", 1, code=3, reason="VALIDATION_EXPOSURE_FORBIDDEN", costs=False)
    assert snapshot(h.root) == before and not h.calls
    h.formal()
    h.calls.clear()
    annotation = h.ws / "annotations/annotations-1.json"
    original = annotation.read_bytes()
    annotation.write_bytes(original + b" ")
    h.costs("tune-1")
    before = snapshot(h.root)
    tune(h, "tune-1", 1, code=3, reason="HASH_MISMATCH", costs=False)
    assert snapshot(h.root) == before and not h.calls
    annotation.write_bytes(original)
    tune(h, "tune-1", 1, costs=False)


# ---------------------------------------------------------------- round counter


def test_round_counter_is_sequential_and_capped_at_three(exposed):
    h = exposed
    h.costs("tune-x")
    tune(h, "tune-x", 2, code=3, reason="TUNING_ROUND_MISMATCH", costs=False)
    tune(h, "tune-1", 1)
    tune(h, "tune-x", 1, code=3, reason="TUNING_ROUND_MISMATCH", costs=False)
    tune(h, "tune-2", 2)
    tune(h, "tune-3", 3)
    h.calls.clear()
    before = snapshot(h.root)
    tune(h, "tune-x", 4, code=3, reason="TUNING_ROUNDS_EXHAUSTED", costs=False)
    assert snapshot(h.root) == before and not h.calls
    assert [r.get("round") for r in rows(h) if r.get("tuning")] == [1, 2, 3]


def test_old_tuning_debug_rows_do_not_count_as_rounds(exposed):
    h = exposed
    h.costs("plain")
    h.cli("debug", "--session", "plain", "--judge-env", h.env, "--tuning")
    tune(h, "tune-1", 1)


def test_round_argument_must_be_a_positive_integer(exposed):
    h = exposed
    for bad in ("0", "one", "-1"):
        tune(h, "tune-1", bad, code=2, costs=False)


# --------------------------------------------------------------- scoring rules


def test_failures_count_over_all_sixty_and_luna_only_pass_stops(exposed):
    h = exposed
    debug_negative = negative(h, split="debug")
    validation_negative = negative(h, split="validation")
    http_item = next(
        i
        for i in h.pack.items
        if i.split == "validation" and i.item_id != validation_negative.item_id
    )
    seen = failing(
        h,
        {
            ("zai", debug_negative.request.report.raw): false_pass,
            ("zai", validation_negative.request.report.raw): false_pass,
            ("zai", http_item.request.report.raw): lambda *_: http(b"{}", status=500),
        },
    )
    result = tune(h, "tune-1", 1, code=6, reason="PENDING_USER_DECISION")
    assert result["outcome"] == "pending_user_decision"
    assert seen
    zai = score(h, "tune-1")["providers"]["zai"]
    # A debug false pass counts (formal counts validation only).
    assert zai["false_passes"] == 1 + 2
    assert zai["invalid"] == 2 and zai["started"] == 60
    assert zai["agreement"] == 48 - 2 - 2 and zai["passed"] is False
    hints = sorted(
        (r["item_id"], r["repetition"], r["hint"]) for r in triage(h, "tune-1")["rows"]
    )
    assert hints == sorted(
        [
            (debug_negative.item_id, 1, "false_pass"),
            (validation_negative.item_id, 1, "false_pass"),
            (validation_negative.item_id, 2, "false_pass"),
            (http_item.item_id, 1, "http"),
            (http_item.item_id, 2, "http"),
        ]
    )
    assert all(
        r["provider"] == "zai" and r["category"] is None
        for r in triage(h, "tune-1")["rows"]
    )


def test_deepseek_never_counts(exposed):
    h = exposed
    item = negative(h, split="debug")
    failing(
        h,
        {(p, item.request.report.raw): false_pass for p in ("zai", "openai")},
    )
    result = tune(h, "tune-1", 1, code=1, reason="TUNING_FAILED")
    assert result["outcome"] == "failed"
    assert score(h, "tune-1")["providers"]["deepseek"]["passed"] is True


def test_score_tuning_is_pure_and_strict(exposed):
    h = exposed
    tune(h, "tune-1", 1)
    labels = final_labels(h.pack)
    recs = records(h, "tune-1", "zai")
    assert score_tuning(h.pack, labels, recs) == TuningScore(48, 0, 0, 0, 60, True)
    # A stopped round keeps every started attempt and cannot pass.
    partial = score_tuning(h.pack, labels, recs[:59])
    assert partial.started == 59 and partial.passed is False
    with pytest.raises(JudgeError) as exc:
        score_tuning(h.pack, labels, [recs[-1], *recs[1:-1], recs[0]])
    assert exc.value.code == "IDENTITY_MISMATCH"
    with pytest.raises(JudgeError):
        score_tuning(h.pack, labels, [*recs[:59], records(h, "tune-1", "openai")[59]])
    with pytest.raises(JudgeError):
        score_tuning(h.pack, labels, [*recs, recs[0]])


def test_round_outcome_order():
    ok = TuningScore(48, 0, 0, 0, 60, True)
    bad = TuningScore(40, 1, 0, 0, 60, False)
    assert round_outcome({"zai": ok, "openai": bad, "deepseek": ok}) == "passed"
    assert round_outcome({"zai": ok, "openai": ok}) == "passed"
    assert round_outcome({"zai": bad, "openai": ok}) == "pending_user_decision"
    assert round_outcome({"zai": bad, "openai": bad, "deepseek": ok}) == "failed"
    assert round_outcome({"deepseek": ok}) == "failed"


# -------------------------------------------------------- triage and rerun


def _fail_zai_once(h):
    item = next(i for i in h.pack.items if i.split == "debug")
    failing(h, {("zai", item.request.report.raw): lambda *_: http(b"{}", status=502)})


def _categorise(h, session, category, **extra):
    path = h.ws / "tuning" / session / "triage.json"
    value = json.loads(path.read_bytes())
    for row in value["rows"]:
        row["category"] = category
        row.update(extra)
    path.write_text(json.dumps(value))


def test_triage_check_requires_every_row_categorised(exposed):
    h = exposed
    _fail_zai_once(h)
    tune(h, "tune-1", 1, code=6)
    h.cli("triage-check", "--session", "tune-1", code=3, reason="TRIAGE_INCOMPLETE")
    _categorise(h, "tune-1", "flaky")
    h.cli("triage-check", "--session", "tune-1", code=3, reason="SCHEMA_INVALID")
    _categorise(h, "tune-1", "eval_env", label_concern="label looks off")
    result = h.cli("triage-check", "--session", "tune-1")
    assert result["triage"]["zai"] == {
        "started": 60,
        "eval_env": 1,
        "judge_error": 0,
        "real_failure": 0,
    }
    assert result["triage"]["openai"]["started"] == 60
    # Machine fields cannot be edited.
    path = h.ws / "tuning/tune-1/triage.json"
    value = json.loads(path.read_bytes())
    value["rows"][0]["hint"] = "transport"
    path.write_text(json.dumps(value))
    h.cli("triage-check", "--session", "tune-1", code=3, reason="TRIAGE_MISMATCH")


def test_environment_only_rerun_keeps_the_round_and_the_original(exposed):
    h = exposed
    _fail_zai_once(h)
    tune(h, "tune-1", 1, code=6)
    h.costs("tune-1b")
    tune(
        h,
        "tune-1b",
        1,
        "--rerun-of",
        "tune-1",
        code=3,
        reason="TUNING_RERUN_FORBIDDEN",
        costs=False,
    )
    _categorise(h, "tune-1", "eval_env")
    h.responder = None
    original = snapshot(h.ws / "tuning/tune-1"), snapshot(h.ws / "debug/tune-1")
    result = tune(h, "tune-1b", 1, "--rerun-of", "tune-1", costs=False)
    assert result["outcome"] == "passed"
    assert (snapshot(h.ws / "tuning/tune-1"), snapshot(h.ws / "debug/tune-1")) == (
        original
    )
    value = score(h, "tune-1b")
    assert (value["round"], value["rerun_of"]) == (1, "tune-1")
    assert rows(h)[-1]["rerun_of"] == "tune-1" and rows(h)[-1]["round"] == 1
    # Only once per round; the rerun did not consume a round.
    tune(
        h,
        "tune-1c",
        1,
        "--rerun-of",
        "tune-1",
        code=3,
        reason="TUNING_RERUN_USED",
    )
    tune(h, "tune-2", 2)


def test_rerun_refused_for_judge_errors_or_a_changed_prompt(exposed):
    h = exposed
    _fail_zai_once(h)
    tune(h, "tune-1", 1, code=6)
    _categorise(h, "tune-1", "judge_error")
    h.costs("tune-1b")
    tune(
        h,
        "tune-1b",
        1,
        "--rerun-of",
        "tune-1",
        code=3,
        reason="TUNING_RERUN_FORBIDDEN",
        costs=False,
    )
    _categorise(h, "tune-1", "eval_env")
    with h.mp.context() as m:
        m.setattr(eval_p7, "judge", SimpleNamespace(PROMPT="changed prompt"))
        tune(
            h,
            "tune-1b",
            1,
            "--rerun-of",
            "tune-1",
            code=3,
            reason="JUDGE_PROMPT_CHANGED",
            costs=False,
        )
    tune(
        h,
        "tune-1b",
        2,
        "--rerun-of",
        "tune-1",
        code=3,
        reason="TUNING_ROUND_MISMATCH",
        costs=False,
    )
