"""B1 offline regressions: raw inputs, immutable artifacts and fixed slots."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation import (
    REQUIRED_DIMENSIONS,
    AggregateError,
    ArtifactRef,
    AttemptJournal,
    AttemptRecord,
    CalibrationCapture,
    DeliveredRecord,
    DimensionValue,
    EvaluationCapture,
    JudgeResult,
    Manifest,
    PlannedSlot,
    ReportCapture,
    RoundCapture,
    StorageError,
    TraceEntry,
    TraceRecord,
    aggregate,
    calibrate,
    canonical_view_hash,
    evaluate,
    load_attempt,
    load_calibration,
    load_round,
    load_scenario,
    load_summary,
    manifest_hash,
    read_legacy_summary,
    write_attempt,
    write_baseline,
    write_calibration,
    write_round,
    write_summary,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def wire(obj):
    if hasattr(obj, "__dataclass_fields__"):
        return wire(asdict(obj))
    if isinstance(obj, dict):
        return {k: wire(v) for k, v in obj.items()}
    if isinstance(obj, (tuple, list)):
        return [wire(v) for v in obj]
    if isinstance(obj, frozenset):
        return sorted(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def raw(obj):
    return json.dumps(
        wire(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def reference(path, payload):
    return ArtifactRef(
        path,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        "application/json",
        "private",
    )


def resolver(root):
    return lambda ref: (root / ref.path).read_bytes()


def capture():
    spec, answer = load_scenario(ROOT / "evals/scenarios/eval-normal-1")
    manifest = Manifest(
        "round-1",
        {k: DimensionValue("hash", k + "-v1", None) for k in REQUIRED_DIMENSIONS},
    )
    attempt = AttemptRecord(
        "attempt-1",
        manifest.round_id,
        manifest_hash(manifest),
        spec.scenario_id,
        spec.version,
        "replay",
        1,
        True,
        "ran",
        False,
        "product-1",
    )
    report = ReportCapture(
        json.dumps(
            {
                "schema_version": "m0-report-v2",
                "assessment_status": "completed",
                "conclusion": "inconclusive",
                "summary": "No causal diagnosis.",
                "claims": [],
                "gaps": [],
                "next_steps": [],
            }
        ),
        "stop",
    )
    outcome = IncidentOutcome(
        spec.scenario_id,
        "completed",
        (),
        "report_available",
        (),
        ("read_only",),
        None,
        (),
        True,
    )
    trace = TraceRecord(
        TraceEntry("investigation", True, "found", "trace-1", "trace-1", "product-1"),
        "attempt-1",
        "attempt-1",
        "offline",
        None,
        "found",
        NOW,
    )
    return EvaluationCapture(
        manifest,
        spec,
        answer,
        attempt,
        outcome,
        report,
        {},
        {},
        (trace,),
        JudgeResult("ok", True, (), True, "judge_config-v1", "calibration_pack-v1"),
        {},
        started_at=NOW,
        ended_at=NOW,
    )


def score(cap):
    return evaluate(
        cap.spec,
        cap.answer,
        cap.attempt,
        cap.outcome,
        cap.report,
        cap.delivered,
        cap.authorized,
        [t.entry for t in cap.traces],
        cap.judge,
    )


def slot(cap):
    a = cap.attempt
    return PlannedSlot(a.scenario_id, a.scenario_version, a.mode, a.repetition)


def calibration_capture():
    labels = {str(i): i >= 12 for i in range(24)}
    return CalibrationCapture(
        labels,
        tuple((k, v) for k, v in labels.items() for _ in range(2)),
        "judge_config-v1",
        "calibration_pack-v1",
    )


@pytest.fixture
def summary(tmp_path):
    cap = capture()
    path = tmp_path / "summary.json"
    ref = write_summary(path, cap)
    assert path.read_bytes() == raw(json.loads(path.read_bytes()))
    assert ref == reference(path.name, path.read_bytes())
    return cap, path


def mutate(path, field, value):
    obj = json.loads(path.read_bytes())
    keys = field.split(".")
    container = obj
    for key in keys[:-1]:
        container = (
            container[int(key)] if isinstance(container, list) else container[key]
        )
    container[keys[-1]] = value
    path.write_bytes(raw(obj))


def test_summary_roundtrip(summary, tmp_path):
    cap, path = summary
    result = load_summary(path, manifest=cap.manifest, resolve=resolver(tmp_path))
    assert result == score(cap) and result.overall
    obj = json.loads(path.read_bytes())
    assert obj["report_parsed"]["summary"] == "No causal diagnosis."
    assert obj["report_sha256"] == hashlib.sha256(cap.report.raw.encode()).hexdigest()


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("schema", "unknown", "SCHEMA_INVALID"),
        ("unknown", True, "SCHEMA_INVALID"),
        ("capture.attempt.started", "false", "SCHEMA_INVALID"),
        ("capture.attempt.started", 0, "SCHEMA_INVALID"),
        ("capture.attempt.repetition", True, "SCHEMA_INVALID"),
        ("capture.attempt.repetition", 1.0, "SCHEMA_INVALID"),
        ("capture.outcome.report_available", 1, "SCHEMA_INVALID"),
        ("evaluation.overall", 1, "SCHEMA_INVALID"),
        ("capture.judge.calibrated", 0, "SCHEMA_INVALID"),
        ("capture.judge.verdict", "false", "SCHEMA_INVALID"),
        ("capture.judge.unknown", 0, "SCHEMA_INVALID"),
        ("capture.spec.incident.unknown", 0, "SCHEMA_INVALID"),
        ("capture.started_at", "2026-10-14T00:00:00", "SCHEMA_INVALID"),
        ("capture.started_at", "2026-10-14T00:00:00+08:00", "SCHEMA_INVALID"),
        ("report_parsed.unknown", "extra", "SCHEMA_INVALID"),
        ("report_parsed.summary", 0, "SCHEMA_INVALID"),
        ("capture.attempt.started", False, "ATTEMPT_NOT_STARTED"),
        ("evaluation.attempt.started", False, "ATTEMPT_NOT_STARTED"),
        ("capture.attempt.round_id", "other", "IDENTITY_MISMATCH"),
        ("capture.attempt.manifest_hash", "0" * 64, "HASH_MISMATCH"),
        ("capture.answer.version", "other", "IDENTITY_MISMATCH"),
        ("capture.outcome.scenario_id", "other", "IDENTITY_MISMATCH"),
        ("capture.spec.incident.scenario_id", "other", "IDENTITY_MISMATCH"),
        ("capture.replay_run_id", "other", "IDENTITY_MISMATCH"),
        ("capture.judge.config_hash", "other", "JUDGE_IDENTITY_MISMATCH"),
        ("capture.traces.0.attempt_id", "other", "IDENTITY_MISMATCH"),
        ("capture.traces.0.entry.role", "source_investigation", "IDENTITY_MISMATCH"),
        ("evaluation.overall", False, "DERIVED_RESULT_MISMATCH"),
        ("evaluation.deterministic_pass", False, "DERIVED_RESULT_MISMATCH"),
        ("evaluation.judge_pass", False, "DERIVED_RESULT_MISMATCH"),
        ("evaluation.failure_class", "setup", "DERIVED_RESULT_MISMATCH"),
        ("evaluation.failure_reasons", ["forged"], "DERIVED_RESULT_MISMATCH"),
        ("evaluation.evidence_status", "incomplete", "DERIVED_RESULT_MISMATCH"),
        ("evaluation.judge_status", "not_run", "DERIVED_RESULT_MISMATCH"),
        ("evaluation.diagnosis_pass", False, "DERIVED_RESULT_MISMATCH"),
        ("evaluation.signature_matches", {"fake": ["span"]}, "DERIVED_RESULT_MISMATCH"),
        (
            "evaluation.gate",
            {"passed": False, "checks": {}, "reasons": []},
            "DERIVED_RESULT_MISMATCH",
        ),
        ("capture.outcome.permissions", ["write"], "DERIVED_RESULT_MISMATCH"),
        ("report_sha256", "0" * 64, "HASH_MISMATCH"),
    ],
)
def test_summary_strictness_and_recomputation(summary, tmp_path, field, value, code):
    cap, path = summary
    mutate(path, field, value)
    with pytest.raises(StorageError, match=f"^{code}$"):
        load_summary(path, manifest=cap.manifest, resolve=resolver(tmp_path))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"a","schema":"b"}',
        b'{"x":{"a":1,"a":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1e999}',
        b"[]",
        b"null",
        b"\xff",
        b"{",
    ],
)
def test_strict_json_before_resolver(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_bytes(payload)

    def forbidden(_):
        pytest.fail("resolver must not run for malformed input")

    with pytest.raises(StorageError, match="^SCHEMA_INVALID$"):
        load_summary(path, manifest=capture().manifest, resolve=forbidden)


def with_delivery(tmp_path):
    cap = capture()
    view = {"tool": "metrics_query", "value": 1}
    record = DeliveredRecord(
        "e-1",
        cap.spec.stimulus.target_id,
        "ok",
        True,
        frozenset(),
        None,
        view,
        canonical_view_hash(view),
        True,
    )
    payload = raw(record)
    (tmp_path / "delivery.json").write_bytes(payload)
    return replace(
        cap,
        delivered={"e-1": record},
        delivery_refs={"e-1": reference("delivery.json", payload)},
    )


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("missing", "EVIDENCE_UNAVAILABLE"),
        ("hash", "HASH_MISMATCH"),
        ("view", "HASH_MISMATCH"),
        ("identity", "IDENTITY_MISMATCH"),
        ("dto", "DERIVED_RESULT_MISMATCH"),
        ("type", "SCHEMA_INVALID"),
        ("resolver", "EVIDENCE_UNAVAILABLE"),
    ],
)
def test_external_delivery_checks(tmp_path, mutation, code):
    cap = with_delivery(tmp_path)
    path = tmp_path / "summary.json"
    write_summary(path, cap)
    assert load_summary(path, manifest=cap.manifest, resolve=resolver(tmp_path)).overall
    resolve = resolver(tmp_path)
    delivery = tmp_path / "delivery.json"
    if mutation == "missing":
        delivery.unlink()
    elif mutation == "hash":
        delivery.write_bytes(b"{}")
    elif mutation == "resolver":

        def resolve(_):
            raise RuntimeError("credential-secret-value")
    else:
        fields = {
            "view": ("view.value", 2),
            "identity": ("evidence_id", "other"),
            "dto": ("delivered", False),
            "type": ("delivered", "false"),
        }
        mutate(delivery, *fields[mutation])
        mutate(
            path,
            "capture.delivery_refs.e-1",
            wire(reference(delivery.name, delivery.read_bytes())),
        )
    with pytest.raises(StorageError, match=f"^{code}$") as caught:
        load_summary(path, manifest=cap.manifest, resolve=resolve)
    assert "credential-secret-value" not in str(caught.value)
    assert caught.value.code == str(caught.value)


@pytest.mark.parametrize(
    "bad_path", ["/absolute", "../escape", "a/../b", "a//b", ".", "", "a\\b"]
)
def test_artifact_paths_fail_closed(tmp_path, bad_path):
    cap = with_delivery(tmp_path)
    ref = replace(cap.delivery_refs["e-1"], path=bad_path)
    with pytest.raises(StorageError, match="^SCHEMA_INVALID$"):
        write_summary(
            tmp_path / "summary.json", replace(cap, delivery_refs={"e-1": ref})
        )
    assert not (tmp_path / "summary.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("passed", False),
        ("agreement", 0),
        ("items", 0),
        ("negatives", 0),
        ("false_passes", 1),
        ("reasons", ["FALSE_PASS"]),
    ],
)
def test_calibration_cached_results_rejected(tmp_path, field, value):
    path = tmp_path / "calibration.json"
    cap = calibration_capture()
    write_calibration(path, cap)
    assert load_calibration(
        path,
        expected_pack_hash=cap.calibration_pack_hash,
        expected_judge_hash=cap.judge_config_hash,
    ).passed
    mutate(path, "result." + field, value)
    with pytest.raises(StorageError, match="^DERIVED_RESULT_MISMATCH$"):
        load_calibration(
            path,
            expected_pack_hash=cap.calibration_pack_hash,
            expected_judge_hash=cap.judge_config_hash,
        )


def test_calibration_failure_and_binding(tmp_path):
    cap = calibration_capture()
    cap = replace(cap, verdicts=(("0", True),) + cap.verdicts[1:])
    path = tmp_path / "calibration.json"
    write_calibration(path, cap)
    result = load_calibration(
        path,
        expected_pack_hash=cap.calibration_pack_hash,
        expected_judge_hash=cap.judge_config_hash,
    )
    assert not result.passed and result.reasons == ("FALSE_PASS",)
    with pytest.raises(StorageError, match="^JUDGE_IDENTITY_MISMATCH$"):
        load_calibration(
            path, expected_pack_hash="other", expected_judge_hash=cap.judge_config_hash
        )
    for bad in (
        replace(cap, labels={"x": "false"}),
        replace(cap, verdicts=cap.verdicts[:-1]),
    ):
        with pytest.raises(StorageError, match="^SCHEMA_INVALID$"):
            write_calibration(tmp_path / "bad.json", bad)


@pytest.mark.parametrize(
    "changes",
    [
        {"items": 0},
        {"comparisons": 0},
        {"agreement": 0},
        {"negatives": 0},
        {"false_passes": 1},
        {"passed": 1},
        {"items": True},
        {"negatives": -1},
        {"reasons": ("forged",)},
    ],
)
def test_aggregate_calibration_defense(changes):
    cap = capture()
    c = calibration_capture()
    calibrated = calibrate(
        c.labels,
        c.verdicts,
        judge_config_hash=c.judge_config_hash,
        calibration_pack_hash=c.calibration_pack_hash,
    )
    with pytest.raises(AggregateError, match="^CALIBRATION_INCONSISTENT$"):
        aggregate(
            cap.manifest, [slot(cap)], [score(cap)], replace(calibrated, **changes)
        )


def test_aggregate_unstarted_defense():
    cap = capture()
    for started in (False, 0, "true"):
        bad = replace(score(cap), attempt=replace(cap.attempt, started=started))
        with pytest.raises(AggregateError, match="^ATTEMPT_NOT_STARTED$"):
            aggregate(cap.manifest, [slot(cap)], [bad], None)


def round_capture(tmp_path, summary=True, started=True):
    cap = capture()
    journal = AttemptJournal(
        replace(cap.attempt, status="unknown", started=started, product_run_id=None),
        cap.spec,
        cap.answer,
        NOW,
    )
    journal_ref = write_attempt(tmp_path / "attempt.json", journal)
    assert load_attempt(tmp_path / "attempt.json") == journal
    summaries = (
        {cap.attempt.attempt_id: write_summary(tmp_path / "summary.json", cap)}
        if summary
        else {}
    )
    calibration_ref = write_calibration(
        tmp_path / "calibration.json", calibration_capture()
    )
    return cap, RoundCapture(
        cap.manifest, (slot(cap),), (journal_ref,), summaries, calibration_ref
    )


def test_complete_round_baseline_and_cached_aggregate(tmp_path):
    _, rnd = round_capture(tmp_path)
    path = tmp_path / "round.json"
    write_round(path, rnd, resolve=resolver(tmp_path))
    result = load_round(path, resolve=resolver(tmp_path))
    assert not result.baseline_complete
    assert result.incomplete_reasons == ("BASELINE_LAYOUT_MISMATCH",)
    assert result.rows[0].passed == result.rows[0].attempted == 1
    baseline = tmp_path / "baseline.md"
    write_baseline(path, resolve=resolver(tmp_path), destination=baseline)
    text = baseline.read_text()
    assert "1/1" in text and "100.00%" in text and "置信不足" in text
    assert "回放1次、live0次" in text and "回放5次、live2次" not in text
    assert "baseline_complete：false" in text
    original = baseline.read_bytes()
    with pytest.raises(StorageError, match="^OUTPUT_EXISTS$"):
        write_baseline(path, resolve=resolver(tmp_path), destination=baseline)
    assert baseline.read_bytes() == original
    mutate(path, "aggregate.rows.0.passed", 0)
    with pytest.raises(StorageError, match="^DERIVED_RESULT_MISMATCH$"):
        write_baseline(
            path, resolve=resolver(tmp_path), destination=tmp_path / "bad.md"
        )
    assert not (tmp_path / "bad.md").exists()


@pytest.mark.parametrize("started", [True, False])
def test_missing_summary_and_not_started(tmp_path, started):
    _, rnd = round_capture(tmp_path, summary=False, started=started)
    path = tmp_path / "round.json"
    write_round(path, rnd, resolve=resolver(tmp_path))
    row = load_round(path, resolve=resolver(tmp_path)).rows[0]
    assert (row.attempted, row.not_started, row.failed) == (
        (1, 0, 1) if started else (0, 1, 0)
    )
    assert row.failure_classes == ({"setup": 1} if started else {})
    write_baseline(
        path, resolve=resolver(tmp_path), destination=tmp_path / "baseline.md"
    )
    assert ("0.00%" if started else "null") in (tmp_path / "baseline.md").read_text()


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("duplicate_journal", "SCHEMA_INVALID"),
        ("duplicate_slot", "SCHEMA_INVALID"),
        ("unplanned", "SCHEMA_INVALID"),
        ("extra_summary", "IDENTITY_MISMATCH"),
        ("unstarted", "ATTEMPT_NOT_STARTED"),
        ("spec", "IDENTITY_MISMATCH"),
        ("missing", "EVIDENCE_UNAVAILABLE"),
    ],
)
def test_round_identity_and_slots(tmp_path, mutation, code):
    cap, rnd = round_capture(tmp_path)
    if mutation == "duplicate_journal":
        rnd = replace(rnd, journals=rnd.journals * 2)
    elif mutation == "duplicate_slot":
        rnd = replace(rnd, planned=rnd.planned * 2)
    elif mutation == "unplanned":
        rnd = replace(rnd, planned=())
    elif mutation == "extra_summary":
        rnd = replace(rnd, summaries={"other": next(iter(rnd.summaries.values()))})
    elif mutation == "missing":
        (tmp_path / "summary.json").unlink()
    else:
        journal = load_attempt(tmp_path / "attempt.json")
        if mutation == "unstarted":
            journal = replace(journal, attempt=replace(journal.attempt, started=False))
        else:
            journal = replace(
                journal,
                spec=replace(
                    cap.spec, stimulus=replace(cap.spec.stimulus, question="different")
                ),
            )
        rnd = replace(rnd, journals=(write_attempt(tmp_path / "j2.json", journal),))
    with pytest.raises(StorageError, match=f"^{code}$"):
        write_round(tmp_path / "bad.json", rnd, resolve=resolver(tmp_path))


def test_absent_journal_zero_denominator(tmp_path):
    _, rnd = round_capture(tmp_path)
    rnd = replace(rnd, journals=(), summaries={})
    write_round(tmp_path / "round.json", rnd, resolve=resolver(tmp_path))
    row = load_round(tmp_path / "round.json", resolve=resolver(tmp_path)).rows[0]
    assert row.attempted == 0 and row.not_started == 1 and row.pass_rate is None


@pytest.mark.parametrize(
    "mode,miss,quality", [("replay", True, 0.0), ("structural", False, None)]
)
def test_replay_miss_and_structural(tmp_path, mode, miss, quality):
    cap = capture()
    cap = replace(cap, attempt=replace(cap.attempt, mode=mode, replay_miss=miss))
    a = write_attempt(
        tmp_path / "journal.json", AttemptJournal(cap.attempt, cap.spec, cap.answer)
    )
    s = write_summary(tmp_path / "summary.json", cap)
    calibration_ref = write_calibration(
        tmp_path / "calibration.json", calibration_capture()
    )
    rnd = RoundCapture(
        cap.manifest, (slot(cap),), (a,), {cap.attempt.attempt_id: s}, calibration_ref
    )
    write_round(tmp_path / "round.json", rnd, resolve=resolver(tmp_path))
    row = load_round(tmp_path / "round.json", resolve=resolver(tmp_path)).rows[0]
    assert row.pass_rate == quality
    if miss:
        assert (row.passed, row.failed, row.replay_miss, row.replay_miss_rate) == (
            0,
            1,
            1,
            1.0,
        )


def test_atomic_concurrent_writers(summary, tmp_path):
    cap, _ = summary
    path = tmp_path / "concurrent.json"

    def writer(_):
        try:
            write_summary(path, cap)
            return "ok"
        except StorageError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(writer, range(12)))
    assert outcomes.count("ok") == 1 and outcomes.count("OUTPUT_EXISTS") == 11
    assert load_summary(path, manifest=cap.manifest, resolve=resolver(tmp_path)).overall
    assert list(tmp_path.glob(".eval-*.tmp")) == []


def test_symlinks_nonregular_and_legacy(summary, tmp_path):
    cap, path = summary
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(StorageError, match="^EVIDENCE_UNAVAILABLE$"):
        load_summary(link, manifest=cap.manifest, resolve=resolver(tmp_path))
    with pytest.raises(StorageError, match="^OUTPUT_EXISTS$"):
        write_summary(link, cap)
    parent_link = tmp_path / "parent"
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(StorageError, match="^EVIDENCE_UNAVAILABLE$"):
        load_summary(
            parent_link / path.name, manifest=cap.manifest, resolve=resolver(tmp_path)
        )
    with pytest.raises(StorageError, match="^EVIDENCE_UNAVAILABLE$"):
        load_summary(tmp_path, manifest=cap.manifest, resolve=resolver(tmp_path))
    for i, trace in enumerate(
        (None, {"run_id": "old"}, {"entries": [{"role": "attempt"}, {"role": "judge"}]})
    ):
        old = tmp_path / f"old-{i}.json"
        payload = {"overall": True, "trace": trace}
        original = json.dumps(payload).encode()
        old.write_bytes(original)
        legacy = read_legacy_summary(old)
        assert not legacy.verified and legacy.payload == payload
        assert legacy.sha256 == hashlib.sha256(original).hexdigest()
        assert len(legacy.trace_entries) == i
        with pytest.raises(StorageError, match="^EVIDENCE_UNAVAILABLE$"):
            load_summary(old, manifest=cap.manifest, resolve=resolver(tmp_path))
        assert old.read_bytes() == original


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("trace.entries.0.entry.required", 0, "SCHEMA_INVALID"),
        ("trace.extra", True, "SCHEMA_INVALID"),
        ("trace.entries", [], "DERIVED_RESULT_MISMATCH"),
        ("report_parsed.summary", "A different summary", "DERIVED_RESULT_MISMATCH"),
        ("evaluation.attempt.attempt_id", "different", "IDENTITY_MISMATCH"),
        ("evaluation.attempt.round_id", "different", "IDENTITY_MISMATCH"),
        ("evaluation.judge_config_hash", "different", "JUDGE_IDENTITY_MISMATCH"),
    ],
)
def test_trace_projection_and_cached_identity(summary, tmp_path, field, value, code):
    cap, path = summary
    mutate(path, field, value)
    with pytest.raises(StorageError, match=f"^{code}$"):
        load_summary(path, manifest=cap.manifest, resolve=resolver(tmp_path))


def test_manifest_and_ledger_binding(summary, tmp_path):
    cap, path = summary
    other = replace(cap.manifest, round_id="different")
    with pytest.raises(StorageError, match="^IDENTITY_MISMATCH$"):
        load_summary(path, manifest=other, resolve=resolver(tmp_path))
    ledger = reference("ledger.json", b"{}")
    new = replace(cap, ledger_ref=ledger)
    write_summary(tmp_path / "with-ledger.json", new)
    with pytest.raises(StorageError, match="^EVIDENCE_UNAVAILABLE$"):
        load_summary(
            tmp_path / "with-ledger.json",
            manifest=cap.manifest,
            resolve=resolver(tmp_path),
        )


def test_legacy_failed_calibration_stays_fail_closed():
    cap = capture()
    c = calibration_capture()
    result = calibrate(
        c.labels,
        c.verdicts,
        judge_config_hash=c.judge_config_hash,
        calibration_pack_hash=c.calibration_pack_hash,
    )
    result = replace(
        result, passed=False, agreement=48, false_passes=1, reasons=("FALSE_PASS",)
    )
    aggregated = aggregate(cap.manifest, [slot(cap)], [score(cap)], result)
    assert not aggregated.baseline_complete
    assert aggregated.incomplete_reasons == ("JUDGE_NOT_CALIBRATED",)


def test_calibration_strict_raw_and_saved_binding(tmp_path):
    cap = calibration_capture()
    path = tmp_path / "calibration.json"
    write_calibration(path, cap)
    mutate(path, "result.judge_config_hash", "other")
    with pytest.raises(StorageError, match="^JUDGE_IDENTITY_MISMATCH$"):
        load_calibration(
            path,
            expected_pack_hash=cap.calibration_pack_hash,
            expected_judge_hash=cap.judge_config_hash,
        )
    obj = json.loads(path.read_bytes())
    obj["result"]["judge_config_hash"] = cap.judge_config_hash
    obj["capture"]["verdicts"][0][1] = 0
    path.write_bytes(raw(obj))
    with pytest.raises(StorageError, match="^SCHEMA_INVALID$"):
        load_calibration(
            path,
            expected_pack_hash=cap.calibration_pack_hash,
            expected_judge_hash=cap.judge_config_hash,
        )


def test_publication_failure_leaves_no_partial_target(summary, tmp_path, monkeypatch):
    cap, _ = summary
    import opspilot.evaluation.storage as storage

    def failed_link(*args, **kwargs):
        raise OSError("secret-from-fs")

    monkeypatch.setattr(storage.os, "link", failed_link)
    path = tmp_path / "fail.json"
    with pytest.raises(StorageError, match="^EVIDENCE_UNAVAILABLE$"):
        write_summary(path, cap)
    assert not path.exists()
    assert not list(tmp_path.glob(".eval-*.tmp"))


def test_writer_bad_python_types_are_fixed_errors(tmp_path):
    cap = with_delivery(tmp_path)
    record = replace(cap.delivered["e-1"], time_scope_refs=frozenset({None, 1}))
    with pytest.raises(StorageError, match="^SCHEMA_INVALID$"):
        write_summary(
            tmp_path / "bad-types.json", replace(cap, delivered={"e-1": record})
        )
    with pytest.raises(StorageError, match="^SCHEMA_INVALID$"):
        write_summary(
            tmp_path / "surrogate.json", replace(cap, usage={"text": "\ud800"})
        )
    assert not (tmp_path / "bad-types.json").exists()
    assert not (tmp_path / "surrogate.json").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"negatives": 24, "agreement": 46},
        {
            "agreement": 48,
            "false_passes": 1,
            "passed": False,
            "reasons": ("FALSE_PASS",),
        },
        {"agreement": 23, "passed": False, "reasons": ("AGREEMENT_BELOW_THRESHOLD",)},
    ],
)
def test_calibration_joint_constraints(changes):
    cap = capture()
    c = calibration_capture()
    calibration = calibrate(c.labels, c.verdicts)
    changed = replace(calibration, **changes)
    if changed.passed:
        with pytest.raises(AggregateError, match="^CALIBRATION_INCONSISTENT$"):
            aggregate(cap.manifest, [slot(cap)], [score(cap)], changed)
    else:
        result = aggregate(cap.manifest, [slot(cap)], [score(cap)], changed)
        assert not result.baseline_complete
        assert "JUDGE_NOT_CALIBRATED" in result.incomplete_reasons


@pytest.mark.parametrize("missing", [False, True])
def test_round_rejects_false_calibration_claim(tmp_path, missing):
    cap, rnd = round_capture(tmp_path)
    if missing:
        rnd = replace(rnd, calibration_ref=None)
        failed = None
    else:
        c = calibration_capture()
        c = replace(c, verdicts=(("0", True),) + c.verdicts[1:])
        cr = write_calibration(tmp_path / "failed.json", c)
        rnd = replace(rnd, calibration_ref=cr)
        failed = calibrate(
            c.labels,
            c.verdicts,
            judge_config_hash=c.judge_config_hash,
            calibration_pack_hash=c.calibration_pack_hash,
        )
    rejected = tmp_path / "rejected-round.json"
    with pytest.raises(StorageError, match="^DERIVED_RESULT_MISMATCH$"):
        write_round(rejected, rnd, resolve=resolver(tmp_path))
    assert not rejected.exists()
    cached = aggregate(cap.manifest, rnd.planned, [score(cap)], failed)
    forged = tmp_path / "cached-round.json"
    forged.write_bytes(
        raw(
            {"schema": "opspilot-eval-aggregate-1", "capture": rnd, "aggregate": cached}
        )
    )
    with pytest.raises(StorageError, match="^DERIVED_RESULT_MISMATCH$"):
        load_round(forged, resolve=resolver(tmp_path))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"bad":',
        b'{"bad":"\\ud800"}',
        b'{"n":1e999}',
        b'{"x":1,"x":2}',
    ],
)
def test_codec_failure_chains_empty(tmp_path, payload):
    path = tmp_path / "invalid.json"
    path.write_bytes(payload)
    with pytest.raises(StorageError) as caught:
        load_summary(path, manifest=capture().manifest, resolve=resolver(tmp_path))
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_bad_datetime_has_no_context(summary, tmp_path):
    cap, path = summary
    mutate(path, "capture.started_at", "synthetic-private-date")
    with pytest.raises(StorageError, match="^SCHEMA_INVALID$") as caught:
        load_summary(path, manifest=cap.manifest, resolve=resolver(tmp_path))
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("cleanup", ["unlink", "close"])
def test_cleanup_failure_is_normalized(tmp_path, monkeypatch, cleanup):
    import opspilot.evaluation.storage as storage

    cap = capture()
    journal = AttemptJournal(cap.attempt, cap.spec, cap.answer)
    real = getattr(storage.os, cleanup)

    def fail(*args, **kwargs):
        real(*args, **kwargs)
        if cleanup == "unlink" or path.exists():
            raise PermissionError(13, "Permission denied", "/synthetic-private-path")

    monkeypatch.setattr(storage.os, cleanup, fail)
    path = tmp_path / "cleanup.json"
    with pytest.raises(StorageError, match="^EVIDENCE_UNAVAILABLE$") as caught:
        write_attempt(path, journal)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not hasattr(caught.value, "filename")
    assert path.exists()


def d1_slots():
    return tuple(
        PlannedSlot(spec.scenario_id, spec.version, mode, repetition)
        for directory in sorted((ROOT / "evals/scenarios").iterdir())
        if directory.is_dir()
        for spec, _answer in (load_scenario(directory),)
        for mode, repetitions in (("replay", 5), ("live", 2))
        for repetition in range(1, repetitions + 1)
    )


def write_layout_round(tmp_path, planned):
    base = capture()
    summaries, journals, evaluations = {}, [], []
    for index, planned_slot in enumerate(planned):
        directory = ROOT / "evals/scenarios" / planned_slot.scenario_id
        if directory.is_dir():
            spec, answer = load_scenario(directory)
        else:
            spec = replace(
                base.spec,
                scenario_id=planned_slot.scenario_id,
                incident=replace(
                    base.spec.incident, scenario_id=planned_slot.scenario_id
                ),
            )
            answer = replace(base.answer, scenario_id=planned_slot.scenario_id)
        spec = replace(spec, version=planned_slot.scenario_version)
        answer = replace(answer, version=planned_slot.scenario_version)
        aid, product_id, trace_id = (
            f"layout-{index}",
            f"product-{index}",
            f"trace-{index}",
        )
        attempt = replace(
            base.attempt,
            attempt_id=aid,
            scenario_id=spec.scenario_id,
            scenario_version=planned_slot.scenario_version,
            mode=planned_slot.mode,
            repetition=planned_slot.repetition,
            product_run_id=product_id,
        )
        trace = replace(
            base.traces[0],
            attempt_id=aid,
            evaluation_id=aid,
            entry=replace(
                base.traces[0].entry,
                run_id=trace_id,
                expected_run_id=trace_id,
                product_run_id=product_id,
            ),
        )
        cap = replace(
            base,
            spec=spec,
            answer=answer,
            attempt=attempt,
            traces=(trace,),
            outcome=replace(base.outcome, scenario_id=spec.scenario_id),
        )
        journals.append(
            write_attempt(
                tmp_path / f"journal-{index}.json",
                AttemptJournal(attempt, spec, answer),
            )
        )
        summaries[aid] = write_summary(tmp_path / f"summary-{index}.json", cap)
        evaluations.append(score(cap))
    c = calibration_capture()
    cr = write_calibration(tmp_path / "calibration.json", c)
    rc = RoundCapture(base.manifest, planned, tuple(journals), summaries, cr)
    path = tmp_path / "round.json"
    write_round(path, rc, resolve=resolver(tmp_path))
    calibration = calibrate(
        c.labels,
        c.verdicts,
        judge_config_hash=c.judge_config_hash,
        calibration_pack_hash=c.calibration_pack_hash,
    )
    raw_aggregate = aggregate(base.manifest, planned, evaluations, calibration)
    assert raw_aggregate.baseline_complete
    assert json.loads(path.read_bytes())["aggregate"] == wire(raw_aggregate)
    return path


@pytest.mark.parametrize(
    "mutation",
    [
        "exact",
        "missing_replay",
        "missing_live",
        "extra_replay",
        "extra_scenario",
        "structural",
        "split_version",
        "old_version",
        "shift_replay",
        "shift_live",
        "gap_replay",
    ],
)
def test_d1_layout_completion_gate_and_actual_plan_note(tmp_path, mutation):
    planned = d1_slots()
    assert len(planned) == 42 and len({s.scenario_id for s in planned}) == 6
    if mutation == "missing_replay":
        planned = planned[1:]
    elif mutation == "missing_live":
        planned = tuple(s for i, s in enumerate(planned) if i != 5)
    elif mutation == "extra_replay":
        planned += (replace(planned[0], repetition=6),)
    elif mutation == "extra_scenario":
        planned += (replace(planned[0], scenario_id="outside-d1"),)
    elif mutation == "structural":
        planned += (replace(planned[0], mode="structural"),)
    elif mutation in ("split_version", "old_version"):
        planned = tuple(
            replace(s, scenario_version=s.scenario_version + "-other")
            if s.scenario_id == planned[0].scenario_id
            and (mutation == "old_version" or s.mode == "live")
            else s
            for s in planned
        )
    elif mutation in ("shift_replay", "shift_live", "gap_replay"):
        mode = "live" if mutation == "shift_live" else "replay"
        planned = tuple(
            replace(s, repetition=s.repetition + 1)
            if s.scenario_id == planned[0].scenario_id
            and s.mode == mode
            and (mutation != "gap_replay" or s.repetition == 5)
            else s
            for s in planned
        )
    path = write_layout_round(tmp_path, planned)
    frozen = path.read_bytes()
    result = load_round(path, resolve=resolver(tmp_path))
    assert result.baseline_complete is (mutation == "exact")
    assert result.incomplete_reasons == (
        () if mutation == "exact" else ("BASELINE_LAYOUT_MISMATCH",)
    )
    baseline = tmp_path / "baseline.md"
    write_baseline(path, resolve=resolver(tmp_path), destination=baseline)
    text = baseline.read_text()
    replay = sum(s.mode == "replay" for s in planned)
    live = sum(s.mode == "live" for s in planned)
    structural = sum(s.mode == "structural" for s in planned)
    assert f"回放{replay}次、live{live}次、structural{structural}次" in text
    assert f"baseline_complete：{str(result.baseline_complete).lower()}" in text
    assert ("BASELINE_LAYOUT_MISMATCH" in text) is (mutation != "exact")
    assert path.read_bytes() == frozen


def test_layout_mismatch_preserves_existing_reasons(tmp_path):
    _, rnd = round_capture(tmp_path, summary=False)
    path = tmp_path / "unknown.json"
    write_round(path, rnd, resolve=resolver(tmp_path))
    before = json.loads(path.read_bytes())["aggregate"]
    result = load_round(path, resolve=resolver(tmp_path))
    assert result.incomplete_reasons == (
        *before["incomplete_reasons"],
        "BASELINE_LAYOUT_MISMATCH",
    )
    assert result.rows[0].attempted == 1 and result.rows[0].failed == 1
