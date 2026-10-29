"""Acceptance tests for issue #398: formal-shape judge tuning rounds.

Authority: the agreed #398 seams (``tune``, ``score_tuning`` semantics,
``tuning/<session>/{score,triage}.json``, ``triage-check``), the M3 decision
record second round Q10-Q11 and the P7 r3 tuning rule. No #398 implementation
was read. Everything goes through the public driver CLI with the existing
offline P7 harness: fake transports, synthetic workspace, a network guard that
refuses every socket. Representation details the contract leaves open are
listed in ISSUE_398_AMBIGUITIES.md and are not asserted here.

First draft: Codex gpt-6.1-sol (session 01a18678-3b6b-7a11-9613-85535b113df2),
revised and completed against the seams.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

import pytest
from eval_b3_support import (
    false_fail,
    false_pass,
    http_error,
    missed_diagnosis,
    not_json,
    timeout,
)
from eval_p7_support import (
    PROVIDERS,
    SECRETS,
    DriverHarness,
    put,
    snapshot,
    tuning_debug,
)

from opspilot.evaluation import judge
from scripts import eval_p7

HINTS = {
    "transport",
    "http",
    "timeout",
    "output_invalid",
    "missed_diagnosis",
    "false_pass",
    "disagreement",
}
CATEGORIES = ("eval_env", "judge_error", "real_failure")
# Exit code -> status, driver interface §0.1 (shared by every subcommand).
STATUS = {
    0: {"ok", "selected"},
    1: {"failed", "no_baseline"},
    3: {"refused"},
    6: {"pending_user_decision"},
}
# The contract fixes the outcome but not tune's exit code for a completed round:
# debug exits 0 when every planned call was made, formal maps results to 1/6.
COMPLETED = {
    "passed": {0},
    "failed": {0, 1},
    "pending_user_decision": {0, 6},
}


class CanaryStreamCut(Exception):
    """A connection failure: retried once by judge_once, then JUDGE_TRANSPORT_ERROR."""


@pytest.fixture
def harness(tmp_path, monkeypatch, capsys):
    return DriverHarness(tmp_path, monkeypatch, capsys, eval_p7).init()


def _ready(h: DriverHarness) -> DriverHarness:
    """The real round-1 starting point: frozen pack, a formal run already exposed
    every validation family (tuning may re-send only those, P7-D21)."""
    h.ready()
    h.formal()
    h.calls.clear()
    h.balance_calls.clear()
    h.responder = None
    return h


# ---------------------------------------------------------------- CLI helpers


def _cli_any(h: DriverHarness, command, *args, codes):
    """h.cli, but with a set of acceptable exit codes (status must still match)."""
    h.capsys.readouterr()
    argv = [command, "--workspace", h.workspace, *map(str, args)]
    try:
        actual = h.driver.main(argv)
    except SystemExit as exc:
        actual = exc.code
    out = h.capsys.readouterr().out
    result = json.loads(out)
    assert isinstance(result, dict)
    assert list(result) == sorted(result)
    assert {"command", "status", "exit_code", "reason"} <= result.keys()
    assert result["command"] == command
    assert actual == result["exit_code"], result
    assert actual in codes, result
    assert result["status"] in STATUS[actual], result
    for secret in SECRETS:
        assert secret not in out
    assert not h.network.attempts
    return result


def _tune_args(session, round_number, *, rerun_of=None, judge_env):
    args = [
        "--session",
        session,
        "--round",
        str(round_number),
        "--providers",
        ",".join(PROVIDERS),
        "--judge-env",
        judge_env,
    ]
    if rerun_of is not None:
        args += ["--rerun-of", rerun_of]
    return args


def _tune(h: DriverHarness, session, round_number, *, rerun_of=None, outcome):
    """A completed tuning round; returns (stdout result, score.json)."""
    h.costs(session)
    result = _cli_any(
        h,
        "tune",
        *_tune_args(session, round_number, rerun_of=rerun_of, judge_env=h.env),
        codes=COMPLETED[outcome],
    )
    score = _score(h, session)
    assert score["outcome"] == outcome, score
    return result, score


def _refused_tune(
    h: DriverHarness, session, round_number, *, rerun_of=None, reason=None
):
    """§0.1 exit 3: no judge call, no balance read, nothing written."""
    h.costs(session)
    h.calls.clear()
    h.balance_calls.clear()
    before = snapshot(h.root)
    result = h.cli(
        "tune",
        *_tune_args(session, round_number, rerun_of=rerun_of, judge_env=h.env),
        code=3,
        reason=reason,
    )
    assert snapshot(h.root) == before
    assert not h.calls and not h.balance_calls
    return result


# ---------------------------------------------------------------- artifacts


def _score(h: DriverHarness, session):
    path = h.ws / "tuning" / session / "score.json"
    assert path.is_file(), path
    return json.loads(path.read_bytes())


def _provider(score, provider):
    value = score["providers"][provider]
    assert {
        "agreement",
        "invalid",
        "missed_diagnoses",
        "false_passes",
        "started",
        "passed",
    } <= value.keys()
    return value


def _triage(h: DriverHarness, session):
    """(document, rows): the row list may be the document or one list inside it."""
    value = json.loads((h.ws / "tuning" / session / "triage.json").read_bytes())
    if isinstance(value, list):
        return value, value
    lists = [k for k, v in value.items() if isinstance(v, list)]
    assert len(lists) == 1, "triage.json must hold exactly one row list"
    return value, value[lists[0]]


def _write_triage(h: DriverHarness, session, document, rows):
    if isinstance(document, list):
        value = rows
    else:
        (key,) = [k for k, v in document.items() if isinstance(v, list)]
        value = {**document, key: rows}
    put(h.ws / "tuning" / session / "triage.json", value)


def _categorise(h: DriverHarness, session, category):
    document, rows = _triage(h, session)
    for row in rows:
        row["category"] = category
    _write_triage(h, session, document, rows)


def _round_rows(h: DriverHarness):
    """Exposure rows written by tune (a plain debug --tuning row has no round)."""
    path = h.private / "exposure.jsonl"
    rows = [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]
    return [r for r in rows if "round" in r]


# ---------------------------------------------------------------- fake judge


def _inject(h: DriverHarness, overrides):
    """overrides: {(provider, item_id, send_ordinal): fn(label, provider)}.

    The ordinal counts sends of that item to that provider; without a retry it is
    the repetition. A returned exception is raised from the transport.
    """
    raw_to_id = {item.request.report.raw: item.item_id for item in h.pack.items}
    sends = Counter()

    def responder(payload, provider, label):
        key = (provider, raw_to_id[payload["report_raw"]])
        sends[key] += 1
        fn = overrides.get((*key, sends[key]))
        if fn is None:
            return None
        response = fn(label, provider)
        if isinstance(response, BaseException):
            raise response
        return response

    h.responder = responder


def _labels(h):
    return {label.item_id: label for label in h.labels}


def _pick(h, *, split, verdict=None, normal=None, diagnoses=None, n=None):
    """The first matching pack item, or the first ``n`` as a list."""
    labels = _labels(h)
    found = [
        item
        for item in h.pack.items
        if item.split == split
        and (verdict is None or labels[item.item_id].verdict is verdict)
        and (normal is None or item.scenario_id.startswith("eval-normal") is normal)
        and (diagnoses is None or bool(labels[item.item_id].diagnoses) is diagnoses)
    ]
    assert len(found) >= (n or 1)
    return found[0] if n is None else found[:n]


def _positive_fault_validation(h, n):
    return _pick(h, split="validation", verdict=True, normal=False, diagnoses=True, n=n)


def _normal_negative(h, split):
    """hidden_normal_cause: verdict False, one causal diagnosis at claims[0]."""
    return _pick(h, split=split, verdict=False, normal=True)


def _fail_round(h):
    """zai and openai each get one HTTP error on a debug item: outcome failed."""
    item = _pick(h, split="debug")
    _inject(
        h,
        {
            ("zai", item.item_id, 1): http_error,
            ("openai", item.item_id, 1): http_error,
        },
    )
    return item


# ================================================================ schedule


def test_398_schedule_is_formal_shape_in_formal_order(harness):
    h = _ready(harness)
    sent = []

    def observe(payload, provider, _label):
        sent.append((provider, payload["report_raw"]))
        return None

    h.responder = observe
    _, score = _tune(h, "tune-order", 1, outcome="passed")

    # run_calibration order: provider, pack item, repetition (validation twice).
    expected = [
        (provider, item.request.report.raw)
        for provider in PROVIDERS
        for item in h.pack.items
        for _ in range(2 if item.split == "validation" else 1)
    ]
    splits = Counter(item.split for item in h.pack.items)
    assert splits == {"debug": 12, "validation": 24}
    assert sent == expected
    assert Counter(p for p, _ in sent) == {p: 60 for p in PROVIDERS}

    # Same per-call files as debug, under debug/<session>/: a .json per call; a
    # .raw only for a rejected answer with a response body (P7-D20), so none here.
    directory = h.ws / "debug" / "tune-order"
    for provider in PROVIDERS:
        names = sorted(p.name for p in directory.glob(f"{provider}-*.json"))
        assert names == [f"{provider}-{n:03d}.json" for n in range(1, 61)]
    assert not list(directory.glob("*.raw"))

    assert score["round"] == 1
    assert score["session"] == "tune-order"
    assert score["rerun_of"] is None
    assert score["prompt_hash"] == hashlib.sha256(judge.PROMPT.encode()).hexdigest()
    assert score["label_pack_hash"] == h.pack_hash
    assert set(score["providers"]) == set(PROVIDERS)
    clean = {
        "agreement": 48,
        "invalid": 0,
        "missed_diagnoses": 0,
        "false_passes": 0,
        "started": 60,
        "passed": True,
    }
    for provider in PROVIDERS:
        value = _provider(score, provider)
        assert {k: value[k] for k in clean} == clean, provider

    (row,) = _round_rows(h)
    assert row["kind"] == "debug"
    assert row["tuning"] is True
    assert row["round"] == 1
    assert row["session"] == "tune-order"
    assert row.get("rerun_of") is None


def test_398_tune_needs_formally_exposed_validation_items(harness):
    """Same --tuning exposure rule: unexposed validation items stay sealed."""
    h = harness
    h.ready()
    h.calls.clear()
    h.balance_calls.clear()
    _refused_tune(h, "tune-unexposed", 1)
    assert _round_rows(h) == []


@pytest.mark.parametrize(
    "target",
    [
        "assembled/items.json",
        "annotations/annotations-1.json",
        "adjudication/adjudications.json",
        "label-pack/adjudications.json",
    ],
)
def test_398_changed_items_annotations_or_labels_are_refused(harness, target):
    h = _ready(harness)
    path = h.ws / target
    assert path.is_file()
    path.write_bytes(path.read_bytes() + b"\n")
    _refused_tune(h, "tune-changed", 1)
    assert _round_rows(h) == []


def test_398_cost_handling_is_unchanged_from_debug(harness):
    h = _ready(harness)
    # Without the manual zai/openai "before" balances tune refuses, like debug.
    h.calls.clear()
    before = snapshot(h.root)
    h.cli(
        "tune",
        *_tune_args("tune-cost", 1, judge_env=h.env),
        code=3,
    )
    assert snapshot(h.root) == before and not h.calls

    _tune(h, "tune-cost", 1, outcome="passed")
    costs = h.ws / "costs" / "tune-cost"
    assert (costs / "deepseek-before.json").is_file()
    assert (costs / "deepseek-after.json").is_file()
    for provider in ("zai", "openai"):
        h.cli(
            "balance",
            "--session",
            "tune-cost",
            "--provider",
            provider,
            "--phase",
            "after",
            "--unknown",
        )
    report = h.cli("cost", "--session", "tune-cost")["cost"]
    assert {p: report["providers"][p]["calls"] for p in PROVIDERS} == {
        p: 60 for p in PROVIDERS
    }


# ================================================================ score


@pytest.mark.parametrize("mismatches", [2, 3])
def test_398_agreement_boundary_46_passes_45_fails(harness, mismatches):
    h = _ready(harness)
    # Positive fault validation items, both repetitions, until n disagreements.
    overrides = {}
    for item in _positive_fault_validation(h, 2):
        for repetition in (1, 2):
            if len(overrides) < mismatches:
                overrides[("zai", item.item_id, repetition)] = false_fail
    outcome = "passed" if mismatches == 2 else "failed"
    # At 45/48 openai must not turn the round into pending: it fails too.
    if mismatches == 3:
        overrides[("openai", _pick(h, split="debug").item_id, 1)] = http_error
    _inject(h, overrides)
    _, score = _tune(h, f"tune-agree-{mismatches}", 1, outcome=outcome)
    zai = _provider(score, "zai")
    assert zai["agreement"] == 48 - mismatches
    assert (zai["invalid"], zai["missed_diagnoses"], zai["false_passes"]) == (0, 0, 0)
    assert zai["started"] == 60
    assert zai["passed"] is (mismatches == 2)


@pytest.mark.parametrize("split", ["debug", "validation"])
@pytest.mark.parametrize("failure", ["invalid", "missed_diagnosis", "false_pass"])
def test_398_any_single_failure_anywhere_fails_the_provider(harness, failure, split):
    """Zero invalid / missed / false pass over all 60, debug items included."""
    h = _ready(harness)
    if failure == "invalid":
        item, fn, field = _pick(h, split=split), not_json, "invalid"
    elif failure == "missed_diagnosis":
        item, fn, field = (
            _normal_negative(h, split),
            missed_diagnosis,
            "missed_diagnoses",
        )
    else:
        item, fn, field = _normal_negative(h, split), false_pass, "false_passes"
    _inject(h, {("zai", item.item_id, 1): fn})

    # Luna still passes, so the round stops for the user (P7-D14).
    _, score = _tune(h, "tune-single", 1, outcome="pending_user_decision")
    zai = _provider(score, "zai")
    assert zai[field] == 1
    assert zai["started"] == 60
    assert zai["passed"] is False
    # Agreement is over the 48 validation observations only; a compromised
    # validation observation gets no agreement credit (formal rule).
    assert zai["agreement"] == (48 if split == "debug" else 47)


@pytest.mark.parametrize(
    "failure,fn,ordinals",
    [
        ("http", http_error, (1,)),
        ("timeout", timeout, (1,)),
        # judge_once retries one connection failure: both sends fail.
        ("transport", lambda _l, _p: CanaryStreamCut("CANARY-P7-CUT"), (1, 2)),
    ],
)
def test_398_transport_failure_counts_as_started_and_invalid(
    harness, failure, fn, ordinals
):
    h = _ready(harness)
    item = _pick(h, split="debug")
    _inject(h, {("zai", item.item_id, n): fn for n in ordinals})
    _, score = _tune(h, f"tune-{failure}", 1, outcome="pending_user_decision")
    zai = _provider(score, "zai")
    assert zai["invalid"] == 1
    assert zai["agreement"] == 48
    # The denominator never shrinks; how a retried send is counted is open.
    assert zai["started"] == 60 if len(ordinals) == 1 else zai["started"] >= 60
    assert zai["passed"] is False
    _, rows = _triage(h, f"tune-{failure}")
    assert [(r["item_id"], r["provider"], r["hint"]) for r in rows] == [
        (item.item_id, "zai", failure)
    ]


# ================================================================ outcome


OUTCOME_CASES = {
    # name: (zai fails, openai fails, deepseek fails, outcome)
    "all-pass": (False, False, False, "passed"),
    "flashx-only": (False, True, True, "passed"),
    "luna-only": (True, False, False, "pending_user_decision"),
    "neither": (True, True, False, "failed"),
    "deepseek-only-fails": (False, False, True, "passed"),
}


@pytest.mark.parametrize("name", sorted(OUTCOME_CASES))
def test_398_round_outcome_and_deepseek_never_counts(harness, name):
    zai_fails, openai_fails, deepseek_fails, outcome = OUTCOME_CASES[name]
    h = _ready(harness)
    item = _pick(h, split="debug")
    failing = {
        "zai": zai_fails,
        "openai": openai_fails,
        "deepseek": deepseek_fails,
    }
    _inject(h, {(p, item.item_id, 1): http_error for p, f in failing.items() if f})
    _, score = _tune(h, f"tune-{name}", 1, outcome=outcome)
    for provider, fails in failing.items():
        assert _provider(score, provider)["passed"] is not fails


# ================================================================ round counter


def test_398_round_counter_starts_at_one_and_rejects_skip_or_repeat(harness):
    h = _ready(harness)
    # An earlier plain tuning debug session is not a tuning round.
    h.costs("tune-plain")
    tuning_debug(h, "tune-plain")
    h.calls.clear()
    h.balance_calls.clear()
    _refused_tune(h, "tune-first-two", 2)
    _fail_round(h)
    _tune(h, "tune-r1", 1, outcome="failed")
    _refused_tune(h, "tune-r1-again", 1)
    _refused_tune(h, "tune-r3-early", 3)
    assert [(r["round"], r["session"]) for r in _round_rows(h)] == [(1, "tune-r1")]


def test_398_round_four_is_tuning_rounds_exhausted(harness):
    h = _ready(harness)
    for n in (1, 2, 3):
        _fail_round(h)
        _tune(h, f"tune-r{n}", n, outcome="failed")
    h.responder = None
    _refused_tune(h, "tune-r4", 4, reason="TUNING_ROUNDS_EXHAUSTED")
    assert [r["round"] for r in _round_rows(h)] == [1, 2, 3]


# ================================================================ rerun


def _failed_original(h, session="tune-s0"):
    _fail_round(h)
    _tune(h, session, 1, outcome="failed")
    return session


def test_398_rerun_keeps_round_and_leaves_original_byte_identical(harness):
    h = _ready(harness)
    s0 = _failed_original(h)
    _categorise(h, s0, "eval_env")
    original = {
        part: snapshot(h.ws / part / s0) for part in ("tuning", "debug", "costs")
    }

    h.responder = None
    _, score = _tune(h, "tune-s0-rerun", 1, rerun_of=s0, outcome="passed")
    assert score["round"] == 1
    assert score["rerun_of"] == s0
    assert {
        part: snapshot(h.ws / part / s0) for part in ("tuning", "debug", "costs")
    } == original
    rows = _round_rows(h)
    assert [(r["round"], r.get("rerun_of")) for r in rows] == [(1, None), (1, s0)]

    # Only once per round.
    _refused_tune(h, "tune-s0-rerun-2", 1, rerun_of=s0)
    # The rerun did not consume a round: the next round is 2.
    _fail_round(h)
    _tune(h, "tune-r2", 2, outcome="failed")


@pytest.mark.parametrize("filled", ["null", "judge_error", "real_failure"])
def test_398_rerun_requires_every_failing_row_eval_env(harness, filled):
    h = _ready(harness)
    s0 = _failed_original(h)
    document, rows = _triage(h, s0)
    assert len(rows) == 2
    rows[0]["category"] = "eval_env"
    rows[1]["category"] = None if filled == "null" else filled
    _write_triage(h, s0, document, rows)
    h.responder = None
    _refused_tune(h, "tune-s0-rerun", 1, rerun_of=s0)
    assert [r["session"] for r in _round_rows(h)] == [s0]


def test_398_rerun_refused_after_prompt_change(harness):
    h = _ready(harness)
    s0 = _failed_original(h)
    _categorise(h, s0, "eval_env")
    changed = judge.PROMPT + "\nCANARY-398-PROMPT-EDIT"
    h.mp.setattr(judge, "PROMPT", changed)
    if hasattr(eval_p7, "PROMPT"):
        h.mp.setattr(eval_p7, "PROMPT", changed)
    h.responder = None
    _refused_tune(h, "tune-s0-rerun", 1, rerun_of=s0)
    assert [r["session"] for r in _round_rows(h)] == [s0]


# ================================================================ triage


def test_398_triage_lists_every_non_clean_attempt_and_triage_check(harness):
    h = _ready(harness)
    debug_any = _pick(h, split="debug", n=3)
    disagree = _positive_fault_validation(h, 1)[0]
    missed = _normal_negative(h, "debug")
    passed_wrongly = _normal_negative(h, "validation")
    validation_any = _pick(h, split="validation", verdict=True)
    _inject(
        h,
        {
            ("zai", debug_any[0].item_id, 1): http_error,
            ("zai", disagree.item_id, 2): false_fail,  # repetition 2
            ("zai", debug_any[1].item_id, 1): lambda _l, _p: CanaryStreamCut("x"),
            ("zai", debug_any[1].item_id, 2): lambda _l, _p: CanaryStreamCut("x"),
            ("openai", missed.item_id, 1): missed_diagnosis,
            ("openai", passed_wrongly.item_id, 1): false_pass,
            ("deepseek", debug_any[2].item_id, 1): not_json,
            ("deepseek", validation_any.item_id, 1): timeout,
        },
    )
    _tune(h, "tune-triage", 1, outcome="failed")
    document, rows = _triage(h, "tune-triage")
    assert sorted(
        (r["item_id"], r["provider"], r["repetition"], r["hint"]) for r in rows
    ) == sorted(
        [
            (debug_any[0].item_id, "zai", 1, "http"),
            (disagree.item_id, "zai", 2, "disagreement"),
            (debug_any[1].item_id, "zai", 1, "transport"),
            (missed.item_id, "openai", 1, "missed_diagnosis"),
            (passed_wrongly.item_id, "openai", 1, "false_pass"),
            (debug_any[2].item_id, "deepseek", 1, "output_invalid"),
            (validation_any.item_id, "deepseek", 1, "timeout"),
        ]
    )
    assert all(r["hint"] in HINTS for r in rows)
    assert all(r["category"] is None for r in rows)

    labels_before = snapshot(h.ws / "label-pack")

    def check(code):
        raw = (h.ws / "tuning/tune-triage/triage.json").read_bytes()
        result = h.cli("triage-check", "--session", "tune-triage", code=code)
        assert (h.ws / "tuning/tune-triage/triage.json").read_bytes() == raw
        return result

    check(3)  # every row still null
    rows[0]["category"] = "eval_env"
    _write_triage(h, "tune-triage", document, rows)
    check(3)  # some rows still null
    filled = ["eval_env", "eval_env", "eval_env", "judge_error", "judge_error"]
    filled += ["real_failure", "CANARY-unknown"]
    for row, category in zip(rows, filled):
        row["category"] = category
    _write_triage(h, "tune-triage", document, rows)
    check(3)  # a value outside the set

    rows[-1]["category"] = "real_failure"
    rows[-1]["label_concern"] = "CANARY-398 label may be wrong"
    _write_triage(h, "tune-triage", document, rows)
    result = check(0)
    assert snapshot(h.ws / "label-pack") == labels_before

    counts = {"eval_env": 3, "judge_error": 2, "real_failure": 2}
    tallies = _dicts_with(result, set(CATEGORIES))
    assert (
        any({c: t[c] for c in CATEGORIES} == counts for t in tallies)
        or _sum(tallies) == counts
    ), result
    # The denominator is every started attempt (180, or 60 per provider).
    started = [
        v
        for k, v in _items(result)
        if isinstance(v, int) and ("started" in k or "denominator" in k)
    ]
    assert 180 in started or started.count(60) >= len(PROVIDERS), result


def _items(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from _items(v)
    elif isinstance(value, list):
        for v in value:
            yield from _items(v)


def _dicts_with(value, keys):
    found = []
    if isinstance(value, dict):
        if keys <= value.keys():
            found.append(value)
        for v in value.values():
            found += _dicts_with(v, keys)
    elif isinstance(value, list):
        for v in value:
            found += _dicts_with(v, keys)
    return found


def _sum(tallies):
    return {c: sum(t[c] for t in tallies) for c in CATEGORIES}
