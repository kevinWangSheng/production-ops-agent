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
   written back under the same ``_id`` with ``refresh=wait_for``, and every
   rewritten index force-merged (187 asks about storage; the rewrite would
   otherwise leave deleted documents behind);
5. R10-1: the question text's ISO date-times and ``YYYY-MM-DD`` dates move by
   the same offset. A date alone becomes the date of "that day 12:00 UTC +
   offset" -- under a non-whole-day offset it can land a day off, the same
   ambiguity as the deshift's date midpoint (N1, disclosed). A time of day
   without a date is not rewritten; ``time_only_phrases`` counts them for the
   disclosure.

The rule reads data, mappings and the question text only -- never an answer.
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

from scripts.holmes_evals.text import _STAMP, DESHIFT_MARGIN, Shift

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
    }
)
#: H-24: index C is written with the current UTC date.
UTC_DATE_SENSITIVE: frozenset[str] = frozenset({"280_es_multi_quirk"})
LEAD = timedelta(seconds=60)
MAX_DOCUMENTS = 10_000
ES_TIMEOUT_S = 60
_DATE_TYPES = frozenset({"date", "date_nanos"})


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


def rewrite_question(text: str, seconds: int) -> tuple[str, list[QuestionRewrite]]:
    """Every date-time and date the deshift reads, moved forward by
    ``seconds``; the same pattern as PR-A's deshift, so the two invert."""
    rewrites: list[QuestionRewrite] = []

    def replace(m: re.Match[str]) -> str:
        result = _forward(m, seconds)
        if result is None:
            return m.group(0)
        new, instant = result
        rewrites.append(QuestionRewrite(m.group(0), new, instant))
        return new

    return _STAMP.sub(replace, text), rewrites


_TIME_ONLY = re.compile(r"(?<![\d:T])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])")


def time_only_phrases(text: str) -> int:
    """Times of day written without a date (not rewritten; disclosed)."""
    stripped = _STAMP.sub(" ", text)
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
        return out


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
        self, method: str, path: str, body: Any = None, *, ndjson: bool = False
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
            with self._opener.open(request, timeout=ES_TIMEOUT_S) as response:
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
        self._call("POST", f"/{self._quote(index)}/_forcemerge?max_num_segments=1")

    def delete(self, pattern: str) -> None:
        try:
            self._call("DELETE", f"/{self._quote(pattern)}?expand_wildcards=all")
        except ShiftError as error:
            if error.code != "ES_NOT_FOUND":
                raise


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
    docs = {name: admin.documents(name) for name in dated}
    instants = [
        moment
        for name, rows in docs.items()
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
    for name in sorted(dated):
        rewritten_docs = []
        counts = [0, 0]
        for doc_id, source in docs[name]:
            new, rewritten, unparsed = shift_document(source, dated[name], seconds)
            counts[0] += rewritten
            counts[1] += unparsed
            if rewritten:
                rewritten_docs.append((doc_id, new))
        admin.bulk_replace(name, rewritten_docs)
        if rewritten_docs:
            admin.forcemerge(name)
        record.indices.append(
            {
                "index": name,
                "fields": dated[name],
                "documents": len(docs[name]),
                "rewritten": counts[0],
                "unparsed": counts[1],
            }
        )
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
