"""Evaluation-side time shift for fixed-time questions (#666 H-8, R10-1).

The product's Elasticsearch search always filters a mapped date field to the
authorised 24-hour box (B1-U1), so a question whose data was written at a
fixed historical time (2024-01, 2025-01, 2026-05/06) would find nothing. The
evaluation moves that data instead of loosening the tool (#666 comment):

1. after ``before_test``, read the mappings of the question's indices
   (``app-<id>-*``) with the engineer key and take every ``date`` /
   ``date_nanos`` field; an index without one is left alone (277, 280's
   index C);
2. the anchor is the latest value of those fields across the question; the
   offset is ``now - 60 s - anchor`` rounded *down* to whole minutes (PR-A
   review N2: minute-precision times then shift back exactly), so the shifted
   data ends at least a minute before delivery;
3. the offset must be large enough that the shifted range widened by an hour
   never overlaps the original range (N4) -- otherwise the deshift could not
   tell evaluation times from original ones;
4. documents are read, only their date fields rewritten in their own format,
   written back under the same ``_id`` -- documents are read in few
   multi-index searches and written in chunked ``_bulk`` requests, then one
   ``_refresh`` and one ``_forcemerge`` over ``app-<id>-*`` (184/185 hold
   about 1600 indices; 187 asks about storage, and the rewrite would
   otherwise leave deleted documents behind);
5. R10-1: the question text's ISO date-times and ``YYYY-MM-DD`` dates move by
   the same offset. A date alone becomes the date of "that day 12:00 UTC +
   offset" -- under a non-whole-day offset it can land a day off, the same
   ambiguity as the deshift's date midpoint (N1, disclosed). A time of day
   without a date is not rewritten; ``time_only_phrases`` counts them for the
   disclosure.

The rule reads data, mappings and the question text only -- never an answer.

C5 Loki questions (#666 PR-C, R10-1, C5-U2 a): 100a, 101 and 259_loki carry a
2025-08 timeline whose incident sits two days before the data's end, so the
anchor is the question's own time, not the data's latest value. The offset
is a whole number of days (lead, 2026-11-19), so a time of day written alone
needs no deshift: the incident hour must end at least ``LEAD`` before now and
still lie wholly inside the 24-hour authorised box at delivery; when no day
fits the attempt waits (:func:`seconds_to_wait_for_shift_day`). The entries
older than ``FIXED_ENTRY_AGE`` before ``before_test`` began, within
``LOKI_ANCHOR_READ`` of the anchor, are read back from the question's own Loki, moved by the offset (entry time and ISO times inside
the line), entries that would land after ``now - LEAD`` are dropped, and the
rest are pushed back under their labels. Loki cannot rewrite in place: the
2025 originals stay, outside the box (disclosed).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from scripts.holmes_evals.text import (
    _MONTH_DATE,
    _STAMP,
    DESHIFT_MARGIN,
    Shift,
    format_month_date,
    month_date_parts,
)

#: §1 of the prereview: questions whose data sits at a fixed historical time.
FIXED_TIME: frozenset[str] = frozenset(
    {
        "183b_elasticsearch_index_discovery",
        "183c_elasticsearch_log_search",
        "183d_elasticsearch_aggregation",
        "183e_elasticsearch_field_mappings",
        "184_elasticsearch_index_explosion",
        "185_elasticsearch_cross_region_search",
        "187_elasticsearch_disk_space",
        "188_elasticsearch_mapping_explosion",
        "189_elasticsearch_timeseries_gap",
        "190_elasticsearch_cross_service_correlation",
        "191_elasticsearch_query_profile",
        "193_elasticsearch_large_mapping_search",
        "245_elasticsearch_trace_large_fields",
        "275_es_field_name_correction",
        "276_es_numeric_severity_field",
        "277_es_timestamp_keyword_field",
        "278_es_schema_discovery",
        "280_es_multi_quirk",
    }
)
#: H-19: decided before the run from data span and anchor only.
TIME_BOX_LIMITED: frozenset[str] = frozenset(
    {
        "184_elasticsearch_index_explosion",
        "185_elasticsearch_cross_region_search",
        "189_elasticsearch_timeseries_gap",
        "235_elasticsearch_cluster_mismatch",
        # 2026-11-20 rental: data lies outside the fixed 24 h ES window (A3-1).
        "280_es_multi_quirk",
    }
)
#: H-24: index C is written with the current UTC date.
UTC_DATE_SENSITIVE: frozenset[str] = frozenset({"280_es_multi_quirk"})
LEAD = timedelta(seconds=60)
MAX_DOCUMENTS = 10_000
#: Index names per request path, in characters (HTTP lines stay under 4 KiB).
URL_NAMES_CHARS = 3000
#: One ``_bulk`` request: at most this many documents or bytes.
BULK_DOCS = 5000
BULK_BYTES = 5 * 1024 * 1024
ES_TIMEOUT_S = 60
#: ``_forcemerge`` over ``app-<id>-*`` can touch ~1600 indices in one request.
FORCEMERGE_TIMEOUT_S = 600
_DATE_TYPES = frozenset({"date", "date_nanos"})

#: C5 (PR-C): Loki questions with a fixed 2025-08 timeline.
LOKI_FIXED_TIME: frozenset[str] = frozenset(
    {
        "100a_loki_historical_logs",
        "101_loki_historical_logs_pod_deleted",
        "259_loki_historical_logs_pod_deleted_docker",
    }
)
#: The question time of each (``question_instants`` of its ``user_prompt``,
#: checked by a unit test); :func:`seconds_to_wait_for_shift_day` needs it
#: before the question is loaded into an attempt.
LOKI_ANCHORS: dict[str, datetime] = {
    qid: datetime(2025, 8, 2, 13, 45, tzinfo=UTC) for qid in LOKI_FIXED_TIME
}
#: The incident hour the question text points at ("around 13:45 UTC").
INCIDENT_SPAN = timedelta(hours=1)
#: The product's authorised box: the 24 hours before submission.
BOX = timedelta(hours=24)
#: Delivery may come up to the driver's 120 s void line after the shift.
DELIVERY_ALLOWANCE = timedelta(seconds=120)
BOX_MARGIN = timedelta(minutes=10)
SHIFT_DAY_WAIT_CAP = timedelta(hours=2)
#: Loki entries older than this before ``before_test`` began are fixed-time data.
FIXED_ENTRY_AGE = timedelta(hours=24)
#: The fixed-time read is ``[anchor - this, anchor + this)``. Loki's query
#: frontend splits every request into 30-minute sub-queries, about 0.1 s each
#: under the fixtures' 50m CPU limit, so a read's cost grows with its span, not
#: its data (a 730-day read is about an hour). The three questions' generators
#: write 2025-08-01 00:00 to 2025-08-04 14:00, i.e. anchor -38 h to +49 h.
LOKI_ANCHOR_READ = timedelta(days=3)
#: Fixed entries this close to the read's edge may continue past it:
#: ``SHIFT_DATA_AT_READ_EDGE`` (fail closed instead of shifting part of them).
LOKI_EDGE_GUARD = timedelta(hours=12)
#: One query_range request spans at most this (about 5 s on the fixtures'
#: Loki, well under ``LOKI_TIMEOUT_S``).
LOKI_QUERY_SPAN = timedelta(days=1)
#: One labels/series request spans at most this; Loki 2.9's default
#: ``max_query_length`` (30d1h) rejects longer ones with HTTP 400.
LOKI_SERIES_SPAN = timedelta(days=30)
LOKI_PAGE = 5000
LOKI_MAX_ENTRIES = 200_000
LOKI_PUSH_ENTRIES = 5000
LOKI_PUSH_BYTES = 4 * 1024 * 1024
LOKI_TIMEOUT_S = 60
#: ``{"streams": [...]}`` around the streams of one push request.
_PUSH_ENVELOPE = 64
DAY = timedelta(days=1)


class ShiftError(Exception):
    """A fixed code; never carries a credential or a response body."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def question_number(qid: str) -> str:
    """``275_es_field_name_correction`` -> ``275``; ``183b_...`` -> ``183b``."""
    head = qid.split("_", 1)[0]
    if not head or not re.fullmatch(r"[0-9]+[a-z]?", head):
        raise ValueError("QUESTION_ID_INVALID")
    return head


