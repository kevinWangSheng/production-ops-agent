"""#666 PR-B contract: evaluation-side time shift of fixed-time questions
(H-8, R10-1, PR-A review N1/N2/N4) and its hand-off to PR-A's deshift (H-9).

Independent contract tests written from the task record's PR-B public
interface (``shift.py``) and the pre-review §1 table, without reading the
implementation. Collection stays green before the module exists.

Rules under test:

* only the 18 questions marked fixed in pre-review §1 are shifted; others
  come back untouched with ``applied=False, reason=RELATIVE_TIME``;
* only ``date``/``date_nanos`` mapped fields are rewritten (nested paths
  included, multi-fields excluded); keyword/text fields holding time text and
  indices without date fields are untouched; ``_id`` is kept;
* the offset is whole minutes, rounded down, with the anchor (latest data
  time) landing at or before planned delivery − 60 s; non-positive offsets
  raise ``SHIFT_NOT_NEEDED``; a shifted range ± 1 h overlapping the original
  raises ``SHIFT_OVERLAP`` before anything is written;
* the question text is rewritten with the same offset, format preserved;
  time-only phrases are not rewritten but counted;
* ``ShiftRecord.as_text_shift()`` feeds PR-A ``deshift``, and shifted then
  deshifted text equals the original.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``EsAdmin.mappings(pattern)`` returns a mapping keyed by index name whose
  values ``date_fields`` accepts; ``date_fields`` is tested only through
  that chain, so the tests do not fix the value's inner shape.
* ``EsAdmin.bulk_replace(index, docs)`` takes the same ``(id, source)``
  pairs ``EsAdmin.documents(index)`` returns.
* ``ShiftRecord`` datetimes (``anchor``, ``original_min/max``) may be
  ``datetime`` or ISO strings; both are compared as instants.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.holmes_evals_b_support import (
    ENGINEER,
    Es,
    new_store,
    no_proxy_env,  # noqa: F401 - fixture
    served,
)
from tests.holmes_evals_support import mod

pytestmark = pytest.mark.usefixtures("no_proxy_env")

FIXED_PREFIXES = (
    "183b", "183c", "183d", "183e", "184", "185", "187", "188", "189", "190",
    "191", "193", "245", "275", "276", "277", "278", "280",
)  # fmt: skip
# #809 (B4-12, PR #824 ruling): 184 (30 d + LEAD from delivery) and 235 (a
# 45-day-old batch) exceed the 30-day lookback.
TIME_BOX_PREFIXES = ("184", "235")
NOW = datetime(2026, 11, 18, 10, 0, 30, tzinfo=UTC)


def _qid(prefix: str) -> str:
    names = [q for q in mod("questions").B1_QUESTIONS if q.split("_", 1)[0] == prefix]
    assert len(names) == 1, prefix
    return names[0]


def _instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _expected_seconds(anchor: datetime, now: datetime = NOW) -> int:
    raw = (now - timedelta(seconds=60) - anchor).total_seconds()
    return math.floor(raw / 60) * 60


def _code(error: BaseException) -> str | None:
    return getattr(error, "code", None)


# -- constants ------------------------------------------------------------------


def test_fixed_time_set_is_the_18_fixed_questions() -> None:
    shift = mod("shift")
    assert shift.FIXED_TIME == frozenset(_qid(p) for p in FIXED_PREFIXES)
    assert len(shift.FIXED_TIME) == 18
    assert shift.FIXED_TIME <= set(mod("questions").B1_QUESTIONS)


def test_time_box_and_utc_date_sets() -> None:
    shift = mod("shift")
    assert shift.TIME_BOX_LIMITED == frozenset(_qid(p) for p in TIME_BOX_PREFIXES)
    assert set(shift.UTC_DATE_SENSITIVE) == {"280_es_multi_quirk"}
    assert shift.LEAD == timedelta(seconds=60)


@pytest.mark.parametrize(
    ("qid", "pattern"),
    [
        ("275_es_field_name_correction", "app-275-*"),
        ("183b_elasticsearch_index_discovery", "app-183b-*"),
        ("259_wrong_cluster_logs_confusion", "app-259-*"),
    ],
)
def test_index_pattern(qid: str, pattern: str) -> None:
    assert mod("shift").index_pattern(qid) == pattern


# -- offset ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "anchor",
    [
        datetime(2026, 5, 14, 12, 2, 15, tzinfo=UTC),
        datetime(2024, 1, 15, 23, 0, 0, tzinfo=UTC),
        datetime(2025, 1, 1, 10, 5, 59, 999000, tzinfo=UTC),
    ],
)
def test_plan_shift_seconds_whole_minutes_rounded_down(anchor: datetime) -> None:
    seconds = mod("shift").plan_shift_seconds(anchor, NOW)
    assert isinstance(seconds, int)
    assert seconds % 60 == 0
    assert seconds == _expected_seconds(anchor)
    assert anchor + timedelta(seconds=seconds) <= NOW - timedelta(seconds=60)
    assert NOW - timedelta(seconds=60) - (anchor + timedelta(seconds=seconds)) < (
        timedelta(seconds=60)
    )


@pytest.mark.parametrize(
    "anchor",
    [
        NOW - timedelta(seconds=90),  # rounds down to 0
        NOW - timedelta(seconds=60),  # exactly 0
        NOW,  # negative
        NOW + timedelta(days=1),
    ],
)
def test_plan_shift_seconds_non_positive_raises(anchor: datetime) -> None:
    shift = mod("shift")
    with pytest.raises(shift.ShiftError) as caught:
        shift.plan_shift_seconds(anchor, NOW)
    assert _code(caught.value) == "SHIFT_NOT_NEEDED"


def test_check_no_overlap() -> None:
    shift = mod("shift")
    lo = datetime(2026, 5, 14, 10, 0, tzinfo=UTC)
    hi = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    # Shifted range ± 1 h = [lo + 1 h, hi + 3 h]: overlaps [lo, hi].
    with pytest.raises(shift.ShiftError) as caught:
        shift.check_no_overlap(lo, hi, 2 * 3600)
    assert _code(caught.value) == "SHIFT_OVERLAP"
    # Span 2 h, margin 1 h: 3 h is the edge; 3 h 1 min is clear.
    shift.check_no_overlap(lo, hi, 3 * 3600 + 60)
    shift.check_no_overlap(lo, hi, 200 * 86400)


# -- values and documents -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "seconds", "expected"),
    [
        ("2026-05-14T12:00:05Z", 3600, "2026-05-14T13:00:05Z"),
        ("2025-01-01T10:00:00.123Z", 60, "2025-01-01T10:01:00.123Z"),
        ("2024-01-15T10:00:00.123456Z", 120, "2024-01-15T10:02:00.123456Z"),
        ("2024-01-15T10:00:00+02:00", 3600, "2024-01-15T11:00:00+02:00"),
        ("2025-01-01T10:00:00", 3600, "2025-01-01T11:00:00"),
        ("2024-01-15T23:30:00Z", 3600, "2024-01-16T00:30:00Z"),
        ("2024-01-31T23:59:00Z", 86400 * 30 + 60, "2024-03-02T00:00:00Z"),
    ],
)
def test_shift_value_keeps_format(value: str, seconds: int, expected: str) -> None:
    assert mod("shift").shift_value(value, seconds) == expected


def test_shift_value_epoch_millis_stays_a_number() -> None:
    out = mod("shift").shift_value(1715688005000, 60)
    assert out == 1715688065000
    assert isinstance(out, int) and not isinstance(out, bool)


@pytest.mark.parametrize("value", ["not a date", "2024-13-45T99:00:00Z", {"a": 1}])
def test_shift_value_unparseable_is_none(value: Any) -> None:
    assert mod("shift").shift_value(value, 60) is None


def test_shift_document_only_listed_date_fields() -> None:
    source = {
        "@timestamp": "2026-05-14T12:00:05Z",
        "event": {"created": "2026-05-14T12:00:06.5Z", "name": "x"},
        "message": "at 2026-05-14T12:00:05Z the DB refused",
        "ingest_ts": "2026-05-14T12:00:05Z",
        "bad": "nope",
    }
    frozen = {**source, "event": dict(source["event"])}
    doc, rewritten, unparsed = mod("shift").shift_document(
        source, ["@timestamp", "event.created", "bad", "absent"], 600
    )
    assert doc == {
        "@timestamp": "2026-05-14T12:10:05Z",
        "event": {"created": "2026-05-14T12:10:06.5Z", "name": "x"},
        "message": "at 2026-05-14T12:00:05Z the DB refused",
        "ingest_ts": "2026-05-14T12:00:05Z",
        "bad": "nope",
    }
    assert (rewritten, unparsed) == (2, 1)
    assert source == frozen  # a new document; the input is not mutated


# -- question text ------------------------------------------------------------------

_S = 100 * 86400 + 5 * 3600 + 7 * 60


def _fmt(moment: datetime, layout: str) -> str:
    return moment.strftime(layout)


def test_rewrite_question_same_offset_formats_kept() -> None:
    shift = mod("shift")
    a = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    b = datetime(2026, 5, 14, 12, 3, 0, tzinfo=UTC)
    text = (
        "Errors between 2026-05-14T12:00:00Z and 2026-05-14T12:03:00.250Z "
        "(window 2026-05-14 12:00 to 12:30) on 2026-05-14, "
        "local 2026-05-14T14:00:00+02:00. Index app-185-geo-x-2024.01.05 stays."
    )
    d = timedelta(seconds=_S)
    local = datetime(2026, 5, 14, 14, 0, 0) + d
    expected = (
        f"Errors between {_fmt(a + d, '%Y-%m-%dT%H:%M:%SZ')} and "
        f"{_fmt(b + d, '%Y-%m-%dT%H:%M:%S')}.250Z "
        f"(window {_fmt(a + d, '%Y-%m-%d %H:%M')} to 12:30) on "
        f"{(datetime(2026, 5, 14, 12, tzinfo=UTC) + d).date().isoformat()}, "
        f"local {_fmt(local, '%Y-%m-%dT%H:%M:%S')}+02:00. "
        "Index app-185-geo-x-2024.01.05 stays."
    )
    out, rewrites = shift.rewrite_question(text, _S)
    assert out == expected
    assert len(rewrites) == 5
    originals = [r.original for r in rewrites]
    assert "2026-05-14T12:00:00Z" in originals
    assert "2026-05-14" in originals
    for r in rewrites:
        assert r.rewritten in out
        assert r.instant.tzinfo is not None
        assert r.instant.utcoffset() == timedelta(0)
    first = next(r for r in rewrites if r.original == "2026-05-14T12:00:00Z")
    assert first.instant == a + d
    offset = next(r for r in rewrites if r.original == "2026-05-14T14:00:00+02:00")
    assert offset.instant == a + d


def test_time_only_phrases_counted_not_rewritten() -> None:
    shift = mod("shift")
    text = "The checkout stopped at 14:00 and came back at 15:30 on 2026-05-14."
    out, rewrites = shift.rewrite_question(text, 3600)
    assert "at 14:00 and came back at 15:30" in out
    assert len(rewrites) == 1
    assert shift.time_only_phrases(text) == 2
    assert shift.time_only_phrases("Between 2026-05-14T12:00:00Z and now") == 0


def test_rewritten_question_deshifts_back_exactly() -> None:
    shift, text_mod = mod("shift"), mod("text")
    original = (
        "Logs from 2026-05-14T12:00:01Z to 2026-05-14T12:02:15Z, peak at "
        "2026-05-14T12:01:00.500Z and 2026-05-14T14:01:30+02:00."
    )
    seconds = 188 * 86400 + 21 * 3600 + 57 * 60
    out, rewrites = shift.rewrite_question(original, seconds)
    assert out != original
    instants = [r.instant for r in rewrites]
    back, count = text_mod.deshift(
        out, text_mod.Shift(seconds, min(instants), max(instants))
    )
    assert back == original
    assert count == len(rewrites) == 4


def test_minute_precision_round_trips_with_whole_minute_offset() -> None:
    shift, text_mod = mod("shift"), mod("text")
    original = "Window 2026-05-14 12:00 to 2026-05-14 12:45 only."
    seconds = 3 * 86400 + 7 * 60
    out, rewrites = shift.rewrite_question(original, seconds)
    instants = [r.instant for r in rewrites]
    back, _ = text_mod.deshift(
        out, text_mod.Shift(seconds, min(instants), max(instants))
    )
    assert back == original


# -- EsAdmin and apply_shift against the fake ---------------------------------------


QUESTION_275 = (
    "Between 2026-05-14T12:00:00Z and 2026-05-14T12:03:00Z checkout-275 logged "
    "errors; the service stopped at 12:02. How many ERROR lines?"
)


def _seed_275(es: Es) -> dict[str, dict[str, Any]]:
    es.create(
        "app-275-logs-x",
        {
            "@timestamp": {"type": "date"},
            "severity": {"type": "keyword"},
            "message": {"type": "text"},
            "ingest": {"type": "keyword"},
        },
    )
    docs = [
        {"@timestamp": "2026-05-14T12:00:01Z", "severity": "ERROR",
         "message": "refused at 2026-05-14T12:00:01Z", "ingest": "2026-05-14T12:00:01Z"},
        {"@timestamp": "2026-05-14T12:01:30.250Z", "severity": "WARN",
         "message": "slow", "ingest": "2026-05-14T12:01:30Z"},
        {"@timestamp": "2026-05-14T12:02:15Z", "severity": "INFO",
         "message": "ok", "ingest": "2026-05-14T12:02:15Z"},
    ]  # fmt: skip
    es.bulk("app-275-logs-x", docs)
    es.create("app-275-meta", {"note": {"type": "keyword"}})
    es.bulk("app-275-meta", [{"note": "2026-05-14T12:00:00Z"}])
    es.create("app-276-logs", {"@timestamp": {"type": "date"}})
    es.bulk("app-276-logs", [{"@timestamp": "2026-06-03T08:00:00Z"}])
    return {
        "x": es.sources("app-275-logs-x"),
        "meta": es.sources("app-275-meta"),
        "276": es.sources("app-276-logs"),
    }


def test_es_admin_reads_and_rewrites_by_id() -> None:
    shift = mod("shift")
    store = new_store()
    es = Es(store)
    before = _seed_275(es)
    with served(store) as url:
        admin = shift.EsAdmin(url, ENGINEER)
        assert sorted(admin.indices("app-275-*")) == ["app-275-logs-x", "app-275-meta"]
        mappings = admin.mappings("app-275-*")
        assert shift.date_fields(mappings["app-275-logs-x"]) == ["@timestamp"]
        assert shift.date_fields(mappings["app-275-meta"]) == []
        docs = admin.documents("app-275-logs-x")
        assert {doc_id: src for doc_id, src in docs} == before["x"]
        changed = [(doc_id, {**src, "severity": "X"}) for doc_id, src in docs]
        admin.bulk_replace("app-275-logs-x", changed)
        admin.forcemerge("app-275-logs-x")
        after = es.sources("app-275-logs-x")
        assert set(after) == set(before["x"])
        assert all(src["severity"] == "X" for src in after.values())
        admin.delete("app-275-*")
        assert sorted(admin.indices("app-275-*")) == []
        assert sorted(admin.indices("app-276-*")) == ["app-276-logs"]


def test_date_fields_nested_and_not_multi_fields() -> None:
    shift = mod("shift")
    store = new_store()
    es = Es(store)
    es.create(
        "app-188-index-a",
        {
            "@timestamp": {"type": "date"},
            "event": {
                "properties": {
                    "created": {"type": "date_nanos"},
                    "name": {"type": "keyword"},
                    "inner": {"properties": {"at": {"type": "date"}}},
                }
            },
            "ingest_ts": {
                "type": "keyword",
                "fields": {"as_date": {"type": "date"}},
            },
            "msg": {"type": "text"},
        },
    )
    with served(store) as url:
        mapping = shift.EsAdmin(url, ENGINEER).mappings("app-188-*")["app-188-index-a"]
    assert sorted(shift.date_fields(mapping)) == [
        "@timestamp",
        "event.created",
        "event.inner.at",
    ]


def test_es_admin_transport_failure_is_shift_io_failed() -> None:
    import socket

    shift = mod("shift")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    admin = shift.EsAdmin(f"http://127.0.0.1:{port}", ENGINEER)
    with pytest.raises(shift.ShiftError) as caught:
        admin.indices("app-275-*")
    assert _code(caught.value) == "SHIFT_IO_FAILED"
    assert ENGINEER not in str(caught.value)


def test_es_admin_refuses_oversized_index() -> None:
    shift = mod("shift")
    store = new_store()
    es = Es(store)
    es.create("app-183c-logs", {"@timestamp": {"type": "date"}})
    es.bulk(
        "app-183c-logs",
        [{"@timestamp": "2025-01-01T00:00:00Z"} for _ in range(10001)],
        ids=False,
    )
    with served(store) as url:
        admin = shift.EsAdmin(url, ENGINEER)
        with pytest.raises(shift.ShiftError) as caught:
            admin.documents("app-183c-logs")
        assert _code(caught.value) == "SHIFT_INDEX_TOO_LARGE"
        with pytest.raises(shift.ShiftError) as caught:
            shift.apply_shift(admin, "183c_elasticsearch_log_search", "Q?", NOW)
        assert _code(caught.value) == "SHIFT_INDEX_TOO_LARGE"


def test_apply_shift_fixed_question_end_to_end() -> None:
    shift, text_mod = mod("shift"), mod("text")
    store = new_store()
    es = Es(store)
    before = _seed_275(es)
    anchor = datetime(2026, 5, 14, 12, 2, 15, tzinfo=UTC)
    seconds = _expected_seconds(anchor)
    d = timedelta(seconds=seconds)
    with served(store) as url:
        admin = shift.EsAdmin(url, ENGINEER)
        question, record = shift.apply_shift(
            admin, "275_es_field_name_correction", QUESTION_275, NOW
        )

    # The record.
    assert record.applied is True
    assert record.reason is None
    assert record.seconds == seconds and seconds % 60 == 0
    assert _instant(record.anchor) == anchor
    assert _instant(record.original_min) == datetime(2026, 5, 14, 12, 0, 1, tzinfo=UTC)
    assert _instant(record.original_max) == anchor
    rows = {row["index"]: row for row in record.indices}
    assert rows["app-275-logs-x"]["fields"] == ["@timestamp"]
    assert rows["app-275-logs-x"]["documents"] == 3
    assert rows["app-275-logs-x"]["rewritten"] == 3
    assert rows["app-275-logs-x"]["unparsed"] == 0
    assert "app-276-logs" not in rows
    if "app-275-meta" in rows:
        assert rows["app-275-meta"]["rewritten"] == 0
    assert "app-275-logs-x" in record.store_size_before
    assert "app-275-logs-x" in record.store_size_after
    assert all(isinstance(v, int) for v in record.store_size_after.values())
    assert record.time_only_phrases == 1  # "at 12:02"
    assert len(record.question_rewrites) == 2

    # The data: only the date field moved, by the same offset, ids kept.
    after = es.sources("app-275-logs-x")
    assert set(after) == set(before["x"])
    for doc_id, src in after.items():
        old = before["x"][doc_id]
        assert _instant(src["@timestamp"]) == _instant(old["@timestamp"]) + d
        assert src["message"] == old["message"]
        assert src["ingest"] == old["ingest"]
        assert src["severity"] == old["severity"]
    assert after[next(k for k, v in before["x"].items()
                      if v["severity"] == "WARN")]["@timestamp"].endswith(".250Z")  # fmt: skip
    assert es.sources("app-275-meta") == before["meta"]
    assert es.sources("app-276-logs") == before["276"]
    newest = max(_instant(s["@timestamp"]) for s in after.values())
    assert newest <= NOW - timedelta(seconds=60)

    # The question: same offset, time-only phrase untouched.
    a = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC) + d
    b = datetime(2026, 5, 14, 12, 3, 0, tzinfo=UTC) + d
    assert question == QUESTION_275.replace(
        "2026-05-14T12:00:00Z", a.strftime("%Y-%m-%dT%H:%M:%SZ")
    ).replace("2026-05-14T12:03:00Z", b.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert "stopped at 12:02" in question

    # Hand-off to PR-A: shifted range is the union of data and question times.
    text_shift = record.as_text_shift()
    assert isinstance(text_shift, text_mod.Shift)
    assert text_shift.seconds == seconds
    assert _instant(text_shift.shifted_min) == a
    assert _instant(text_shift.shifted_max) == b
    assert _instant(record.shifted_min) == a
    assert _instant(record.shifted_max) == b
    back, count = text_mod.deshift(question, text_shift)
    assert back == QUESTION_275
    assert count == 2


def test_apply_shift_relative_question_untouched() -> None:
    shift = mod("shift")
    store = new_store()
    es = Es(store)
    es.create("app-254-prod-logs", {"timestamp": {"type": "date"}})
    es.bulk("app-254-prod-logs", [{"timestamp": "2026-11-18T08:00:00Z"}])
    before = es.sources("app-254-prod-logs")
    q = "Did the DR test on 2026-11-18T08:00:00Z log errors?"
    with served(store) as url:
        out, record = shift.apply_shift(
            shift.EsAdmin(url, ENGINEER), "254_elasticsearch_dr_test_log_check", q, NOW
        )
    assert out == q
    assert (record.applied, record.reason) == (False, "RELATIVE_TIME")
    assert record.as_text_shift() is None
    assert es.sources("app-254-prod-logs") == before


def test_apply_shift_fixed_question_without_date_fields_is_skipped() -> None:
    shift = mod("shift")
    store = new_store()
    es = Es(store)
    es.create(
        "app-277-logs-y", {"ingest_ts": {"type": "keyword"}, "m": {"type": "text"}}
    )
    es.bulk("app-277-logs-y", [{"ingest_ts": "2026-06-02T10:00:00Z", "m": "x"}])
    before = es.sources("app-277-logs-y")
    q = "What was ingested on 2026-06-02 and 2026-06-03T09:00:00Z?"
    with served(store) as url:
        out, record = shift.apply_shift(
            shift.EsAdmin(url, ENGINEER), "277_es_timestamp_keyword_field", q, NOW
        )
    assert out == q  # no data shift, so the question keeps its dates
    assert (record.applied, record.reason) == (False, "NO_DATE_FIELDS")
    assert record.as_text_shift() is None
    assert es.sources("app-277-logs-y") == before


def test_apply_shift_date_field_without_values_is_no_dated_documents() -> None:
    shift = mod("shift")
    store = new_store()
    es = Es(store)
    es.create("app-278-events-z", {"ts": {"type": "date"}, "kind": {"type": "keyword"}})
    es.bulk("app-278-events-z", [{"kind": "a"}, {"kind": "b"}])
    q = "Events on 2026-05-20T10:00:00Z?"
    with served(store) as url:
        out, record = shift.apply_shift(
            shift.EsAdmin(url, ENGINEER), "278_es_schema_discovery", q, NOW
        )
    assert out == q
    assert (record.applied, record.reason) == (False, "NO_DATED_DOCUMENTS")
    assert record.as_text_shift() is None


def test_apply_shift_overlap_raises_before_writing() -> None:
    shift = mod("shift")
    store = new_store()
    es = Es(store)
    es.create("app-185-geo-us-a", {"@timestamp": {"type": "date"}})
    old = (NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    es.bulk("app-185-geo-us-a", [{"@timestamp": old}, {"@timestamp": recent}])
    before = es.sources("app-185-geo-us-a")
    with served(store) as url:
        with pytest.raises(shift.ShiftError) as caught:
            shift.apply_shift(
                shift.EsAdmin(url, ENGINEER),
                "185_elasticsearch_cross_region_search",
                "Q?",
                NOW,
            )
    assert _code(caught.value) == "SHIFT_OVERLAP"
    assert es.sources("app-185-geo-us-a") == before


def test_apply_shift_anchor_is_max_over_all_indices_and_fields() -> None:
    shift = mod("shift")
    store = new_store()
    es = Es(store)
    es.create("app-190-a", {"@timestamp": {"type": "date"}})
    es.bulk("app-190-a", [{"@timestamp": "2024-01-15T10:00:00Z"}])
    es.create("app-190-b", {"@timestamp": {"type": "date"}, "end": {"type": "date"}})
    es.bulk(
        "app-190-b",
        [{"@timestamp": "2024-01-15T10:10:00Z", "end": "2024-01-15T10:24:30Z"}],
    )
    with served(store) as url:
        _, record = shift.apply_shift(
            shift.EsAdmin(url, ENGINEER),
            "190_elasticsearch_cross_service_correlation",
            "Q?",
            NOW,
        )
    anchor = datetime(2024, 1, 15, 10, 24, 30, tzinfo=UTC)
    assert _instant(record.anchor) == anchor
    assert record.seconds == _expected_seconds(anchor)
    assert _instant(record.original_min) == datetime(2024, 1, 15, 10, 0, tzinfo=UTC)
    shifted = es.sources("app-190-b")
    (src,) = shifted.values()
    assert _instant(src["end"]) == anchor + timedelta(seconds=record.seconds)
