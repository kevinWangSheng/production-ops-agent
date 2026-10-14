"""PR-B1 acceptance tests authored only from the public interface and contract.

Authority: docs/tasks/2026-10-14-eval-pr-b1-interface.md (cited I:<line>),
docs/tasks/2026-10-14-eval-pr-b-contract.md section 4 (C:<line>), issue #222
and the merged PR-A public code. No B1 implementation is read. Deterministic:
no model, network or database; every file lives under tmp_path. Unclear
points and the readings taken are listed in EVAL_B1_AMBIGUITIES.md.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import fields, replace

import pytest
from eval_b1_support import (
    ATTEMPT_SCHEMA,
    DELIVERY_NAME,
    FAULT,
    JUDGE_HASH,
    NORMAL,
    OTHER_HASH,
    PACK_HASH,
    RECORDING_NAME,
    ROUND_SCHEMA,
    SUMMARY_SCHEMA,
    Bundle,
    StorageError,
    build_round,
    calibration_capture,
    calibration_doc,
    canonical,
    code_of,
    evaluate_capture,
    forge_summary,
    frozen_manifest,
    journal_for,
    make_capture,
    read_doc,
    recalibrate,
    retarget_delivery,
    round_doc,
    sha,
    source_trace_record,
    summary_doc,
    tamper,
    to_json,
    trace_record,
    write_doc,
)
from eval_contract_support import EID, evaluation

from opspilot.evaluation import (
    AggregateError,
    CalibrationResult,
    DimensionValue,
    EvaluationCapture,
    LegacySummary,
    PlannedSlot,
    ReportCapture,
    aggregate,
    calibrate,
    load_attempt,
    load_calibration,
    load_round,
    load_summary,
    manifest_hash,
    read_legacy_summary,
    write_attempt,
    write_baseline,
    write_calibration,
    write_round,
    write_summary,
)


@pytest.fixture
def bundle(tmp_path):
    return Bundle(tmp_path / "bundle")


@pytest.fixture
def written(bundle):
    """A passing capture written by the B1 writer."""
    cap = make_capture(bundle)
    ref = write_summary(bundle.path("summary.json"), cap)
    return cap, ref, bundle.path("summary.json")


def load(bundle, path, manifest=None):
    return load_summary(
        path, manifest=manifest or frozen_manifest(), resolve=bundle.resolve
    )


def rejects(code, call, *args, **kwargs):
    with pytest.raises(StorageError) as excinfo:
        call(*args, **kwargs)
    assert code_of(excinfo) == code


def rejects_any(codes, call, *args, **kwargs):
    with pytest.raises(StorageError) as excinfo:
        call(*args, **kwargs)
    assert code_of(excinfo) in codes


# ================================================================ summary round trip


def test_summary_round_trip_equals_pr_a_evaluate(written, bundle):
    # I:39-40, I:72: load_summary re-runs PR-A evaluate() on the saved inputs.
    cap, ref, path = written
    before = path.read_bytes()
    result = load(bundle, path)
    assert result == evaluate_capture(cap)
    assert result.overall is True
    # I:52: functions never write the file they read.
    assert path.read_bytes() == before


def test_summary_writer_ref_describes_actual_file_bytes(written):
    # I:52 ref.path is the file name; I:68 sha256/bytes are of the actual bytes.
    _cap, ref, path = written
    data = path.read_bytes()
    assert ref.path == "summary.json"
    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.bytes == len(data)
    assert ref.media_type
    assert ref.visibility in ("private", "public")


def test_summary_file_layout_matches_contract(written):
    # I:56-58, I:64: exact top-level keys, every DTO field incl. nulls/defaults,
    # trace.entries is the full projection of capture.traces, report fields.
    cap, _ref, path = written
    doc = read_doc(path)
    assert doc == summary_doc(cap)
    capture_doc = doc["capture"]
    assert set(capture_doc) == {f.name for f in fields(EvaluationCapture)}
    # I:66: unmeasured usage/cost stay null, never defaulted to zero.
    assert capture_doc["usage"] is None and capture_doc["cost"] is None
    assert capture_doc["ledger_ref"] is None
    # I:64: ScenarioSpec.incident carries the full IncidentScenario.
    assert capture_doc["spec"]["incident"]["scenario_id"] == FAULT
    # I:64: writer emits UTC datetimes with +00:00.
    assert capture_doc["started_at"].endswith("+00:00")


def test_report_schema_error_is_saved_as_gate_failure(bundle):
    # I:58, I:66: an unparsable report is a gate failure, not a rejected Run;
    # report_parsed is null and report_sha256 is the raw UTF-8 SHA.
    raw = "not a report {"
    cap = replace(make_capture(bundle), report=ReportCapture(raw, "stop"))
    path = bundle.path("bad-report.json")
    write_summary(path, cap)
    doc = read_doc(path)
    assert doc["report_parsed"] is None
    assert doc["report_sha256"] == sha(raw.encode("utf-8"))
    result = load(bundle, path)
    assert result == evaluate_capture(cap)
    assert result.overall is False
    assert "REPORT_INVALID" in result.gate.reasons


def test_failing_run_round_trips_with_its_failure(bundle):
    # C:145, I:72: failing Runs are stored and read back, not dropped.
    cap = make_capture(bundle, replay_miss=True)
    path = bundle.path("miss.json")
    write_summary(path, cap)
    result = load(bundle, path)
    assert result == evaluate_capture(cap)
    assert result.failure_class == "replay_miss"


def test_foreign_product_run_trace_stays_evidence_incomplete(bundle):
    # I:72: a required trace bound to another product Run is not rejected;
    # PR-A judges it evidence_incomplete.
    cap = make_capture(bundle)
    cap = replace(
        cap,
        traces=(
            trace_record("attempt-1", product_run_id="product-other"),
            source_trace_record("attempt-1"),
        ),
    )
    path = bundle.path("foreign.json")
    write_summary(path, cap)
    result = load(bundle, path)
    assert result.evidence_status == "incomplete"
    assert result.overall is False


def test_null_judge_has_no_hash_binding(bundle):
    # I:72: judge None carries no judge-hash requirement.
    cap = replace(make_capture(bundle), judge=None)
    path = bundle.path("no-judge.json")
    write_summary(path, cap)
    assert load(bundle, path).judge_status == "not_run"


def test_reader_accepts_z_suffix_datetime(written, bundle):
    # I:64: reader accepts Z or +00:00.
    _cap, _ref, path = written

    def zulu(doc):
        doc["capture"]["started_at"] = "2026-10-13T12:00:00Z"

    out = tamper(path, bundle.path("zulu.json"), zulu)
    assert load(bundle, out).overall is True


# ================================================================ strict parsing


def test_duplicate_top_level_json_key_rejected(written, bundle):
    # I:82, C:144: duplicate JSON keys are SCHEMA_INVALID.
    _cap, _ref, path = written
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{")
    dup = bundle.path("dup.json")
    dup.write_text('{"schema":"' + SUMMARY_SCHEMA + '",' + text[1:], encoding="utf-8")
    rejects("SCHEMA_INVALID", load, bundle, dup)


def test_duplicate_nested_json_key_rejected(written, bundle):
    # I:66, I:82: every layer rejects duplicate keys.
    _cap, _ref, path = written
    text = path.read_text(encoding="utf-8")
    marker = '"attempt_id":"attempt-1"'
    assert marker in text
    dup = bundle.path("dup-nested.json")
    dup.write_text(text.replace(marker, marker + "," + marker, 1), encoding="utf-8")
    rejects("SCHEMA_INVALID", load, bundle, dup)


def test_invalid_json_and_non_object_rejected(bundle):
    # I:82: illegal JSON is SCHEMA_INVALID.
    broken = bundle.path("broken.json")
    broken.write_text('{"schema":', encoding="utf-8")
    rejects("SCHEMA_INVALID", load, bundle, broken)
    array = bundle.path("array.json")
    array.write_text("[]", encoding="utf-8")
    rejects("SCHEMA_INVALID", load, bundle, array)


@pytest.mark.parametrize("schema", ["opspilot-eval-summary-2", 1, None])
def test_unknown_explicit_schema_rejected(written, bundle, schema):
    # I:56, I:76: unknown explicit schema is SCHEMA_INVALID, never upgraded.
    _cap, _ref, path = written

    def change(doc):
        doc["schema"] = schema

    rejects("SCHEMA_INVALID", load, bundle, tamper(path, bundle.path("s.json"), change))


def test_legacy_summary_without_schema_is_unavailable(written, bundle):
    # I:76: a schema-less legacy file cannot be loaded as verified.
    _cap, _ref, path = written

    def drop(doc):
        del doc["schema"]

    out = tamper(path, bundle.path("legacy.json"), drop)
    rejects("EVIDENCE_UNAVAILABLE", load, bundle, out)


def _walk(doc, dotted):
    keys = dotted.split(".")
    for key in keys[:-1]:
        doc = doc[int(key)] if isinstance(doc, list) else doc[key]
    last = keys[-1]
    return doc, int(last) if isinstance(doc, list) else last


def _set(doc, dotted, value):
    parent, key = _walk(doc, dotted)
    parent[key] = value


def _del(doc, dotted):
    parent, key = _walk(doc, dotted)
    del parent[key]


@pytest.mark.parametrize(
    "where",
    [
        "notes",
        "capture.extra",
        "capture.attempt.extra",
        "evaluation.extra",
        "trace.entries.0.extra",
        "capture.spec.incident.extra",
    ],
)
def test_unknown_field_rejected(written, bundle, where):
    # I:56: unknown fields at any listed layer are SCHEMA_INVALID.
    _cap, _ref, path = written
    out = tamper(path, bundle.path("u.json"), lambda d: _set(d, where, 1))
    rejects("SCHEMA_INVALID", load, bundle, out)


@pytest.mark.parametrize(
    "where",
    [
        "report_parsed",
        "trace",
        "capture.recording_ref",
        "capture.usage",
        "capture.attempt.product_run_id",
        "evaluation.judge_config_hash",
    ],
)
def test_missing_field_rejected_even_when_nullable(written, bundle, where):
    # I:54-56: every listed field is required, including nullable/default ones.
    _cap, _ref, path = written
    out = tamper(path, bundle.path("m.json"), lambda d: _del(d, where))
    rejects("SCHEMA_INVALID", load, bundle, out)


@pytest.mark.parametrize(
    ("where", "value"),
    [
        ("capture.attempt.replay_miss", "false"),
        ("capture.attempt.replay_miss", 0),
        ("capture.attempt.started", "true"),
        ("capture.attempt.started", 1),
        ("capture.delivered." + EID + ".delivered", 1),
        ("capture.judge.calibrated", "true"),
        ("evaluation.overall", 1),
        ("evaluation.deterministic_pass", "true"),
    ],
)
def test_bool_impostors_rejected(written, bundle, where, value):
    # I:64, C:144: bool must be JSON true/false; "false"/0/1 are rejected.
    _cap, _ref, path = written
    out = tamper(path, bundle.path("b.json"), lambda d: _set(d, where, value))
    rejects("SCHEMA_INVALID", load, bundle, out)


@pytest.mark.parametrize(
    ("where", "value"),
    [
        ("capture.attempt.repetition", True),  # I:64 int does not accept bool
        ("capture.attempt.repetition", 1.0),
        ("capture.attempt.attempt_id", 7),
        ("capture.attempt.mode", "turbo"),  # I:64 Literal only listed strings
        ("capture.outcome.evidence_ids", EID),
        ("capture.judge.verdict", "true"),
        ("capture.delivery_refs." + EID + ".visibility", "secret"),
        ("evaluation.failure_class", "made_up"),
    ],
)
def test_wrong_types_rejected(written, bundle, where, value):
    # I:64, I:82: wrong JSON types and out-of-Literal values are SCHEMA_INVALID.
    _cap, _ref, path = written
    out = tamper(path, bundle.path("t.json"), lambda d: _set(d, where, value))
    rejects("SCHEMA_INVALID", load, bundle, out)


@pytest.mark.parametrize(
    "value", ["2026-10-13T20:00:00+08:00", "2026-10-13T12:00:00", "yesterday"]
)
def test_non_utc_datetime_rejected(written, bundle, value):
    # I:64: datetimes are UTC ISO8601 strings.
    _cap, _ref, path = written
    out = tamper(
        path, bundle.path("d.json"), lambda d: _set(d, "capture.started_at", value)
    )
    rejects("SCHEMA_INVALID", load, bundle, out)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_number_tokens_rejected(written, bundle, number):
    # I:66, C:144: no layer accepts NaN/Infinity, including free-form usage.
    _cap, _ref, path = written
    out = tamper(
        path, bundle.path("n.json"), lambda d: _set(d, "capture.usage", {"t": number})
    )
    rejects("SCHEMA_INVALID", load, bundle, out)


def test_overflowing_number_literal_rejected(written, bundle):
    # I:64: float only accepts finite JSON numbers; 1e999 parses to inf.
    _cap, _ref, path = written
    out = tamper(
        path, bundle.path("o.json"), lambda d: _set(d, "capture.cost", {"usd": 12.5})
    )
    text = out.read_text(encoding="utf-8")
    assert '"usd":12.5' in text
    out.write_text(text.replace('"usd":12.5', '"usd":1e999'), encoding="utf-8")
    rejects("SCHEMA_INVALID", load, bundle, out)


@pytest.mark.parametrize(
    ("where", "value"),
    [
        ("capture.answer.signature", None),  # fault needs signature
        ("capture.answer.injected", None),  # fault needs injected
        ("capture.answer.expected_output", []),
        ("capture.answer.signature.any_of", []),
        ("capture.answer.signature.min_matches", 0),
    ],
)
def test_pr_a_scenario_constraints_enforced(written, bundle, where, value):
    # I:64: PR-A scenario restrictions still hold; DTO constraint -> SCHEMA_INVALID.
    _cap, _ref, path = written
    out = tamper(path, bundle.path("c.json"), lambda d: _set(d, where, value))
    rejects("SCHEMA_INVALID", load, bundle, out)


def test_frozenset_with_duplicates_rejected(bundle):
    # I:64: frozenset arrays must not repeat members. Delivery ref and capture
    # are kept consistent so only the duplicate is wrong.
    cap = make_capture(bundle)
    doc = summary_doc(cap)
    rec = doc["capture"]["delivered"][EID]
    rec["time_scope_refs"] = rec["time_scope_refs"] * 2
    data = canonical(rec)
    (bundle.root / "dup-delivery.json").write_bytes(data)
    ref = doc["capture"]["delivery_refs"][EID]
    ref.update(path="dup-delivery.json", sha256=sha(data), bytes=len(data))
    rejects("SCHEMA_INVALID", load, bundle, write_doc(bundle.path("f.json"), doc))


@pytest.mark.parametrize(
    "bad_path",
    [
        "/abs/delivery.json",
        "../delivery.json",
        "a\\b.json",
        "a//b.json",
        "./delivery.json",
        "a/../delivery.json",
        "",
    ],
)
def test_ref_path_must_be_relative_posix_inside_package(written, bundle, bad_path):
    # I:68, I:82: ref path format errors are SCHEMA_INVALID.
    _cap, _ref, path = written
    out = tamper(
        path,
        bundle.path("p.json"),
        lambda d: _set(d, f"capture.delivery_refs.{EID}.path", bad_path),
    )
    rejects("SCHEMA_INVALID", load, bundle, out)


@pytest.mark.parametrize("bad_sha", ["ABC", "A" * 64, "g" * 64, "a" * 63])
def test_ref_sha_must_be_64_lower_hex(written, bundle, bad_sha):
    # I:68, I:82: hash format errors are SCHEMA_INVALID.
    _cap, _ref, path = written
    out = tamper(
        path,
        bundle.path("h.json"),
        lambda d: _set(d, f"capture.delivery_refs.{EID}.sha256", bad_sha),
    )
    rejects("SCHEMA_INVALID", load, bundle, out)


# ================================================================ derived fields


@pytest.mark.parametrize(
    ("where", "value"),
    [
        ("evaluation.overall", False),
        ("evaluation.gate.checks.signature", False),
        ("evaluation.failure_reasons", ["DIAGNOSIS_FAILED"]),
        ("evaluation.signature_matches", {}),
        ("evaluation.evidence_status", "incomplete"),
        ("report_parsed.summary", "A different summary."),
    ],
)
def test_tampered_derived_field_is_mismatch(written, bundle, where, value):
    # I:72, I:88, C:145: saved derived fields must equal recomputation.
    _cap, _ref, path = written
    out = tamper(path, bundle.path("x.json"), lambda d: _set(d, where, value))
    rejects("DERIVED_RESULT_MISMATCH", load, bundle, out)


def test_cached_pass_cannot_make_failing_run_pass(bundle):
    # I:72 "缓存不能决定通过": a replay_miss Run saved as passing is rejected.
    cap = make_capture(bundle, replay_miss=True)
    path = bundle.path("miss.json")
    write_summary(path, cap)

    def claim_pass(doc):
        ev = doc["evaluation"]
        ev.update(
            overall=True,
            deterministic_pass=True,
            failure_class=None,
            failure_reasons=[],
        )

    out = tamper(path, bundle.path("fake-pass.json"), claim_pass)
    rejects("DERIVED_RESULT_MISMATCH", load, bundle, out)


# ================================================================ hash / identity / evidence


def test_delivery_ref_bytes_changed_is_hash_mismatch(written, bundle):
    # I:62, I:85: external ref bytes are re-hashed by the reader, not trusted.
    _cap, _ref, path = written
    with bundle.path(DELIVERY_NAME).open("ab") as handle:
        handle.write(b" ")
    rejects("HASH_MISMATCH", load, bundle, path)


def test_recording_ref_bytes_checked(written, bundle):
    # I:62: non-null recording ref bytes/hash are verified.
    _cap, _ref, path = written
    bundle.path(RECORDING_NAME).write_bytes(b"\x00opaque-recording\x02")
    rejects("HASH_MISMATCH", load, bundle, path)


def test_resolver_bytes_not_trusted(written, bundle):
    # I:52: the reader checks bytes/SHA itself even if the resolver lies.
    _cap, _ref, path = written
    rejects(
        "HASH_MISMATCH",
        load_summary,
        path,
        manifest=frozen_manifest(),
        resolve=lambda ref: b"{}",
    )


def test_delivery_view_hash_wrong_is_hash_mismatch(bundle):
    # I:62, I:85: view_sha256 must recompute; evaluation is resealed so only
    # the view hash is wrong.
    cap = make_capture(bundle)
    bad = replace(cap.delivered[EID], view_sha256=OTHER_HASH)
    cap = retarget_delivery(bundle, cap, bad, "bad-view.json")
    rejects("HASH_MISMATCH", load, bundle, forge_summary(bundle, "v.json", cap))


def test_report_raw_hash_wrong_is_hash_mismatch(written, bundle):
    # I:58, I:85: report_sha256 is the raw UTF-8 SHA.
    _cap, _ref, path = written
    out = tamper(
        path, bundle.path("r.json"), lambda d: _set(d, "report_sha256", OTHER_HASH)
    )
    rejects("HASH_MISMATCH", load, bundle, out)


def test_attempt_manifest_hash_wrong_is_hash_mismatch(bundle):
    # I:72, I:85: attempt manifest hash must be PR-A manifest_hash(manifest).
    cap = make_capture(bundle)
    cap = replace(cap, attempt=replace(cap.attempt, manifest_hash=OTHER_HASH))
    rejects("HASH_MISMATCH", load, bundle, forge_summary(bundle, "mh.json", cap))


def test_capture_manifest_must_be_the_passed_manifest(bundle):
    # I:72, I:86: capture.manifest is bound to the passed manifest object.
    passed = frozen_manifest()
    cap = make_capture(bundle)
    other = dict(passed.dimensions)
    other["seed"] = DimensionValue("version", "v2", None)
    cap = replace(cap, manifest=replace(passed, dimensions=other))
    path = forge_summary(bundle, "mo.json", cap)
    rejects("IDENTITY_MISMATCH", load, bundle, path, passed)


def test_attempt_round_must_match_manifest(bundle):
    # I:72, I:86: attempt round_id binds the passed manifest.
    cap = make_capture(bundle)
    cap = replace(cap, attempt=replace(cap.attempt, round_id="round-other"))
    rejects("IDENTITY_MISMATCH", load, bundle, forge_summary(bundle, "ro.json", cap))


def test_scenario_version_mismatch_is_identity_mismatch(bundle):
    # I:72, I:86: spec/answer/attempt scenario version must agree.
    cap = make_capture(bundle)
    cap = replace(cap, answer=replace(cap.answer, version="v2"))
    good = evaluate_capture(make_capture(bundle))
    path = forge_summary(bundle, "sv.json", cap, good)
    rejects("IDENTITY_MISMATCH", load, bundle, path)


def test_trace_record_bound_to_other_attempt(bundle):
    # I:72, I:86: TraceRecord.attempt_id/evaluation_id equal the attempt_id.
    cap = make_capture(bundle)
    stray = replace(cap.traces[0], attempt_id="attempt-other")
    cap = replace(cap, traces=(stray, cap.traces[1]))
    rejects("IDENTITY_MISMATCH", load, bundle, forge_summary(bundle, "tr.json", cap))


def test_trace_record_evaluation_id_bound(bundle):
    # I:72: evaluation_id must also equal the current attempt_id.
    cap = make_capture(bundle)
    stray = replace(cap.traces[0], evaluation_id="attempt-other")
    cap = replace(cap, traces=(stray, cap.traces[1]))
    rejects("IDENTITY_MISMATCH", load, bundle, forge_summary(bundle, "te.json", cap))


def test_required_source_trace_rejected(bundle):
    # I:72, C:140: a source-role trace cannot pose as a required current trace.
    # Code not fixed by the table: see EVAL_B1_AMBIGUITIES.md item 4.
    cap = make_capture(bundle)
    source = source_trace_record("attempt-1", required=True)
    cap = replace(cap, traces=(cap.traces[0], source))
    path = forge_summary(bundle, "src.json", cap)
    rejects_any({"IDENTITY_MISMATCH", "SCHEMA_INVALID"}, load, bundle, path)


def test_trace_entries_must_project_capture_traces(written, bundle):
    # I:58: top-level trace.entries is checked against capture.traces.
    # Code unspecified: see EVAL_B1_AMBIGUITIES.md item 5.
    _cap, _ref, path = written

    def drift(doc):
        doc["trace"]["entries"][0]["project"] = "elsewhere"

    out = tamper(path, bundle.path("te2.json"), drift)
    with pytest.raises(StorageError):
        load(bundle, out)


def test_replay_run_id_must_equal_product_run(bundle):
    # I:72, I:86: non-null replay_run_id equals attempt.product_run_id.
    cap = replace(make_capture(bundle), replay_run_id="product-other")
    rejects("IDENTITY_MISMATCH", load, bundle, forge_summary(bundle, "rr.json", cap))


def test_delivery_ref_key_must_match_evidence(written, bundle):
    # I:62, I:86: delivery ref map keys match capture.delivered / evidence_id.
    _cap, _ref, path = written

    def rekey(doc):
        refs = doc["capture"]["delivery_refs"]
        refs["ev-other"] = refs.pop(EID)

    rejects(
        "IDENTITY_MISMATCH", load, bundle, tamper(path, bundle.path("k.json"), rekey)
    )


def test_delivery_ref_content_must_equal_capture(bundle):
    # I:62: ref JSON must equal capture.delivered exactly (fail-closed).
    # Code unspecified: see EVAL_B1_AMBIGUITIES.md item 6.
    cap = make_capture(bundle)
    other = replace(cap.delivered[EID], status="partial")
    ref = bundle.put("other.json", canonical(to_json(other)))
    cap = replace(cap, delivery_refs={EID: ref})
    with pytest.raises(StorageError):
        load(bundle, forge_summary(bundle, "dc.json", cap))


def test_judge_hash_not_frozen_is_judge_identity_mismatch(bundle):
    # I:72, I:87: judge hashes bind the manifest's hash dimensions.
    cap = make_capture(bundle)
    cap = replace(cap, judge=replace(cap.judge, config_hash=OTHER_HASH))
    path = forge_summary(bundle, "jh.json", cap)
    rejects("JUDGE_IDENTITY_MISMATCH", load, bundle, path)


def test_judge_dimension_not_hash_kind(bundle):
    # I:87: a non-hash judge dimension never yields a loaded evaluation.
    dims = dict(frozen_manifest().dimensions)
    dims["judge_config"] = DimensionValue("version", JUDGE_HASH, None)
    frozen = replace(frozen_manifest(), dimensions=dims)
    cap = make_capture(bundle, manifest=frozen)
    path = forge_summary(bundle, "jk.json", cap)
    rejects("JUDGE_IDENTITY_MISMATCH", load, bundle, path, frozen)


def test_unstarted_summary_rejected(bundle):
    # I:83, C:146, #222(1): a summary whose attempt never started is refused.
    cap = make_capture(bundle)
    started = evaluate_capture(cap)
    unstarted = replace(cap.attempt, started=False)
    cap = replace(cap, attempt=unstarted)
    path = forge_summary(bundle, "ns.json", cap, replace(started, attempt=unstarted))
    rejects("ATTEMPT_NOT_STARTED", load, bundle, path)


def test_write_summary_refuses_unstarted_attempt(bundle):
    # I:83, I:91: writers validate first; no file appears.
    cap = make_capture(bundle)
    cap = replace(cap, attempt=replace(cap.attempt, started=False))
    target = bundle.path("never.json")
    rejects("ATTEMPT_NOT_STARTED", write_summary, target, cap)
    assert not target.exists()


def test_missing_files_are_evidence_unavailable(written, bundle):
    # I:52, I:84: missing local file or external ref.
    _cap, _ref, path = written
    rejects("EVIDENCE_UNAVAILABLE", load, bundle, bundle.path("absent.json"))
    bundle.path(DELIVERY_NAME).unlink()
    rejects("EVIDENCE_UNAVAILABLE", load, bundle, path)


def test_resolver_exception_hides_raw_details(written, bundle):
    # I:78, I:84: resolver failure -> EVIDENCE_UNAVAILABLE; message and chain
    # carry no raw data, paths or credentials.
    _cap, _ref, path = written
    raw_detail = "SECRET-CANARY-0001 /private/bundle/delivery.json"

    def boom(ref):
        raise RuntimeError(raw_detail)

    with pytest.raises(StorageError) as excinfo:
        load_summary(path, manifest=frozen_manifest(), resolve=boom)
    assert code_of(excinfo) == "EVIDENCE_UNAVAILABLE"
    seen, error = set(), excinfo.value
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        assert "SECRET" not in str(error) and "/private/bundle" not in str(error)
        error = error.__cause__ or error.__context__


def test_symlinked_summary_path_rejected(written, bundle):
    # I:68: the reader rejects symlinks on the local path. Code: item 7.
    _cap, _ref, path = written
    link = bundle.path("link.json")
    link.symlink_to(path)
    rejects_any({"EVIDENCE_UNAVAILABLE", "SCHEMA_INVALID"}, load, bundle, link)


# ================================================================ legacy summaries


def test_legacy_single_trace_becomes_one_display_entry(tmp_path):
    # I:76: payload and byte hash unchanged, single trace -> one entry, unverified.
    path = tmp_path / "old.json"
    data = json.dumps({"run_id": "r1", "trace": {"run_id": "t", "read_back": "found"}})
    path.write_text(data, encoding="utf-8")
    before = path.read_bytes()
    legacy = read_legacy_summary(path)
    assert isinstance(legacy, LegacySummary)
    assert legacy.payload == json.loads(data)
    assert legacy.sha256 == sha(before)
    assert len(legacy.trace_entries) == 1
    assert legacy.verified is False
    assert path.read_bytes() == before


def test_legacy_entries_array_kept_and_missing_trace_empty(tmp_path):
    # I:76: trace.entries array kept as is; no trace -> no entries.
    entries = [{"role": "product_run", "run_id": "a"}, {"role": "judge", "run_id": "b"}]
    with_entries = tmp_path / "a.json"
    with_entries.write_text(
        json.dumps({"trace": {"entries": entries}}), encoding="utf-8"
    )
    assert [dict(e) for e in read_legacy_summary(with_entries).trace_entries] == entries
    bare = tmp_path / "b.json"
    bare.write_text(json.dumps({"run_id": "r"}), encoding="utf-8")
    assert read_legacy_summary(bare).trace_entries == ()
    assert read_legacy_summary(bare).verified is False


# ================================================================ attempt journals


def test_attempt_journal_round_trip_and_layout(bundle):
    # I:41-42, I:59: {"schema","journal"}; started=False may be frozen too.
    cap = make_capture(bundle)
    for name, journal in (
        ("j1.json", journal_for(cap)),
        ("j2.json", journal_for(cap, started=False)),
    ):
        ref = write_attempt(bundle.path(name), journal)
        data = bundle.path(name).read_bytes()
        assert ref.sha256 == sha(data) and ref.bytes == len(data)
        assert read_doc(bundle.path(name)) == {
            "schema": ATTEMPT_SCHEMA,
            "journal": to_json(journal),
        }
        assert load_attempt(bundle.path(name)) == journal


@pytest.mark.parametrize(
    ("where", "value"),
    [
        ("journal.attempt.started", "true"),
        ("journal.attempt.started", 1),
        ("journal.attempt.repetition", False),
        ("schema", "opspilot-eval-attempt-9"),
        ("journal.extra", None),
    ],
)
def test_attempt_journal_strict(bundle, where, value):
    # I:56, I:64, I:82.
    write_attempt(bundle.path("j.json"), journal_for(make_capture(bundle)))
    out = tamper(
        bundle.path("j.json"), bundle.path("jt.json"), lambda d: _set(d, where, value)
    )
    rejects("SCHEMA_INVALID", load_attempt, out)


# ================================================================ calibration


def test_calibration_round_trip_recomputes(bundle):
    # I:43-45, I:60, I:73: result equals calibrate() with default P7 params.
    cc = calibration_capture()
    ref = write_calibration(bundle.path("cal.json"), cc)
    assert ref.sha256 == sha(bundle.path("cal.json").read_bytes())
    assert read_doc(bundle.path("cal.json")) == calibration_doc(cc)
    result = load_calibration(
        bundle.path("cal.json"),
        expected_pack_hash=PACK_HASH,
        expected_judge_hash=JUDGE_HASH,
    )
    assert isinstance(result, CalibrationResult)
    assert result == recalibrate(cc)
    assert result.passed is True


def test_small_calibration_reads_back_as_failure(bundle):
    # I:73: too few samples is a readable failed result, not an error.
    cc = calibration_capture(items=4, negatives=2)
    write_calibration(bundle.path("small.json"), cc)
    result = load_calibration(
        bundle.path("small.json"),
        expected_pack_hash=PACK_HASH,
        expected_judge_hash=JUDGE_HASH,
    )
    assert result.passed is False
    assert "TOO_FEW_ITEMS" in result.reasons


def test_saved_calibration_passed_not_trusted(bundle):
    # #222(2), I:73, C:147: result is rebuilt from labels and verdicts. The
    # saved result is a self-consistent pass belonging to other verdicts.
    failing = calibration_capture(flip=2)
    honest_pass = recalibrate(calibration_capture())
    assert recalibrate(failing).passed is False and honest_pass.passed is True
    path = write_doc(bundle.path("forged.json"), calibration_doc(failing, honest_pass))
    rejects(
        "DERIVED_RESULT_MISMATCH",
        load_calibration,
        path,
        expected_pack_hash=PACK_HASH,
        expected_judge_hash=JUDGE_HASH,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"passed": True, "reasons": []},  # #222(2): passed with agreement too low
        {"agreement": 0},
        {"false_passes": 0},
        {"items": 30},
    ],
)
def test_saved_calibration_counter_tampering(bundle, changes):
    # I:73, I:88: saved result is compared field by field with recomputation.
    cc = calibration_capture(flip=2)
    doc = calibration_doc(cc)
    doc["result"].update(changes)
    rejects(
        "DERIVED_RESULT_MISMATCH",
        load_calibration,
        write_doc(bundle.path("c.json"), doc),
        expected_pack_hash=PACK_HASH,
        expected_judge_hash=JUDGE_HASH,
    )


def test_calibration_wrong_verdict_count_is_schema_invalid(bundle):
    # I:73, I:82 (updated): raw verdict count wrong -> SCHEMA_INVALID.
    cc = calibration_capture()
    item = next(iter(cc.labels))
    bad = replace(cc, verdicts=cc.verdicts + ((item, True),))
    path = write_doc(bundle.path("vc.json"), calibration_doc(bad, recalibrate(cc)))
    rejects(
        "SCHEMA_INVALID",
        load_calibration,
        path,
        expected_pack_hash=PACK_HASH,
        expected_judge_hash=JUDGE_HASH,
    )


@pytest.mark.parametrize(
    ("where", "value"),
    [
        ("capture.verdicts.0.1", 0),
        ("capture.verdicts.0.1", "false"),
        ("capture.verdicts.0.1", None),
    ],
)
def test_calibration_verdict_must_be_bool(bundle, where, value):
    # I:73 "错误判定不得用非 bool 表示", I:64.
    write_calibration(bundle.path("cal.json"), calibration_capture())
    out = tamper(
        bundle.path("cal.json"), bundle.path("cb.json"), lambda d: _set(d, where, value)
    )
    rejects(
        "SCHEMA_INVALID",
        load_calibration,
        out,
        expected_pack_hash=PACK_HASH,
        expected_judge_hash=JUDGE_HASH,
    )


def test_calibration_label_must_be_bool(bundle):
    # I:64: labels are JSON booleans.
    cc = calibration_capture()
    write_calibration(bundle.path("cal.json"), cc)
    first = next(iter(cc.labels))
    out = tamper(
        bundle.path("cal.json"),
        bundle.path("cl.json"),
        lambda d: _set(d, f"capture.labels.{first}", "false"),
    )
    rejects(
        "SCHEMA_INVALID",
        load_calibration,
        out,
        expected_pack_hash=PACK_HASH,
        expected_judge_hash=JUDGE_HASH,
    )


@pytest.mark.parametrize("which", ["pack", "judge"])
def test_calibration_expected_hashes(bundle, which):
    # I:73, I:87: expected frozen hashes are checked after recomputation.
    write_calibration(bundle.path("cal.json"), calibration_capture())
    kwargs = {"expected_pack_hash": PACK_HASH, "expected_judge_hash": JUDGE_HASH}
    kwargs[f"expected_{which}_hash"] = OTHER_HASH
    rejects(
        "JUDGE_IDENTITY_MISMATCH", load_calibration, bundle.path("cal.json"), **kwargs
    )


# ================================================================ rounds and denominators


@pytest.fixture
def rnd(tmp_path):
    return build_round(tmp_path / "round")


def _row(agg, scenario, mode):
    return next(r for r in agg.rows if (r.scenario_id, r.mode) == (scenario, mode))


def test_round_round_trip_equals_pr_a_aggregate(rnd):
    # I:46-47, I:61, I:74: load_round re-reads everything and calls aggregate().
    result = load_round(rnd["round_path"], resolve=rnd["bundle"].resolve)
    assert result == rnd["expected"]
    doc = read_doc(rnd["round_path"])
    assert doc == round_doc(rnd["round_capture"], rnd["expected"])
    assert doc["schema"] == ROUND_SCHEMA


def test_denominator_rules(rnd):
    # I:74, C:146: started-without-summary is an unknown failure in the
    # denominator; started=False or no journal is not_started.
    agg = load_round(rnd["round_path"], resolve=rnd["bundle"].resolve)
    row = _row(agg, FAULT, "replay")
    assert (row.planned, row.attempted, row.not_started) == (5, 3, 2)
    assert (row.passed, row.failed) == (1, 2)
    assert row.pass_rate == pytest.approx(1 / 3)
    # replay_miss counts as a failure, with its own rate (I:95).
    assert row.replay_miss == 1
    assert row.replay_miss_rate == pytest.approx(1 / 3)
    assert dict(row.failure_classes) == {"replay_miss": 1, "setup": 1}
    structural = _row(agg, NORMAL, "structural")
    assert structural.attempted == 1 and structural.pass_rate is None
    empty = _row(agg, NORMAL, "replay")
    assert (empty.attempted, empty.not_started, empty.pass_rate) == (0, 1, None)
    assert agg.baseline_complete is False
    assert agg.incomplete_reasons == ("SLOTS_NOT_STARTED", "EVIDENCE_INCOMPLETE")


def test_round_cached_aggregate_tampered(rnd, tmp_path):
    # I:61, I:88: cached aggregate must equal recomputation item by item.
    def fake(doc):
        doc["aggregate"]["baseline_complete"] = True
        doc["aggregate"]["incomplete_reasons"] = []

    out = tamper(rnd["round_path"], rnd["bundle"].path("r2.json"), fake)
    rejects("DERIVED_RESULT_MISMATCH", load_round, out, resolve=rnd["bundle"].resolve)

    def fake_row(doc):
        doc["aggregate"]["rows"][0]["passed"] += 1

    out = tamper(rnd["round_path"], rnd["bundle"].path("r3.json"), fake_row)
    rejects("DERIVED_RESULT_MISMATCH", load_round, out, resolve=rnd["bundle"].resolve)


def _round_with(rnd, name, **changes):
    rc = replace(rnd["round_capture"], **changes)
    return write_doc(rnd["bundle"].path(name), round_doc(rc, rnd["expected"]))


def test_round_duplicate_journal_rejected(rnd):
    # I:74, I:82: journal attempt_id/slot repeated -> SCHEMA_INVALID.
    journals = rnd["round_capture"].journals
    path = _round_with(rnd, "dj.json", journals=journals + (journals[0],))
    rejects("SCHEMA_INVALID", load_round, path, resolve=rnd["bundle"].resolve)


def test_round_unplanned_journal_rejected(rnd):
    # I:74, I:82: a journal for a slot not in the plan -> SCHEMA_INVALID.
    bundle = rnd["bundle"]
    extra = make_capture(bundle, attempt_id="attempt-9", repetition=9)
    ref = write_attempt(bundle.path("journal-9.json"), journal_for(extra))
    path = _round_with(rnd, "uj.json", journals=rnd["round_capture"].journals + (ref,))
    rejects("SCHEMA_INVALID", load_round, path, resolve=bundle.resolve)


def test_round_summary_for_unstarted_journal(rnd):
    # I:74, I:83, #222(1): a summary paired with a started=False journal.
    bundle = rnd["bundle"]
    ref = write_summary(bundle.path("summary-attempt-4.json"), rnd["caps"]["attempt-4"])
    summaries = {**rnd["summary_refs"], "attempt-4": ref}
    path = _round_with(rnd, "ns.json", summaries=summaries)
    rejects("ATTEMPT_NOT_STARTED", load_round, path, resolve=bundle.resolve)


def test_round_present_summary_unreadable_is_not_missing(rnd):
    # I:74: a present summary ref that cannot be read is EVIDENCE_UNAVAILABLE,
    # never silently treated as "started without summary".
    rnd["bundle"].path("summary-attempt-1.json").unlink()
    rejects(
        "EVIDENCE_UNAVAILABLE",
        load_round,
        rnd["round_path"],
        resolve=rnd["bundle"].resolve,
    )


def test_round_journal_bytes_changed(rnd):
    # I:52, I:85: journal ref bytes are re-hashed.
    with rnd["bundle"].path("journal-attempt-3.json").open("ab") as handle:
        handle.write(b"\n")
    rejects(
        "HASH_MISMATCH", load_round, rnd["round_path"], resolve=rnd["bundle"].resolve
    )


def test_round_journal_product_run_must_match_summary(rnd):
    # I:74, I:86: a journal that already names a product Run must agree.
    bundle = rnd["bundle"]
    journal = journal_for(rnd["caps"]["attempt-1"], product_run_id="product-other")
    ref = write_attempt(bundle.path("journal-pr.json"), journal)
    journals = tuple(
        ref if r == rnd["journal_refs"]["attempt-1"] else r
        for r in rnd["round_capture"].journals
    )
    path = _round_with(rnd, "pr.json", journals=journals)
    rejects("IDENTITY_MISMATCH", load_round, path, resolve=bundle.resolve)


def test_round_summary_frozen_inputs_must_match_journal(rnd):
    # I:74: summary must have the same frozen spec/answer as its journal.
    # Code not fixed by the table: see EVAL_B1_AMBIGUITIES.md item 8.
    bundle = rnd["bundle"]
    base = journal_for(rnd["caps"]["attempt-1"])
    changed = replace(base, answer=replace(base.answer, expected_output=("Other.",)))
    ref = write_attempt(bundle.path("journal-ans.json"), changed)
    journals = tuple(
        ref if r == rnd["journal_refs"]["attempt-1"] else r
        for r in rnd["round_capture"].journals
    )
    path = _round_with(rnd, "ans.json", journals=journals)
    rejects_any(
        {"IDENTITY_MISMATCH", "SCHEMA_INVALID"},
        load_round,
        path,
        resolve=bundle.resolve,
    )


def test_round_forged_calibration_rejected(rnd):
    # #222(2) through load_round: calibration file result is recomputed.
    bundle = rnd["bundle"]
    failing = calibration_capture(flip=2)
    data = canonical(calibration_doc(failing, recalibrate(calibration_capture())))
    ref = bundle.put("cal-forged.json", data)
    path = _round_with(rnd, "cf.json", calibration_ref=ref)
    rejects("DERIVED_RESULT_MISMATCH", load_round, path, resolve=bundle.resolve)


def test_write_round_validates_before_publishing(rnd):
    # I:91: writer validates all data first; nothing is published on failure.
    bundle = rnd["bundle"]
    journals = rnd["round_capture"].journals
    capture_ = replace(rnd["round_capture"], journals=journals + (journals[0],))
    before = sorted(p.name for p in bundle.root.iterdir())
    rejects(
        "SCHEMA_INVALID",
        write_round,
        bundle.path("new-round.json"),
        capture_,
        resolve=bundle.resolve,
    )
    assert sorted(p.name for p in bundle.root.iterdir()) == before


# ================================================================ baseline table


def _table_row(text, scenario, version, mode):
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.strip().startswith("|") and cells[:3] == [scenario, version, mode]:
            return cells[3:]
    raise AssertionError(f"no row for {scenario}/{version}/{mode}")


def _is_number(cell):
    try:
        float(cell.rstrip("%"))
    except ValueError:
        return False
    return True


def test_baseline_table(rnd, tmp_path):
    # I:95, C:166, B8: table from a reloaded round; passed/attempted口径;
    # replay_miss counted as failure with its own miss rate; null rates.
    dest = tmp_path / "baseline.md"
    ref = write_baseline(
        rnd["round_path"], resolve=rnd["bundle"].resolve, destination=dest
    )
    data = dest.read_bytes()
    assert ref.sha256 == sha(data) and ref.bytes == len(data)
    text = data.decode("utf-8")
    cells = _table_row(text, FAULT, "v1", "replay")
    planned, attempted, not_started, passed, failed, rate, miss, miss_rate, classes = (
        cells[:9]
    )
    assert (planned, attempted, not_started) == ("5", "3", "2")
    assert passed == "1/3"
    assert failed == "2"
    assert _is_number(rate)
    assert miss == "1/3"
    assert _is_number(miss_rate)
    assert "replay_miss" in classes and "setup" in classes
    structural = _table_row(text, NORMAL, "v1", "structural")
    assert structural[5] == "null"  # structural quality rate
    assert not _is_number(structural[7])  # miss rate only for replay
    empty = _table_row(text, NORMAL, "v1", "replay")
    assert empty[5] == "null" and empty[7] == "null"  # zero denominators
    assert "D5：回放5次、live2次；样本量有限，置信不足" in text
    assert "baseline_complete" in text
    assert "SLOTS_NOT_STARTED" in text and "EVIDENCE_INCOMPLETE" in text


def test_baseline_reloads_round_not_cache(rnd, tmp_path):
    # I:95, B8 (C:66): write_baseline re-runs load_round; a tampered cache is
    # refused and no table is published.
    def fake(doc):
        doc["aggregate"]["rows"][0]["passed"] += 1

    out = tamper(rnd["round_path"], rnd["bundle"].path("rb.json"), fake)
    dest = tmp_path / "never.md"
    rejects(
        "DERIVED_RESULT_MISMATCH",
        write_baseline,
        out,
        resolve=rnd["bundle"].resolve,
        destination=dest,
    )
    assert not dest.exists()


# ================================================================ exclusive, atomic writes


def test_every_writer_refuses_existing_target(bundle, tmp_path):
    # I:89, I:91: no writer overwrites an existing target.
    cap = make_capture(bundle)
    cases = [
        (write_summary, (cap,), {}),
        (write_attempt, (journal_for(cap),), {}),
        (write_calibration, (calibration_capture(),), {}),
    ]
    for index, (writer, args, kwargs) in enumerate(cases):
        target = bundle.path(f"exists-{index}.json")
        target.write_bytes(b"keep")
        rejects("OUTPUT_EXISTS", writer, target, *args, **kwargs)
        assert target.read_bytes() == b"keep"


def test_round_and_baseline_refuse_existing_target(rnd):
    # I:89, I:95.
    bundle = rnd["bundle"]
    existing = bundle.path("round-existing.json")
    existing.write_bytes(b"keep")
    rejects(
        "OUTPUT_EXISTS",
        write_round,
        existing,
        rnd["round_capture"],
        resolve=bundle.resolve,
    )
    assert existing.read_bytes() == b"keep"
    table = bundle.path("table.md")
    table.write_bytes(b"keep")
    rejects(
        "OUTPUT_EXISTS",
        write_baseline,
        rnd["round_path"],
        resolve=bundle.resolve,
        destination=table,
    )
    assert table.read_bytes() == b"keep"


def test_dangling_symlink_target_is_output_exists(bundle, tmp_path):
    # I:89: an existing symlink counts as existing; its pointee is not created.
    pointee = tmp_path / "pointee.json"
    link = bundle.path("link.json")
    link.symlink_to(pointee)
    rejects("OUTPUT_EXISTS", write_attempt, link, journal_for(make_capture(bundle)))
    assert not pointee.exists()
    assert link.is_symlink()


def test_concurrent_writers_only_one_wins(bundle):
    # I:89, I:91: hard-link exclusive publish; exactly one concurrent success.
    journal = journal_for(make_capture(bundle))
    target = bundle.path("race.json")
    gate = threading.Barrier(8)
    results = []

    def worker():
        gate.wait()
        try:
            results.append(write_attempt(target, journal))
        except StorageError as error:
            results.append(error.code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == 8
    assert sum(1 for r in results if not isinstance(r, str)) == 1
    assert sorted(r for r in results if isinstance(r, str)) == ["OUTPUT_EXISTS"] * 7
    assert load_attempt(target) == journal


def test_writer_leaves_only_the_target(tmp_path):
    # I:91: private temp file is removed after publishing and after refusal.
    out = tmp_path / "out"
    out.mkdir()
    cap = make_capture(Bundle(tmp_path / "src"))
    write_attempt(out / "a.json", journal_for(cap))
    rejects("OUTPUT_EXISTS", write_attempt, out / "a.json", journal_for(cap))
    assert sorted(p.name for p in out.iterdir()) == ["a.json"]


def test_writer_validation_failure_publishes_nothing(tmp_path):
    # I:91: data is validated before any file is written.
    out = tmp_path / "out"
    out.mkdir()
    cap = make_capture(Bundle(tmp_path / "src"))
    bad = journal_for(cap)
    bad = replace(bad, answer=replace(bad.answer, version="v9"))
    with pytest.raises(StorageError):
        write_attempt(out / "bad.json", bad)
    assert list(out.iterdir()) == []


def test_write_io_error_is_evidence_unavailable(tmp_path):
    # I:91: non-duplicate write IO errors are EVIDENCE_UNAVAILABLE.
    cap = make_capture(Bundle(tmp_path / "src"))
    rejects(
        "EVIDENCE_UNAVAILABLE",
        write_attempt,
        tmp_path / "no-such-dir" / "a.json",
        journal_for(cap),
    )


# ================================================================ aggregate() #222 defenses


def _agg_inputs(**attempt_changes):
    frozen = frozen_manifest()
    planned = (PlannedSlot(FAULT, "v1", "replay", 1),)
    ev = evaluation(attempt_record=None)
    attempt = replace(
        ev.attempt,
        round_id=frozen.round_id,
        manifest_hash=manifest_hash(frozen),
        scenario_id=FAULT,
        **attempt_changes,
    )
    ev = replace(
        ev,
        attempt=attempt,
        judge_config_hash=JUDGE_HASH,
        judge_calibration_hash=PACK_HASH,
    )
    return frozen, planned, [ev]


def _passing_calibration():
    return recalibrate(calibration_capture())


def test_aggregate_positive_control():
    # I:75: defenses keep PR-A semantics for honest inputs.
    frozen, planned, evs = _agg_inputs()
    agg = aggregate(frozen, planned, evs, _passing_calibration())
    assert agg.baseline_complete is True
    assert agg.rows[0].attempted == 1


@pytest.mark.parametrize("started", [False, 1, "true", None])
def test_aggregate_rejects_unstarted_evaluation(started):
    # #222(1), I:75, C:147: started not truly True -> ATTEMPT_NOT_STARTED;
    # the evaluation never fills a slot.
    frozen, planned, evs = _agg_inputs(started=started)
    with pytest.raises(AggregateError) as excinfo:
        aggregate(frozen, planned, evs, _passing_calibration())
    assert str(excinfo.value) == "ATTEMPT_NOT_STARTED"


@pytest.mark.parametrize(
    "changes",
    [
        {"agreement": 0},  # #222(2): passed=True with agreement 0
        {"false_passes": 1},  # #222(2): passed=True with a false pass
        {"items": 23, "comparisons": 46, "agreement": 46},  # passed below P7 item floor
        {"negatives": 11},
        {"passed": False},  # failed with no reasons although counts pass
        {"passed": False, "reasons": ("FALSE_PASS",)},  # reason not supported
        {"comparisons": 47},  # comparisons != items*2
        {"agreement": 49},  # agreement > comparisons
        {"negatives": 25},  # negatives > items
        {"passed": False, "reasons": ("FALSE_PASS",), "false_passes": 25},
        {"items": True},  # non-int count
        {"agreement": -1},
        {"negatives": 12.0},
    ],
)
def test_aggregate_rejects_inconsistent_calibration(changes):
    # #222(2), I:75, I:82 (updated): direct DTO errors -> AggregateError.
    frozen, planned, evs = _agg_inputs()
    forged = replace(_passing_calibration(), **changes)
    with pytest.raises(AggregateError) as excinfo:
        aggregate(frozen, planned, evs, forged)
    assert str(excinfo.value) == "CALIBRATION_INCONSISTENT"


def test_aggregate_keeps_pr_a_failed_calibration_compatibility():
    # I:99: a failed result with 48 agreement and 1 false pass stays accepted
    # (no joint agreement/false_pass gate) and still blocks the baseline.
    frozen, planned, evs = _agg_inputs()
    legacy = CalibrationResult(
        False,
        24,
        48,
        48,
        12,
        1,
        ("FALSE_PASS",),
        judge_config_hash=JUDGE_HASH,
        calibration_pack_hash=PACK_HASH,
    )
    agg = aggregate(frozen, planned, evs, legacy)
    assert agg.baseline_complete is False
    assert "JUDGE_NOT_CALIBRATED" in agg.incomplete_reasons


def test_aggregate_accepts_honest_failed_calibration():
    # I:75: a calibrate() failure is consistent and blocks the baseline only.
    frozen, planned, evs = _agg_inputs()
    labels = {f"i{n}": n >= 2 for n in range(4)}
    verdicts = [(k, v) for k, v in labels.items() for _ in range(2)]
    honest = calibrate(
        labels, verdicts, judge_config_hash=JUDGE_HASH, calibration_pack_hash=PACK_HASH
    )
    agg = aggregate(frozen, planned, evs, honest)
    assert "JUDGE_NOT_CALIBRATED" in agg.incomplete_reasons