def index_pattern(qid: str) -> str:
    return f"app-{question_number(qid)}-*"


def date_fields(mapping: Mapping[str, Any]) -> list[str]:
    """Dotted paths of the ``date``/``date_nanos`` fields of one index's
    mapping (``{"properties": ...}`` or ``{"mappings": {"properties": ...}}``);
    multi-fields are not included."""
    if "mappings" in mapping and isinstance(mapping["mappings"], Mapping):
        mapping = mapping["mappings"]
    props = mapping.get("properties")
    out: list[str] = []

    def walk(properties: Any, prefix: str) -> None:
        if not isinstance(properties, Mapping):
            return
        for name, spec in properties.items():
            if not isinstance(spec, Mapping):
                continue
            path = f"{prefix}{name}"
            if spec.get("type") in _DATE_TYPES:
                out.append(path)
            walk(spec.get("properties"), path + ".")

    walk(props, "")
    return sorted(out)


# -- values ---------------------------------------------------------------------------

_VALUE = re.compile(
    r"^(?P<date>\d{4})(?P<d1>[-/])(?P<mo>\d{2})(?P=d1)(?P<d>\d{2})"
    r"(?:(?P<sep>[Tt ])(?P<h>\d{2}):(?P<mi>\d{2})"
    r"(?::(?P<s>\d{2})(?:(?P<fsep>[.,])(?P<frac>\d{1,9}))?)?"
    r"(?P<zone>[Zz]|[+-]\d{2}(?::?\d{2})?)?)?$"
)


def _zone(text: str | None) -> timezone | None:
    if not text:
        return None
    if text in ("Z", "z"):
        return UTC
    sign = 1 if text[0] == "+" else -1
    digits = text[1:].replace(":", "")
    return timezone(
        sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    )


def parse_value(value: Any) -> datetime | None:
    """A date field value as aware UTC; epoch millis for numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    m = _VALUE.match(value)
    if not m:
        return None
    try:
        frac = m["frac"] or ""
        moment = datetime(
            int(m["date"]),
            int(m["mo"]),
            int(m["d"]),
            int(m["h"] or 0),
            int(m["mi"] or 0),
            int(m["s"] or 0),
            int(frac[:6].ljust(6, "0")) if frac else 0,
            tzinfo=_zone(m["zone"]) or UTC,
        )
    except ValueError:
        return None
    return moment.astimezone(UTC)


def shift_value(value: Any, seconds: int) -> Any | None:
    """``value`` moved by ``seconds`` in its own format; None when it is not a
    date this rule reads. Numbers stay epoch millis of the same type; strings
    keep separators, precision (fraction digits beyond microseconds are kept
    as written) and the zone notation (an offset stays the same offset)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value + seconds * 1000
    if isinstance(value, float):
        return value + seconds * 1000.0
    if not isinstance(value, str):
        return None
    m = _VALUE.match(value)
    if not m:
        return None
    moment = parse_value(value)
    if moment is None:
        return None
    zone = _zone(m["zone"]) or UTC
    moved = (moment + timedelta(seconds=seconds)).astimezone(zone)
    d1 = m["d1"]
    out = moved.strftime(f"%Y{d1}%m{d1}%d")
    if m["h"] is None:
        return out
    out += m["sep"] + moved.strftime("%H:%M")
    if m["s"] is not None:
        out += moved.strftime(":%S")
        if m["frac"] is not None:
            frac = m["frac"]
            micro = f"{moved.microsecond:06d}"
            out += m["fsep"] + (
                micro[: len(frac)] if len(frac) <= 6 else micro + frac[6:]
            )
    return out + (m["zone"] or "")


