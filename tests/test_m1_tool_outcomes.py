"""The five outcome classes and the raw/view/hash evidence they register.

``feature_list.json`` F3 requires input errors, connectivity failures, timeouts
and no-data results to stay distinguishable, and ``PRODUCT-CONSTRAINTS.md``
requires every observation to carry source, query, target, window, freshness
and an inspectable evidence reference. These tests assert the separation
directly: no assertion accepts "not ok" as a substitute for the exact class.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from opspilot.tools import (
    PROJECTION_REVISION,
    ParameterSpec,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
    TransportUnavailable,
    Window,
)
from opspilot.tools.registry import canonical, canonical_hash
from tests.m1_tool_support import (
    NOW,
    WINDOW_END,
    WINDOW_START,
    RecordingSink,
    body,
    build,
    registration,
    request,
)


def test_ok_outcome_registers_raw_bytes_view_and_both_hashes():
    payload = body([{"metric": "checkout", "value": 3}])
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(
        body=payload, data_as_of=NOW - timedelta(seconds=30)
    )

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("ok", None)
    assert outcome.source_contact == "confirmed"
    assert outcome.adopted
    record = outcome.evidence
    assert record.raw == payload
    assert record.raw_sha256 == hashlib.sha256(payload).hexdigest()
    assert record.view_sha256 == canonical_hash(record.view)
    assert record.projection_revision == PROJECTION_REVISION
    assert sink.records == [record]
    # A detached copy, not the same object -- see
    # test_the_model_view_is_detached_from_the_committed_evidence.
    assert outcome.model_view == record.view
    assert outcome.model_view is not record.view
    assert record.view["content"] == [{"metric": "checkout", "value": 3}]
    assert record.view["trust"] == "untrusted-evidence"
    assert record.freshness_seconds == 30.0
    assert record.view["freshness_seconds"] == 30.0
    assert record.operation.operation_id == "step-1-t0"
    # The evidence identity is per dispatched observation, not per stable
    # operation -- this assertion used to require them equal, which is exactly
    # what let two dispatches of one operation collide (see
    # test_two_dispatches_of_one_operation_get_distinct_evidence_ids). The
    # operation id stays in the view beside it for correlation.
    assert record.evidence_id.startswith("step-1-t0:")
    assert record.view["evidence_id"] == record.evidence_id
    assert record.view["operation_id"] == "step-1-t0"


def test_ok_outcome_registers_the_source_coverage_interval():
    payload = body([{"metric": "checkout", "value": 3}])
    source_start = datetime(2026, 9, 14, 0, 10, tzinfo=timezone.utc)
    source_end = datetime(2026, 9, 14, 0, 55, tzinfo=timezone.utc)
    executor, transport, _, _ = build()
    transport.response = TransportResponse(
        body=payload, source_start_at=source_start, source_end_at=source_end
    )

    outcome = executor.execute(request())

    assert outcome.status == "ok"
    assert outcome.evidence.source_start_at == source_start
    assert outcome.evidence.source_end_at == source_end
    assert outcome.model_view["source_start_at"] == source_start.isoformat()
    assert outcome.model_view["source_end_at"] == source_end.isoformat()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_start_at": datetime(2026, 9, 14, 0, 1)},
        {"source_start_at": NOW.replace(tzinfo=None), "source_end_at": NOW},
        {"source_start_at": NOW, "source_end_at": NOW.replace(tzinfo=None)},
        {"source_start_at": "invalid", "source_end_at": NOW},
        {"source_start_at": NOW, "source_end_at": "invalid"},
        {"source_end_at": datetime(2026, 9, 14, 0, 1, tzinfo=timezone.utc)},
        {
            "source_start_at": datetime(2026, 9, 14, 1, tzinfo=timezone.utc),
            "source_end_at": datetime(2026, 9, 14, 0, tzinfo=timezone.utc),
        },
    ],
)
def test_malformed_source_coverage_is_fail_closed(kwargs):
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(body=body([]), **kwargs)

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")
    assert outcome.evidence is None
    assert sink.records == []


def test_no_data_is_a_completed_query_not_an_error():
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(body=body([]))

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA")
    assert outcome.source_contact == "confirmed"
    # A completed empty query is still evidence, and it is still adopted.
    assert outcome.adopted and outcome.evidence in sink.records
    assert outcome.model_view["content"] == []
    assert outcome.model_view["result_count"] == 0
    assert outcome.evidence.raw == body([])


def test_source_error_is_never_reported_as_no_data():
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(body=body([]), source_status="500")

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "SOURCE_ERROR")
    assert outcome.source_contact == "confirmed"
    # Nothing was observed, so nothing is registered and nothing is adopted.
    assert outcome.evidence is None and sink.records == []
    assert outcome.model_view["content"] is None
    assert outcome.model_view["trust"] == "gateway"


@pytest.mark.parametrize(
    ("source_status", "reason"),
    [("503", "SOURCE_UNAVAILABLE"), ("400", "INVALID_PARAMS"), ("418", "SOURCE_ERROR")],
)
def test_source_status_is_classified_by_the_registration(source_status, reason):
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([]), source_status=source_status)

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", reason)


@pytest.mark.parametrize(
    ("error", "status", "reason", "contact"),
    [
        (TransportTimeout(), "timeout", "TOOL_TIMEOUT", "possible"),
        (TransportUnavailable(), "error", "SOURCE_UNAVAILABLE", "possible"),
        (TransportResultTooLarge(), "error", "RESULT_TOO_LARGE", "confirmed"),
        (
            ValueError("vendor text with 8f3c internals"),
            "error",
            "SOURCE_ERROR",
            "possible",
        ),
    ],
)
def test_transport_failures_are_classified_and_never_leak_vendor_text(
    error, status, reason, contact
):
    executor, transport, sink, _ = build()
    transport.error = error

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason, outcome.source_contact) == (
        status,
        reason,
        contact,
    )
    assert outcome.evidence is None and sink.records == []
    assert "vendor text" not in json.dumps(outcome.model_view)


def test_the_four_failure_and_empty_classes_stay_separable():
    outcomes = {}

    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([]))
    outcomes["no_data"] = executor.execute(request())

    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([]), source_status="500")
    outcomes["error"] = executor.execute(request())

    executor, transport, _, _ = build()
    transport.error = TransportTimeout()
    outcomes["timeout"] = executor.execute(request())

    executor, _, _, _ = build()
    outcomes["denied"] = executor.execute(request(target_ref="payments-prod"))

    assert {name: outcome.status for name, outcome in outcomes.items()} == {
        "no_data": "no_data",
        "error": "error",
        "timeout": "timeout",
        "denied": "denied",
    }
    assert len({outcome.reason for outcome in outcomes.values()}) == 4
    # Only the completed empty query produced an observation.
    assert [name for name, o in outcomes.items() if o.evidence is not None] == [
        "no_data"
    ]
    assert outcomes["denied"].source_contact == "none"
    assert outcomes["timeout"].source_contact == "possible"


def test_invalid_parameters_are_an_input_error_that_never_reaches_the_source():
    executor, transport, sink, _ = build()

    outcome = executor.execute(request(params={"step_seconds": "not-an-integer"}))

    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.source_contact == "none"
    assert not transport.called and sink.records == []


def test_missing_required_parameter_is_an_input_error():
    executor, transport, _, _ = build()

    outcome = executor.execute(request(params={"step_seconds": 15}))

    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert not transport.called


def test_a_string_parameter_holding_a_lone_surrogate_is_an_input_error():
    """Independent review finding on top of the source-response-row surrogate
    fix: a model-supplied string parameter (e.g. ``{"expr": "\\ud800"}``) is a
    valid Python ``str`` -- ``ParameterSpec.accepts()`` only ``isinstance``
    checks it -- but is not valid UTF-8. Left unchecked, it reaches
    ``_record()``'s ``view["query"] = dict(plan.params)`` and crashes
    ``canonical_hash(view)`` with an uncaught ``UnicodeEncodeError``: the same
    failure class as the row-level finding, but via the params input, on a
    currently-reachable dispatch path (no future combination-layer wiring
    required).
    """

    executor, transport, sink, _ = build()
    transport.response = TransportResponse(body=body(rows=[]))

    outcome = executor.execute(request(params={"expr": "\ud800"}))

    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.source_contact == "none"
    assert not transport.called and sink.records == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_numeric_parameter_is_an_input_error(bad):
    """Bot review finding: ``NaN``/``Infinity`` are valid Python ``float``
    values -- ``ParameterSpec.accepts()`` only ``isinstance`` checks a
    ``number`` parameter -- but ``canonical()`` serializes them as the bare,
    non-standard JSON tokens ``NaN``/``Infinity`` (``json.dumps``'s default),
    which a strict transport-side deserializer may reject or a source may
    interpret inconsistently, letting a model-supplied query value escape
    the declared JSON contract.
    """

    custom = registration(
        parameters={
            "expr": ParameterSpec("string", required=True),
            "magnitude": ParameterSpec("number"),
        }
    )
    executor, transport, sink, _ = build(registrations=[custom])
    transport.response = TransportResponse(body=body(rows=[]))

    outcome = executor.execute(request(params={"expr": "ok", "magnitude": bad}))

    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.source_contact == "none"
    assert not transport.called and sink.records == []


@pytest.mark.parametrize(
    "payload",
    [
        b"not json at all",
        b"\xff\xfe\x00",
        json.dumps({"data": {}}).encode(),
        json.dumps({"data": {"result": {"series": 1}}}).encode(),
        json.dumps([1, 2, 3]).encode(),
    ],
)
def test_unreadable_results_are_errors_not_empty_results(payload):
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(body=payload)

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")
    assert outcome.source_contact == "confirmed"
    assert outcome.evidence is None and sink.records == []


@pytest.mark.parametrize(
    "payload",
    [
        b"[" * 20_000 + b"]" * 20_000,  # RecursionError: too deeply nested
        b'{"data":{"result":' + b"9" * 5000 + b"}}",  # ValueError: int too long
    ],
    ids=["deeply-nested-recursion-error", "oversized-integer-value-error"],
)
def test_a_json_decoder_limit_is_malformed_not_a_crash(payload):
    """Bot review finding: an otherwise size-compliant but adversarial body
    can make Python's ``json`` decoder raise ``RecursionError`` or a
    digit-count ``ValueError`` instead of ``json.JSONDecodeError``. Neither
    was caught, so an untrusted source's response could abort ``execute()``
    outright instead of producing the promised ``MALFORMED_RESULT`` outcome.
    ``max_result_bytes`` is raised only for this test so the size check
    itself doesn't refuse the payload before the decoder ever runs.
    """

    executor, transport, sink, _ = build(
        registrations=[registration(max_result_bytes=len(payload) + 1024)]
    )
    transport.response = TransportResponse(body=payload)

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")
    assert outcome.source_contact == "confirmed"
    assert outcome.evidence is None and sink.records == []


def test_a_row_with_a_lone_surrogate_is_malformed_not_a_crash():
    """Bot review finding: a size-compliant body decodes fine (``json.loads``
    accepts a ``\\uD800``-style escape as a lone surrogate codepoint in a
    Python ``str``, no error), but ``_fit_rows()`` later calls
    ``canonical(row).encode("utf-8")``, which raises ``UnicodeEncodeError``
    -- a fresh failure mode beyond the JSON-decoder-limit fix, since it
    happens *after* decoding succeeds, during canonical re-encoding. An
    untrusted source response must not be able to crash ``execute()`` this
    way either.
    """

    payload = b'{"data":{"result":["\\ud800"]}}'
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(body=payload)

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")
    assert outcome.source_contact == "confirmed"
    assert outcome.evidence is None and sink.records == []


@pytest.mark.parametrize("token", [b"NaN", b"Infinity", b"-Infinity"])
def test_a_row_holding_a_non_finite_number_is_malformed_not_a_crash(token):
    """Bot review finding: a bare ``NaN``/``Infinity``/``-Infinity`` token is
    accepted by ``json.loads()``'s default ``parse_constant`` (a Python
    extension, not standard JSON) with no error, decoding into a non-finite
    ``float``. ``canonical()`` then re-emits the same non-standard token
    rather than raising, so it would otherwise reach the committed view
    unnoticed -- the row-level mirror of the non-finite *parameter* check
    added for the same reason.
    """

    payload = b'{"data":{"result":[' + token + b"]}}"
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(body=payload)

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")
    assert outcome.source_contact == "confirmed"
    assert outcome.evidence is None and sink.records == []


def test_a_non_response_object_from_the_transport_is_an_error():
    executor, transport, _, _ = build()
    transport.response = {"data": {"result": []}}

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")


def test_naive_freshness_from_an_adapter_is_refused():
    executor, transport, _, _ = build()
    transport.response = TransportResponse(
        body=body([{"value": 1}]), data_as_of=NOW.replace(tzinfo=None)
    )

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")


def test_view_truncation_is_marked_while_raw_evidence_stays_complete():
    rows = [{"series": index, "value": index * 2} for index in range(40)]
    payload = body(rows)
    executor, transport, _, _ = build(registrations=[registration(max_view_bytes=128)])
    transport.response = TransportResponse(body=payload)

    outcome = executor.execute(request())

    view = outcome.model_view
    assert outcome.status == "ok"
    assert view["result_count"] == 40
    assert 0 < view["returned_count"] < 40
    assert view["truncated"] is True
    assert view["omitted_rows"] == 40 - view["returned_count"]
    assert view["omitted_bytes"] > 0
    assert len(canonical(view["content"]).encode()) <= 128
    # The full capture stays available behind the evidence reference.
    assert outcome.evidence.raw == payload
    assert outcome.evidence.raw_sha256 == hashlib.sha256(payload).hexdigest()


def test_a_view_truncated_to_nothing_is_not_reported_as_no_data():
    rows = [{"series": "x" * 300}]
    executor, transport, _, _ = build(registrations=[registration(max_view_bytes=64)])
    transport.response = TransportResponse(body=body(rows))

    outcome = executor.execute(request())

    assert outcome.status == "ok"
    assert outcome.model_view["content"] == []
    assert outcome.model_view["result_count"] == 1
    assert outcome.model_view["truncated"] is True


@pytest.mark.parametrize(("rows", "status"), [([], "no_data"), ([{"value": 1}], "ok")])
def test_an_incomplete_result_is_marked_in_the_view_and_the_record(rows, status):
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body(rows, partial=True))

    outcome = executor.execute(request())

    assert outcome.status == status
    assert outcome.model_view["incomplete"] is True
    assert outcome.evidence.incomplete is True


def test_unknown_freshness_stays_unknown_instead_of_becoming_zero():
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([{"value": 1}]))

    outcome = executor.execute(request())

    assert outcome.evidence.data_as_of is None
    assert outcome.evidence.freshness_seconds is None
    assert outcome.model_view["data_as_of"] is None
    assert outcome.model_view["freshness_seconds"] is None


def test_evidence_must_be_committed_before_it_may_be_consumed():
    executor, transport, sink, _ = build(sink=RecordingSink(fail=True))
    transport.response = TransportResponse(body=body([{"value": 1}]))

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "EVIDENCE_NOT_COMMITTED")
    assert outcome.source_contact == "confirmed"
    assert outcome.evidence is None
    assert outcome.model_view["content"] is None
    assert len(sink.records) == 1  # it was offered, it was not committed


def test_the_model_view_is_detached_from_the_committed_evidence():
    """Bot review finding: ``model_view`` and ``evidence.view`` were the same
    mutable dict object. A caller mutating the model-facing view (e.g.
    context assembly appending to or normalizing content) would silently
    mutate the "committed" evidence's view too, leaving it inconsistent with
    ``view_sha256``, which was computed before any such mutation -- and any
    sink that retained the record object would see its evidence change after
    registration.
    """

    executor, transport, sink, _ = build()
    transport.response = TransportResponse(body=body([{"value": 1}]))

    outcome = executor.execute(request())

    outcome.model_view["content"].append({"injected": "value"})
    outcome.model_view["extra"] = "mutated"

    assert outcome.evidence.view["content"] == [{"value": 1}]
    assert "extra" not in outcome.evidence.view
    assert sink.records[0].view["content"] == [{"value": 1}]
    assert outcome.evidence.view_sha256 == canonical_hash(outcome.evidence.view)


def test_mutating_a_committed_evidence_view_does_not_corrupt_the_stored_record():
    """Bot review finding: ``EvidenceRecord.view`` handed out the same mutable
    dict object on every access, so a consumer that mutated
    ``outcome.evidence.view`` (or an evidence sink that retained the record
    and later mutated its own reference) permanently corrupted the committed
    evidence even though ``view_sha256`` still hashed the original,
    pre-mutation contents.
    """

    executor, transport, sink, _ = build()
    transport.response = TransportResponse(body=body([{"value": 1}]))

    outcome = executor.execute(request())
    record = outcome.evidence

    first_access = record.view
    first_access["content"].append({"injected": "value"})
    first_access["extra"] = "mutated"

    second_access = record.view
    assert "extra" not in second_access
    assert second_access["content"] == [{"value": 1}]
    assert sink.records[0].view["content"] == [{"value": 1}]
    assert record.view_sha256 == canonical_hash(record.view)


def test_a_mismatched_evidence_reference_is_not_a_commit():
    executor, transport, _, _ = build(sink=RecordingSink(reference="other-evidence"))
    transport.response = TransportResponse(body=body([{"value": 1}]))

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "EVIDENCE_NOT_COMMITTED")
    assert outcome.evidence is None


def test_raw_evidence_is_byte_identical_to_the_source_payload():
    payload = '{"data": {"result": [{"msg": "中文 spacing"}]}}'.encode()
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=payload)

    outcome = executor.execute(request())

    assert outcome.evidence.raw == payload
    assert outcome.evidence.raw_sha256 == hashlib.sha256(payload).hexdigest()
    assert outcome.model_view["content"] == [{"msg": "中文 spacing"}]


def test_the_operation_record_carries_source_query_target_window_and_timing():
    executor, transport, _, clock = build()
    transport.clock, transport.duration = clock, 1.5
    transport.response = TransportResponse(body=body([{"value": 1}]))

    outcome = executor.execute(request())

    audit = outcome.operation.audit_json()
    assert audit["operation_id"] == "step-1-t0"
    assert audit["subject_kind"] == "incident"
    assert audit["subject_id"] == "incident-42"
    assert audit["run_id"] == "run-9"
    assert audit["tool"] == "metrics.range_query"
    assert audit["tool_version"] == "v1"
    assert audit["source"] == "prometheus"
    assert audit["target_id"] == "checkout-prod"
    assert audit["query"] == '{"expr":"rate(http_errors[5m])"}'
    assert audit["window"]["start"] == "2026-09-14T00:00:00+00:00"
    assert audit["elapsed_seconds"] == 1.5
    assert audit["timeout_seconds"] == 10.0
    assert audit["sent"] is True
    assert executor.operations_used == 1
    assert executor.tool_seconds_used == 1.5


def test_operation_identity_is_stable_per_step_and_tool_index():
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([{"value": 1}]))

    first = executor.execute(request(tool_index=0))
    second = executor.execute(request(tool_index=1))

    assert first.operation.operation_id == "step-1-t0"
    assert second.operation.operation_id == "step-1-t1"
    assert first.evidence.evidence_id != second.evidence.evidence_id


def test_unknown_source_interval_is_not_filled_from_query_or_freshness():
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([]), data_as_of=NOW)
    outcome = executor.execute(request())
    assert outcome.status == "no_data"
    assert outcome.evidence.source_start_at is None
    assert outcome.evidence.source_end_at is None
    assert outcome.model_view["source_start_at"] is None
    assert outcome.model_view["source_end_at"] is None


def test_single_source_instant_and_offset_are_preserved():
    # Inside the authorized scope window, in a non-UTC offset: the offset is
    # what this test is about. It used to read ``NOW``, which sits past
    # WINDOW_END and is now refused as out-of-scope source data (see
    # test_source_timestamps_outside_the_authorized_scope_are_refused); that
    # value was incidental to preserving the offset.
    instant = (WINDOW_START + timedelta(minutes=30)).astimezone(
        timezone(timedelta(hours=8))
    )
    executor, transport, _, _ = build()
    transport.response = TransportResponse(
        body=body([{"value": 1}]), source_start_at=instant, source_end_at=instant
    )
    outcome = executor.execute(request())
    assert outcome.status == "ok"
    assert outcome.model_view["source_start_at"] == instant.isoformat()
    assert outcome.model_view["source_end_at"] == instant.isoformat()
    assert outcome.evidence.view_sha256 == canonical_hash(outcome.model_view)


def test_source_timestamps_outside_the_authorized_scope_are_refused():
    """Bot review finding: the reported interval was checked only for type and
    ordering, so an adapter reporting source timestamps outside the Run's
    authorized window still had its rows adopted and shown to the model.

    ``TransportResponse``'s own contract says these bounds are "the actual
    source timestamps represented by this response", so a response claiming
    data from outside the authorization is out-of-scope data whose violation
    is detectable from trusted metadata -- fail closed. The bound compared is
    the *scope* window, not the narrower requested one: a source may cover a
    different interval inside the authorization, never outside it.
    """
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(
        body=body([{"value": 1}]),
        source_start_at=WINDOW_START - timedelta(minutes=1),
        source_end_at=WINDOW_END,
    )
    outcome = executor.execute(request())
    assert (outcome.status, outcome.reason) == ("denied", "WINDOW_OUT_OF_SCOPE")
    assert outcome.source_contact == "confirmed"  # the source really was read
    assert outcome.model_view["content"] is None  # the rows never reach the model
    assert sink.records == []  # refused before evidence registration, as a
    # malformed interval already is


def test_a_source_interval_inside_the_scope_but_wider_than_requested_is_kept():
    """The scope window is the authorization boundary; the requested window is
    not. A source whose covered interval differs from the request but stays
    inside the scope (bucket alignment) must still be adopted.
    """
    executor, transport, _, _ = build()
    transport.response = TransportResponse(
        body=body([{"value": 1}]),
        source_start_at=WINDOW_START,
        source_end_at=WINDOW_END,
    )
    outcome = executor.execute(request())
    assert outcome.status == "ok"


def test_two_dispatches_of_one_operation_get_distinct_evidence_ids():
    """Bot review finding: the per-dispatch charging protocol permits the same
    stable operation to be dispatched twice (a retry, a duplicate delivery),
    and those responses can carry different bytes and observation times. With
    one shared ``evidence_id`` a sink keyed by it would drop or overwrite one
    observation and return the surviving reference, which ``_register()``
    accepts as proof the new record was committed -- the model view would then
    describe bytes its own evidence link does not resolve to.
    """
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(body=body([{"value": 1}]))
    first = executor.execute(request())
    transport.response = TransportResponse(body=body([{"value": 2}]))
    second = executor.execute(request())  # same step_id and tool_index

    assert first.operation.operation_id == second.operation.operation_id
    assert first.evidence.evidence_id != second.evidence.evidence_id
    assert len(sink.records) == 2
    assert {record.evidence_id for record in sink.records} == {
        first.evidence.evidence_id,
        second.evidence.evidence_id,
    }
    # Each view resolves to its own bytes.
    assert first.model_view["content"] == [{"value": 1}]
    assert second.model_view["content"] == [{"value": 2}]


def test_a_future_data_as_of_is_refused_before_adoption():
    """Bot review finding: an aware `data_as_of` later than the moment the read
    completed was accepted, `_record()` computed a negative
    `freshness_seconds`, and the rows were committed and shown to the model --
    impossible future-dated metadata reading as unusually fresh.
    """
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(
        body=body([{"value": 1}]), data_as_of=NOW + timedelta(hours=1)
    )

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")
    assert outcome.model_view["content"] is None
    assert sink.records == []


def test_a_future_source_interval_is_refused_before_adoption():
    """The same defect one field over, found by checking the class rather than
    only the reported field: a source interval claiming rows that cannot exist
    yet was adopted whenever the scope window reached into the future.
    """
    scope_window = Window(NOW - timedelta(minutes=5), NOW + timedelta(minutes=30))
    executor, transport, sink, _ = build(scope_overrides={"window": scope_window})
    transport.response = TransportResponse(
        body=body([{"value": 1}]),
        source_start_at=NOW + timedelta(minutes=10),
        source_end_at=NOW + timedelta(minutes=10),
    )

    outcome = executor.execute(
        request(
            window={
                "start": (NOW - timedelta(minutes=5)).isoformat(),
                "end": (NOW + timedelta(minutes=5)).isoformat(),
            }
        )
    )

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")
    assert sink.records == []


def test_a_data_as_of_equal_to_the_read_instant_is_still_valid():
    """The boundary stays open: zero freshness is a real, ordinary reading."""
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([{"value": 1}]), data_as_of=NOW)

    outcome = executor.execute(request())

    assert outcome.status == "ok"
    assert outcome.evidence.freshness_seconds == 0.0