def _get(source: Any, path: str) -> list[tuple[Any, Any]]:
    """``(container, key)`` slots holding the values at a dotted path."""
    if isinstance(source, list):
        out: list[tuple[Any, Any]] = []
        for item in source:
            out.extend(_get(item, path))
        return out
    if not isinstance(source, dict):
        return []
    if path in source:
        value = source[path]
        if isinstance(value, list):
            return [(value, i) for i in range(len(value)) if value[i] is not None]
        return [] if value is None else [(source, path)]
    head, dot, rest = path.partition(".")
    if dot and head in source:
        return _get(source[head], rest)
    return []


def shift_document(
    source: Mapping[str, Any], fields: Sequence[str], seconds: int
) -> tuple[dict[str, Any], int, int]:
    """``(new source, rewritten values, unparsed values)``; only ``fields``
    change -- message bodies with embedded time text (245) do not."""
    doc: dict[str, Any] = json.loads(json.dumps(source))
    rewritten = unparsed = 0
    for path in fields:
        for container, key in _get(doc, path):
            new = shift_value(container[key], seconds)
            if new is None:
                unparsed += 1
                continue
            container[key] = new
            rewritten += 1
    return doc, rewritten, unparsed


# -- offset ------------------------------------------------------------------------------


def plan_shift_seconds(anchor: datetime, now: datetime) -> int:
    """``now - LEAD - anchor`` rounded down to whole minutes, in seconds."""
    span = (now - LEAD - anchor).total_seconds()
    seconds = int(span // 60) * 60
    if seconds <= 0:
        raise ShiftError("SHIFT_NOT_NEEDED")
    return seconds


def check_no_overlap(
    original_min: datetime, original_max: datetime, seconds: int
) -> None:
    """N4: the shifted range ± 1 hour must stay clear of the original range."""
    low = original_min + timedelta(seconds=seconds) - DESHIFT_MARGIN
    high = original_max + timedelta(seconds=seconds) + DESHIFT_MARGIN
    if low <= original_max and original_min <= high:
        raise ShiftError("SHIFT_OVERLAP")


# -- question text (R10-1) ------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionRewrite:
    original: str
    rewritten: str
    instant: datetime  # UTC instant the rewritten text stands for


def _forward(m: re.Match[str], seconds: int) -> tuple[str, datetime] | None:
    try:
        day = date(int(m["y"]), int(m["mo"]), int(m["d"]))
        if m["h"] is None:
            moment = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
            moved = moment + timedelta(seconds=seconds)
            return moved.date().isoformat(), moved
        zone = _zone(m["zone"]) or UTC
        local = datetime(
            day.year,
            day.month,
            day.day,
            int(m["h"]),
            int(m["mi"]),
            int(m["s"] or 0),
            tzinfo=zone,
        )
    except ValueError:
        return None
    moved = local + timedelta(seconds=seconds)
    out = moved.strftime("%Y-%m-%d") + m["sep"] + moved.strftime("%H:%M")
    if m["s"] is not None:
        out += moved.strftime(":%S") + (m["frac"] or "")
    return out + (m["zone"] or ""), moved.astimezone(UTC)


def _forward_month(m: re.Match[str], seconds: int) -> tuple[str, datetime] | None:
    try:
        day, moment = month_date_parts(m)
    except ValueError:
        return None
    if moment is None:
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
        moved = noon + timedelta(seconds=seconds)
        return format_month_date(m, moved.date(), None), moved
    moved = moment + timedelta(seconds=seconds)
    return format_month_date(m, moved.date(), moved), moved


def rewrite_question(text: str, seconds: int) -> tuple[str, list[QuestionRewrite]]:
    """Every date-time and date the deshift reads, moved forward by
    ``seconds``; the same patterns as PR-A's deshift (ISO, and English month
    names since PR-C), so the two invert."""
    rewrites: list[QuestionRewrite] = []

    def replace(m: re.Match[str]) -> str:
        result = _forward(m, seconds)
        if result is None:
            return m.group(0)
        new, instant = result
        rewrites.append(QuestionRewrite(m.group(0), new, instant))
        return new

    def replace_month(m: re.Match[str]) -> str:
        result = _forward_month(m, seconds)
        if result is None:
            return m.group(0)
        new, instant = result
        rewrites.append(QuestionRewrite(m.group(0), new, instant))
        return new

    text = _STAMP.sub(replace, text)
    return _MONTH_DATE.sub(replace_month, text), rewrites


def question_instants(text: str) -> list[datetime]:
    """The UTC instants the question's dates and date-times stand for (a date
    alone counts as its 12:00 UTC), in text order."""
    _, rewrites = rewrite_question(text, 0)
    return [r.instant for r in rewrites]


def _incident_at(anchor: datetime, at: datetime) -> datetime:
    """The latest ``anchor + k days`` whose incident hour ends ``LEAD``
    before ``at``."""
    days = (at - LEAD - INCIDENT_SPAN - anchor) // DAY
    return anchor + days * DAY


def _day_fits(incident: datetime, at: datetime) -> bool:
    return incident >= at + DELIVERY_ALLOWANCE + BOX_MARGIN - BOX


def plan_day_shift_seconds(anchor: datetime, now: datetime) -> int:
    """The whole-day offset (in seconds) that puts the question's incident
    hour inside the box: the largest ``k`` with ``anchor + k days + 1 h <=
    now - LEAD``; ``SHIFT_DAY_UNAVAILABLE`` when that incident would start
    before the box at delivery, ``SHIFT_NOT_NEEDED`` for ``k <= 0``."""
    incident = _incident_at(anchor, now)
    days = (incident - anchor) // DAY
    if days <= 0:
        raise ShiftError("SHIFT_NOT_NEEDED")
    if not _day_fits(incident, now):
        raise ShiftError("SHIFT_DAY_UNAVAILABLE")
    return int(days) * 86400


def seconds_to_wait_for_shift_day(
    qid: str, now: datetime, setup_timeout_s: float
) -> float:
    """0 when a whole-day offset fits from ``now`` through ``now +
    setup_timeout_s`` (the shift runs after ``before_test``); otherwise the
    seconds until the next day's incident hour has ended ``LEAD`` ago.
    Only :data:`LOKI_FIXED_TIME`; over ``SHIFT_DAY_WAIT_CAP`` raises
    ``DeterministicError("SHIFT_WAIT_FOR_DAY")`` (the driver's class)."""
    if qid not in LOKI_FIXED_TIME:
        return 0.0
    anchor = LOKI_ANCHORS[qid]
    latest = now + timedelta(seconds=setup_timeout_s)
    incident = _incident_at(anchor, now)
    if _incident_at(anchor, latest) == incident and _day_fits(incident, latest):
        return 0.0
    ready = _incident_at(anchor, now) + DAY + INCIDENT_SPAN + LEAD
    wait = ready - now
    if wait > SHIFT_DAY_WAIT_CAP:
        from scripts.itbench_lite.driver import DeterministicError

        raise DeterministicError("SHIFT_WAIT_FOR_DAY")
    return max(wait.total_seconds(), 0.0) + 1.0


_TIME_ONLY = re.compile(r"(?<![\d:T])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])")


def time_only_phrases(text: str) -> int:
    """Times of day written without a date (not rewritten; disclosed)."""
    stripped = _MONTH_DATE.sub(" ", _STAMP.sub(" ", text))
    return len(_TIME_ONLY.findall(stripped))


# -- the record ------------------------------------------------------------------------------


@dataclass
class ShiftRecord:
    applied: bool
    reason: str | None = None
    seconds: int = 0
    anchor: datetime | None = None
    original_min: datetime | None = None
    original_max: datetime | None = None
    shifted_min: datetime | None = None
    shifted_max: datetime | None = None
    indices: list[dict[str, Any]] = field(default_factory=list)
    store_size_before: dict[str, int] = field(default_factory=dict)
    store_size_after: dict[str, int] = field(default_factory=dict)
    question_rewrites: list[QuestionRewrite] = field(default_factory=list)
    time_only_phrases: int = 0
    # PR-C (Loki); left out of ``as_json`` for Elasticsearch so B1 records
    # stay byte-identical.
    backend: str = "elasticsearch"
    anchor_source: str | None = None
    fixed_entries: int = 0
    pushed: int = 0
    dropped_future: int = 0
    line_stamps_rewritten: int = 0
    streams: list[dict[str, Any]] = field(default_factory=list)

    def as_text_shift(self) -> Shift | None:
        """PR-A's deshift input, or None when nothing was shifted."""
        if not self.applied or self.shifted_min is None or self.shifted_max is None:
            return None
        return Shift(self.seconds, self.shifted_min, self.shifted_max)

    def as_json(self) -> dict[str, Any]:
        def plain(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.astimezone(UTC).isoformat()
            if isinstance(value, dict):
                return {k: plain(v) for k, v in value.items()}
            if isinstance(value, list):
                return [plain(v) for v in value]
            return value

        out: dict[str, Any] = plain(asdict(self))
        if self.backend == "elasticsearch":
            for name in _LOG_ONLY_FIELDS:
                out.pop(name, None)
        else:
            for name in ("indices", "store_size_before", "store_size_after"):
                out.pop(name, None)
        return out


_LOG_ONLY_FIELDS = (
    "backend",
    "anchor_source",
    "fixed_entries",
    "pushed",
    "dropped_future",
    "line_stamps_rewritten",
    "streams",
)


# -- engineer-side client -------------------------------------------------------------------


class EsAdmin:
    """The evaluation's engineer-key client (setup side only, H-5)."""

    def __init__(
        self,
        url: str,
        engineer_key: str,
        *,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ShiftError("ES_URL_INVALID")
        self.url = url.rstrip("/")
        self._key = engineer_key
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    def __repr__(self) -> str:
        return f"EsAdmin({self.url!r})"

    def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        ndjson: bool = False,
        timeout: float = ES_TIMEOUT_S,
    ) -> Any:
        if body is None:
            data = None
        elif ndjson:
            data = body.encode("utf-8")
        else:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"ApiKey {self._key}",
                "Content-Type": "application/x-ndjson"
                if ndjson
                else "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ShiftError("ES_NOT_FOUND", path.split("?")[0]) from None
            raise ShiftError(
                "SHIFT_IO_FAILED", f"{method} {path.split('?')[0]} HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, OSError) as error:
            raise ShiftError(
                "SHIFT_IO_FAILED",
                f"{method} {path.split('?')[0]} {type(error).__name__}",
            ) from None
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except ValueError:
            raise ShiftError("SHIFT_IO_FAILED", "not JSON") from None

    @staticmethod
    def _quote(pattern: str) -> str:
        return ",".join(urllib.parse.quote(p, safe="*") for p in pattern.split(","))

    def indices(self, pattern: str) -> dict[str, int]:
        """``{index: store bytes}`` of the indices matching ``pattern``."""
        rows = self._call(
            "GET",
            f"/_cat/indices/{self._quote(pattern)}?format=json&bytes=b&h=index,store.size",
        )
        if not isinstance(rows, list):
            raise ShiftError("SHIFT_IO_FAILED", "cat shape")
        return {str(r["index"]): int(r.get("store.size") or 0) for r in rows}

    def mappings(self, pattern: str) -> dict[str, Any]:
        out = self._call("GET", f"/{self._quote(pattern)}/_mapping")
        if not isinstance(out, dict):
            raise ShiftError("SHIFT_IO_FAILED", "mapping shape")
        return out

    def documents(self, index: str) -> list[tuple[str, dict[str, Any]]]:
        out = self._call(
            "POST",
            f"/{self._quote(index)}/_search",
            {
                "size": MAX_DOCUMENTS,
                "query": {"match_all": {}},
                "sort": ["_doc"],
                "track_total_hits": True,
            },
        )
        hits = (out or {}).get("hits") or {}
        total = (hits.get("total") or {}).get("value")
        rows = hits.get("hits")
        if not isinstance(rows, list) or not isinstance(total, int):
            raise ShiftError("SHIFT_IO_FAILED", "search shape")
        if total > MAX_DOCUMENTS or len(rows) != total:
            raise ShiftError("SHIFT_INDEX_TOO_LARGE", index)
        return [(str(h["_id"]), dict(h.get("_source") or {})) for h in rows]

    def bulk_replace(
        self, index: str, docs: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> None:
        if not docs:
            return
        lines = []
        for doc_id, source in docs:
            lines.append(json.dumps({"index": {"_index": index, "_id": doc_id}}))
            lines.append(json.dumps(source, ensure_ascii=False))
        out = self._call(
            "POST", "/_bulk?refresh=wait_for", "\n".join(lines) + "\n", ndjson=True
        )
        if not isinstance(out, dict) or out.get("errors") is not False:
            raise ShiftError("SHIFT_IO_FAILED", f"bulk errors in {index}")

    def forcemerge(self, index: str) -> None:
        """``_forcemerge?max_num_segments=1`` (an index name or a pattern)."""
        self._call(
            "POST",
            f"/{self._quote(index)}/_forcemerge?max_num_segments=1",
            timeout=FORCEMERGE_TIMEOUT_S,
        )

    def refresh(self, pattern: str) -> None:
        self._call("POST", f"/{self._quote(pattern)}/_refresh")

    def documents_many(
        self, names: Sequence[str]
    ) -> dict[str, list[tuple[str, dict[str, Any]]]]:
        """Every document of ``names`` in few searches: the names go in
        comma-separated groups that fit one URL and one result window; a group
        over the window is split, a single index over it is
        ``SHIFT_INDEX_TOO_LARGE``."""
        out: dict[str, list[tuple[str, dict[str, Any]]]] = {n: [] for n in names}
        pending = [list(g) for g in _name_groups(names)]
        while pending:
            group = pending.pop()
            body = self._call(
                "POST",
                f"/{self._quote(','.join(group))}/_search",
                {
                    "size": MAX_DOCUMENTS,
                    "query": {"match_all": {}},
                    "sort": ["_doc"],
                    "track_total_hits": True,
                },
            )
            hits = (body or {}).get("hits") or {}
            total = (hits.get("total") or {}).get("value")
            rows = hits.get("hits")
            if not isinstance(rows, list) or not isinstance(total, int):
                raise ShiftError("SHIFT_IO_FAILED", "search shape")
            if total > MAX_DOCUMENTS or len(rows) != total:
                if len(group) == 1:
                    raise ShiftError("SHIFT_INDEX_TOO_LARGE", group[0])
                half = len(group) // 2
                pending += [group[:half], group[half:]]
                continue
            for hit in rows:
                out.setdefault(str(hit["_index"]), []).append(
                    (str(hit["_id"]), dict(hit.get("_source") or {}))
                )
        return out

    def bulk_rewrite(self, rows: Sequence[tuple[str, str, Mapping[str, Any]]]) -> None:
        """``(index, id, source)`` written back in chunked ``_bulk`` requests
        without a per-request refresh (the caller refreshes once)."""
        lines: list[str] = []
        size = 0
        for index, doc_id, source in rows:
            pair = (
                json.dumps({"index": {"_index": index, "_id": doc_id}}),
                json.dumps(source, ensure_ascii=False),
            )
            lines.extend(pair)
            size += len(pair[0]) + len(pair[1]) + 2
            if len(lines) >= 2 * BULK_DOCS or size >= BULK_BYTES:
                self._bulk(lines)
                lines, size = [], 0
        if lines:
            self._bulk(lines)

    def _bulk(self, lines: Sequence[str]) -> None:
        out = self._call("POST", "/_bulk", "\n".join(lines) + "\n", ndjson=True)
        if not isinstance(out, dict) or out.get("errors") is not False:
            raise ShiftError("SHIFT_IO_FAILED", "bulk errors")

    def delete_indices(self, names: Sequence[str]) -> None:
        """Delete by explicit names, chunked to fit the URL: Elasticsearch 8
        refuses wildcard deletes (``action.destructive_requires_name``)."""
        for group in _name_groups(names):
            try:
                self._call("DELETE", f"/{self._quote(','.join(group))}")
            except ShiftError as error:
                if error.code != "ES_NOT_FOUND":
                    raise

    def delete(self, pattern: str) -> None:
        """The indices matching ``pattern``, deleted by name."""
        names = sorted(self.indices(pattern))
        if names:
            self.delete_indices(names)


def _name_groups(names: Sequence[str]) -> list[list[str]]:
    """Comma-joined groups of at most ``URL_NAMES_CHARS`` characters."""
    groups: list[list[str]] = []
    current: list[str] = []
    length = 0
    for name in names:
        cost = len(urllib.parse.quote(name, safe="*")) + 1
        if current and length + cost > URL_NAMES_CHARS:
            groups.append(current)
            current, length = [], 0
        current.append(name)
        length += cost
    if current:
        groups.append(current)
    return groups


# -- orchestration -------------------------------------------------------------------------------


def apply_shift(
    admin: EsAdmin, qid: str, question: str, now: datetime
) -> tuple[str, ShiftRecord]:
    """Shift a fixed-time question's data and question text by one offset.

    Returns the question to deliver and the record for the summary.
    """
    phrases = time_only_phrases(question)
    if qid not in FIXED_TIME:
        return question, ShiftRecord(
            applied=False, reason="RELATIVE_TIME", time_only_phrases=phrases
        )
    pattern = index_pattern(qid)
    mapped = admin.mappings(pattern)
    fields = {name: date_fields(spec) for name, spec in sorted(mapped.items())}
    dated = {name: f for name, f in fields.items() if f}
    if not dated:
        return question, ShiftRecord(
            applied=False, reason="NO_DATE_FIELDS", time_only_phrases=phrases
        )
    docs = admin.documents_many(sorted(dated))
    instants = [
        moment
        for name, rows in docs.items()
        if name in dated
        for _, source in rows
        for path in dated[name]
        for container, key in _get(json.loads(json.dumps(source)), path)
        if (moment := parse_value(container[key])) is not None
    ]
    if not instants:
        return question, ShiftRecord(
            applied=False, reason="NO_DATED_DOCUMENTS", time_only_phrases=phrases
        )
    anchor = max(instants)
    original_min = min(instants)
    seconds = plan_shift_seconds(anchor, now)
    check_no_overlap(original_min, anchor, seconds)
    before = admin.indices(pattern)
    record = ShiftRecord(
        applied=True,
        seconds=seconds,
        anchor=anchor,
        original_min=original_min,
        original_max=anchor,
        store_size_before={k: before[k] for k in sorted(before) if k in dated},
        time_only_phrases=phrases,
    )
    # One chunked _bulk across the question's indices, then one refresh and
    # one forcemerge over the pattern (184/185 have ~1600 indices).
    rewrites_out: list[tuple[str, str, dict[str, Any]]] = []
    for name in sorted(dated):
        counts = [0, 0]
        for doc_id, source in docs.get(name, []):
            new, rewritten, unparsed = shift_document(source, dated[name], seconds)
            counts[0] += rewritten
            counts[1] += unparsed
            if rewritten:
                rewrites_out.append((name, doc_id, new))
        record.indices.append(
            {
                "index": name,
                "fields": dated[name],
                "documents": len(docs.get(name, [])),
                "rewritten": counts[0],
                "unparsed": counts[1],
            }
        )
    if rewrites_out:
        admin.bulk_rewrite(rewrites_out)
        admin.refresh(pattern)
        admin.forcemerge(pattern)
    after = admin.indices(pattern)
    record.store_size_after = {k: after[k] for k in sorted(after) if k in dated}
    delivered, rewrites = rewrite_question(question, seconds)
    record.question_rewrites = rewrites
    shifted = [m + timedelta(seconds=seconds) for m in (original_min, anchor)]
    shifted += [r.instant for r in rewrites]
    record.shifted_min, record.shifted_max = min(shifted), max(shifted)
    if rewrites:
        # The union must still clear the original range (N4).
        low = record.shifted_min - DESHIFT_MARGIN
        high = record.shifted_max + DESHIFT_MARGIN
        if low <= anchor and original_min <= high:
            raise ShiftError("SHIFT_OVERLAP")
    return delivered, record


# -- Loki (PR-C) -------------------------------------------------------------------------------


@dataclass(frozen=True)
class LokiEntry:
    labels: Mapping[str, str]
    ns: int
    line: str


def _ns(moment: datetime) -> int:
    moment = moment.astimezone(UTC)
    whole = int((moment - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(seconds=1))
    return whole * 1_000_000_000 + moment.microsecond * 1000


def _from_ns(ns: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=ns // 1000)


def _selector_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _spans(
    start: datetime, end: datetime, step: timedelta
) -> list[tuple[datetime, datetime]]:
    """``[start, end)`` as consecutive half-open spans of at most ``step``."""
    out: list[tuple[datetime, datetime]] = []
    lo = start
    while lo < end:
        hi = min(lo + step, end)
        out.append((lo, hi))
        lo = hi
    return out


def _covering_names(streams: set[tuple[tuple[str, str], ...]]) -> list[str]:
    """Label names that together appear in every stream (greedy: the name in
    the most uncovered streams first, ties by name)."""
    left = set(streams)
    names: list[str] = []
    while left:
        counts: dict[str, int] = {}
        for key in left:
            for name, _ in key:
                counts[name] = counts.get(name, 0) + 1
        if not counts:
            break  # a stream without labels cannot exist in Loki
        best = min(counts, key=lambda n: (-counts[n], n))
        names.append(best)
        left = {key for key in left if best not in dict(key)}
    return names


class LokiAdmin:
    """Engineer-side client of a question's own Loki (its original port, not
    the gateway; the fixtures' Loki has no authentication)."""

    def __init__(
        self, url: str, *, opener: urllib.request.OpenerDirector | None = None
    ) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ShiftError("LOKI_URL_INVALID")
        self.url = url.rstrip("/")
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    def __repr__(self) -> str:
        return f"LokiAdmin({self.url!r})"

    def _call(
        self, method: str, path: str,
        query: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
        body: Any = None,
    ) -> Any:  # fmt: skip
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=LOKI_TIMEOUT_S) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise ShiftError(
                "SHIFT_IO_FAILED", f"{method} {path} HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, OSError) as error:
            raise ShiftError(
                "SHIFT_IO_FAILED", f"{method} {path} {type(error).__name__}"
            ) from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ShiftError("SHIFT_IO_FAILED", "not JSON") from None

    def label_names(self, start: datetime, end: datetime) -> list[str]:
        out = self._call(
            "GET", "/loki/api/v1/labels",
            {"start": str(_ns(start)), "end": str(_ns(end))},
        )  # fmt: skip
        data = out.get("data") if isinstance(out, dict) else None
        return sorted(str(n) for n in data or [] if not str(n).startswith("__"))

    def series(
        self, start: datetime, end: datetime, names: Sequence[str]
    ) -> list[dict[str, str]]:
        """Label sets of the streams with a label in ``names`` and data in
        ``[start, end)`` (one ``match[]`` per name)."""
        query = [("match[]", "{" + f'{n}=~".+"' + "}") for n in names]
        query += [("start", str(_ns(start))), ("end", str(_ns(end)))]
        out = self._call("GET", "/loki/api/v1/series", query)
        data = out.get("data") if isinstance(out, dict) else None
        if not isinstance(data, list):
            raise ShiftError("SHIFT_IO_FAILED", "unexpected series result")
        return [{str(k): str(v) for k, v in (s or {}).items()} for s in data]

    def _page(self, selector: str, start_ns: int, end_ns: int) -> list[LokiEntry]:
        out = self._call(
            "GET",
            "/loki/api/v1/query_range",
            {
                "query": selector,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": str(LOKI_PAGE),
                "direction": "forward",
            },
        )
        data = out.get("data") if isinstance(out, dict) else None
        if not isinstance(data, dict) or data.get("resultType") != "streams":
            raise ShiftError("SHIFT_IO_FAILED", "unexpected query_range result")
        rows: list[LokiEntry] = []
        for stream in data.get("result") or []:
            labels = {str(k): str(v) for k, v in (stream.get("stream") or {}).items()}
            for ts, line in stream.get("values") or []:
                rows.append(LokiEntry(labels, int(ts), str(line)))
        return rows

    def entries(self, start: datetime, end: datetime) -> list[LokiEntry]:
        """Every entry in ``[start, end)`` of every stream, oldest first.

        The streams with data in the range are listed first (labels, then
        series, per ``LOKI_SERIES_SPAN``); a few label names that together
        cover every listed stream (every stream carries at least one label)
        each give a selector ``{L=~".+"}``, read per ``LOKI_QUERY_SPAN``
        window, paged forward by time, duplicates across selectors, windows
        and page boundaries dropped."""
        streams: set[tuple[tuple[str, str], ...]] = set()
        for lo, hi in _spans(start, end, LOKI_SERIES_SPAN):
            names = self.label_names(lo, hi)
            if names:
                for labels in self.series(lo, hi, names):
                    streams.add(tuple(sorted(labels.items())))
        selectors = _covering_names(streams)
        seen: set[tuple[tuple[tuple[str, str], ...], int, str]] = set()
        out: list[LokiEntry] = []
        for lo, hi in _spans(start, end, LOKI_QUERY_SPAN):
            self._window_entries(lo, hi, selectors, seen, out)
        out.sort(key=lambda e: (e.ns, sorted(e.labels.items()), e.line))
        return out

    def _window_entries(
        self,
        start: datetime,
        end: datetime,
        names: Sequence[str],
        seen: set[tuple[tuple[tuple[str, str], ...], int, str]],
        out: list[LokiEntry],
    ) -> None:
        end_ns = _ns(end)
        for name in names:
            selector = "{" + f'{name}=~".+"' + "}"
            cursor = _ns(start)
            while True:
                page = self._page(selector, cursor, end_ns)
                for entry in page:
                    key = (tuple(sorted(entry.labels.items())), entry.ns, entry.line)
                    if key in seen or not cursor <= entry.ns < end_ns:
                        continue
                    seen.add(key)
                    out.append(entry)
                    if len(out) > LOKI_MAX_ENTRIES:
                        raise ShiftError("SHIFT_TOO_MANY_ENTRIES")
                if len(page) < LOKI_PAGE:
                    break
                latest = max(e.ns for e in page)
                if latest <= cursor:
                    raise ShiftError("SHIFT_PAGE_STUCK")
                cursor = latest

    def push(self, entries: Sequence[LokiEntry]) -> None:
        """Chunked ``/loki/api/v1/push`` (at most ``LOKI_PUSH_ENTRIES``
        entries or ``LOKI_PUSH_BYTES`` per request)."""
        batch: dict[tuple[tuple[str, str], ...], list[list[str]]] = {}
        count = size = 0

        def flush() -> None:
            nonlocal batch, count, size
            if batch:
                streams = [{"stream": dict(k), "values": v} for k, v in batch.items()]
                self._call("POST", "/loki/api/v1/push", body={"streams": streams})
            batch, count, size = {}, 0, 0

        for entry in entries:
            key = tuple(sorted(entry.labels.items()))
            value = [str(entry.ns), entry.line]
            # The value's JSON size plus room for this stream's label block.
            cost = len(json.dumps(value).encode("utf-8")) + 2
            if key not in batch:
                cost += len(json.dumps({"stream": dict(key), "values": []})) + 2
            if count and (
                count + 1 > LOKI_PUSH_ENTRIES
                or size + cost > LOKI_PUSH_BYTES - _PUSH_ENVELOPE
            ):
                flush()
                cost = len(json.dumps(value).encode("utf-8")) + 2
                cost += len(json.dumps({"stream": dict(key), "values": []})) + 2
            batch.setdefault(key, []).append(value)
            count += 1
            size += cost
        flush()


def shift_line(line: str, seconds: int) -> tuple[str, int]:
    """ISO date-times and dates inside a log line moved by ``seconds``."""
    count = 0

    def replace(m: re.Match[str]) -> str:
        nonlocal count
        result = _forward(m, seconds)
        if result is None:
            return m.group(0)
        count += 1
        return result[0]

    return _STAMP.sub(replace, line), count


def apply_loki_shift(
    admin: LokiAdmin,
    qid: str,
    question: str,
    now: datetime,
    *,
    setup_started: datetime,
) -> tuple[str, ShiftRecord]:
    """Shift a fixed-time Loki question by whole days (see module docstring)."""
    phrases = time_only_phrases(question)
    if qid not in LOKI_FIXED_TIME:
        return question, ShiftRecord(
            applied=False, reason="RELATIVE_TIME", time_only_phrases=phrases,
            backend="loki",
        )  # fmt: skip
    instants = question_instants(question)
    if not instants:
        raise ShiftError("SHIFT_NO_QUESTION_TIME")
    anchor = min(instants)
    seconds = plan_day_shift_seconds(anchor, now)
    read_start = anchor - LOKI_ANCHOR_READ
    read_end = min(anchor + LOKI_ANCHOR_READ, setup_started - FIXED_ENTRY_AGE)
    fixed = admin.entries(read_start, read_end)
    if not fixed:
        raise ShiftError("SHIFT_NO_FIXED_ENTRIES")
    if fixed[0].ns < _ns(read_start + LOKI_EDGE_GUARD) or (
        read_end == anchor + LOKI_ANCHOR_READ
        and fixed[-1].ns >= _ns(read_end - LOKI_EDGE_GUARD)
    ):
        raise ShiftError("SHIFT_DATA_AT_READ_EDGE")
    check_no_overlap(_from_ns(fixed[0].ns), _from_ns(fixed[-1].ns), seconds)
    cutoff = _ns(now - LEAD)
    offset = seconds * 1_000_000_000
    kept: list[LokiEntry] = []
    stamps = dropped = 0
    per_stream: dict[tuple[tuple[str, str], ...], list[int]] = {}
    for entry in fixed:
        key = tuple(sorted(entry.labels.items()))
        tally = per_stream.setdefault(key, [0, 0])
        tally[0] += 1
        moved = entry.ns + offset
        if moved > cutoff:
            dropped += 1
            continue
        line, n = shift_line(entry.line, seconds)
        stamps += n
        kept.append(LokiEntry(entry.labels, moved, line))
        tally[1] += 1
    if not kept:
        raise ShiftError("SHIFT_NO_FIXED_ENTRIES", "all entries after now")
    admin.push(kept)
    delivered, rewrites = rewrite_question(question, seconds)
    low, high = _from_ns(kept[0].ns - offset), _from_ns(kept[-1].ns - offset)
    shifted = [_from_ns(kept[0].ns), _from_ns(kept[-1].ns)] + [
        r.instant for r in rewrites
    ]
    record = ShiftRecord(
        applied=True,
        seconds=seconds,
        anchor=anchor,
        original_min=low,
        original_max=high,
        shifted_min=min(shifted),
        shifted_max=max(shifted),
        question_rewrites=rewrites,
        time_only_phrases=phrases,
        backend="loki",
        anchor_source="question",
        fixed_entries=len(fixed),
        pushed=len(kept),
        dropped_future=dropped,
        line_stamps_rewritten=stamps,
        streams=[
            {"labels": dict(k), "entries": v[0], "pushed": v[1]}
            for k, v in sorted(per_stream.items())
        ],
    )
    if (
        min(shifted) - DESHIFT_MARGIN <= _from_ns(fixed[-1].ns)
        and _from_ns(fixed[0].ns) <= max(shifted) + DESHIFT_MARGIN
    ):
        raise ShiftError("SHIFT_OVERLAP")
    return delivered, record


def shift_question(
    backend: str,
    *,
    es_admin: EsAdmin | None,
    loki_admin: LokiAdmin | None,
    qid: str,
    question: str,
    now: datetime,
    setup_started: datetime,
) -> tuple[str, ShiftRecord]:
    """One entry point for every backend: Elasticsearch -> :func:`apply_shift`,
    Loki -> :func:`apply_loki_shift`, VictoriaLogs -> no shift (both questions
    are relative)."""
    if backend == "elasticsearch":
        assert es_admin is not None
        return apply_shift(es_admin, qid, question, now)
    if backend == "loki":
        assert loki_admin is not None
        return apply_loki_shift(
            loki_admin, qid, question, now, setup_started=setup_started
        )
    if backend in ("sql", "mongodb"):
        # PR-F (C6): no absolute time in the question, data written by setup.
        return question, ShiftRecord(
            applied=False, reason="NO_TIME_REFERENCE",
            time_only_phrases=time_only_phrases(question), backend=backend,
        )  # fmt: skip
    return question, ShiftRecord(
        applied=False, reason="RELATIVE_TIME",
        time_only_phrases=time_only_phrases(question), backend=backend,
    )  # fmt: skip
