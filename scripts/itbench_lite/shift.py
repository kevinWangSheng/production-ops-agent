"""One time offset for all six data classes of a scenario (#581).

The replay moves metrics, traces, alerts, cluster objects, events and logs by
the same whole number of seconds, so that the latest end time over all classes
lands about a minute before the planned alert delivery and nothing lands in
the future. ``compute_offset_seconds`` is the pure rule; the ``*_end_ns``
readers give each class's end time; the ``shift_*`` helpers apply the offset.

The offset is a whole number of seconds so that every timestamp keeps its
original fractional digits and format.

Cluster objects, events and logs are consumed by the fake API server (#583);
this module only reads their time fields and shifts their rows.
"""

from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from scripts.itbench_lite.literals import parse_cell
from scripts.itbench_lite.snapshot import Snapshot, load_snapshot

NS = 1_000_000_000
DATA_CLASSES = ("metrics", "traces", "alerts", "objects", "events", "logs")
DEFAULT_LEAD = timedelta(seconds=60)

# -- timestamp formats ------------------------------------------------------

#: ClickHouse ``DateTime``/``DateTime64`` as exported: ``YYYY-MM-DD HH:MM:SS[.f…]``.
_CLICKHOUSE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})([ T])(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z?)"
)
#: RFC 3339 as Kubernetes and Prometheus write it.
_RFC3339 = re.compile(
    r"(\d{4}-\d{2}-\d{2})([Tt])(\d{2}:\d{2}:\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})"
)


def parse_clickhouse_ns(text: str) -> int:
    """UTC epoch nanoseconds of a ClickHouse timestamp cell."""
    match = _CLICKHOUSE.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a ClickHouse timestamp: {text!r}")
    year, month, day, _sep, hour, minute, second, frac, _z = match.groups()
    seconds = calendar.timegm(
        (int(year), int(month), int(day), int(hour), int(minute), int(second))
    )
    return seconds * NS + int((frac or "").ljust(9, "0"))


def shift_clickhouse_timestamp(text: str, offset_seconds: int) -> str:
    """``text`` moved by ``offset_seconds``, keeping its separator and digits."""
    match = _CLICKHOUSE.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a ClickHouse timestamp: {text!r}")
    year, month, day, sep, hour, minute, second, frac, zulu = match.groups()
    moment = datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second)
    ) + timedelta(seconds=offset_seconds)
    out = moment.strftime(f"%Y-%m-%d{sep}%H:%M:%S")
    if frac is not None:
        out += "." + frac
    return out + zulu


def parse_rfc3339_ns(text: str) -> int:
    """UTC epoch nanoseconds of an RFC 3339 timestamp."""
    match = _RFC3339.fullmatch(text)
    if match is None:
        raise ValueError(f"not an RFC 3339 timestamp: {text!r}")
    date, _t, clock, frac, zone = match.groups()
    tz = "+00:00" if zone in ("Z", "z") else zone
    moment = datetime.fromisoformat(f"{date}T{clock}{tz}")
    digits = (frac or ".")[1:]
    return calendar.timegm(moment.utctimetuple()) * NS + int(digits[:9].ljust(9, "0"))


def _is_zero_time(date: str) -> bool:
    # Go's zero ``time.Time`` (year 1) is a sentinel, not a moment.
    return date.startswith("0001-")


def shift_rfc3339(text: str, offset_seconds: int) -> str:
    """``text`` moved by ``offset_seconds``, keeping its zone and digits.

    Go's zero time (``0001-01-01T00:00:00Z``) is a sentinel and stays as is.
    """
    match = _RFC3339.fullmatch(text)
    if match is None:
        raise ValueError(f"not an RFC 3339 timestamp: {text!r}")
    date, sep, clock, frac, zone = match.groups()
    if _is_zero_time(date):
        return text
    tz = "+00:00" if zone in ("Z", "z") else zone
    moment = datetime.fromisoformat(f"{date}T{clock}{tz}")
    moved = (moment + timedelta(seconds=offset_seconds)).astimezone(moment.tzinfo)
    return moved.strftime(f"%Y-%m-%d{sep}%H:%M:%S") + (frac or "") + zone


def is_rfc3339(value: object) -> bool:
    return isinstance(value, str) and _RFC3339.fullmatch(value) is not None


def shift_json_times(value: Any, offset_seconds: int) -> Any:
    """A copy of a JSON value with every RFC 3339 string moved by the offset.

    Any string that is exactly an RFC 3339 timestamp moves, wherever it sits
    (metadata, status, managedFields, also ConfigMap data that happens to be
    one); other strings, keys and numbers stay as they are.
    """
    if isinstance(value, dict):
        return {k: shift_json_times(v, offset_seconds) for k, v in value.items()}
    if isinstance(value, list):
        return [shift_json_times(v, offset_seconds) for v in value]
    if is_rfc3339(value):
        return shift_rfc3339(value, offset_seconds)
    return value


def json_times_ns(value: Any) -> Iterable[int]:
    """Epoch nanoseconds of every non-sentinel RFC 3339 string in a JSON value."""
    if isinstance(value, dict):
        for v in value.values():
            yield from json_times_ns(v)
    elif isinstance(value, list):
        for v in value:
            yield from json_times_ns(v)
    elif is_rfc3339(value) and not _is_zero_time(value):
        yield parse_rfc3339_ns(value)


def to_ns(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError("delivery time must be timezone-aware")
    utc = moment.astimezone(UTC)
    return calendar.timegm(utc.utctimetuple()) * NS + utc.microsecond * 1000


def from_ns(value: int) -> datetime:
    seconds, rest = divmod(value, NS)
    return datetime.fromtimestamp(seconds, UTC) + timedelta(microseconds=rest // 1000)


# -- the offset ---------------------------------------------------------------


def compute_offset_seconds(
    end_ns: Mapping[str, int | None],
    delivery_at: datetime,
    *,
    lead: timedelta = DEFAULT_LEAD,
) -> int:
    """Whole seconds that move the latest end of any class to ``delivery_at - lead``.

    ``end_ns`` maps a data class to its latest end time (epoch ns), ``None``
    for a class the scenario does not have. The result is rounded down, so the
    latest shifted end is at most ``delivery_at - lead`` and less than one
    second earlier than that; every other point is earlier still.
    """
    present = [v for v in end_ns.values() if v is not None]
    if not present:
        raise ValueError("no data class has an end time")
    target = to_ns(delivery_at) - (lead // timedelta(microseconds=1)) * 1000
    return (target - max(present)) // NS


def latest_shifted_ns(end_ns: Mapping[str, int | None], offset_seconds: int) -> int:
    return max(v for v in end_ns.values() if v is not None) + offset_seconds * NS


def check_not_future(
    end_ns: Mapping[str, int | None], offset_seconds: int, delivery_at: datetime
) -> None:
    """Raise ``ValueError`` if any class would end after ``delivery_at``."""
    limit = to_ns(delivery_at)
    for kind, value in end_ns.items():
        if value is not None and value + offset_seconds * NS > limit:
            raise ValueError(f"{kind} would end after the delivery time")


# -- per-class end times ------------------------------------------------------


def _max(values: Iterable[int]) -> int | None:
    return max(values, default=None)


def metrics_end_ns(snapshot: Snapshot) -> int | None:
    """Latest ``timestamp`` of a metrics row that carries a value."""

    def times() -> Iterable[int]:
        for path in snapshot.metrics:
            for row in snapshot.iter_tsv(path):
                if (row.get("value") or "").strip():
                    yield parse_clickhouse_ns(row["timestamp"])

    return _max(times())


def _literal_list(text: str | None, column: str) -> list[Any]:
    if not text or not text.strip():
        return []
    value = parse_cell(text, column)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"column {column}: not a list")
    return list(value)


def traces_end_ns(snapshot: Snapshot) -> int | None:
    """Latest span end (``Timestamp + Duration``) or span event time."""
    path = snapshot.tsv_path("traces")
    if path is None:
        return None

    def times() -> Iterable[int]:
        for row in snapshot.iter_tsv(path):
            yield parse_clickhouse_ns(row["Timestamp"]) + int(row["Duration"])
            for stamp in _literal_list(row.get("Events.Timestamp"), "Events.Timestamp"):
                yield parse_clickhouse_ns(str(stamp))

    return _max(times())


def alerts_end_ns(snapshot: Snapshot) -> int | None:
    """Latest ``activeAt`` over every entry of every alert file."""

    def times() -> Iterable[int]:
        for path in snapshot.alerts:
            for entry in snapshot.read_alert_file(path):
                active = entry.get("activeAt")
                if is_rfc3339(active) and not _is_zero_time(active):
                    yield parse_rfc3339_ns(active)

    return _max(times())


def _row_times(snapshot: Snapshot, kind: str, *, body: bool) -> int | None:
    path = snapshot.tsv_path(kind)
    if path is None:
        return None

    def times() -> Iterable[int]:
        for row in snapshot.iter_tsv(path):
            yield parse_clickhouse_ns(row["Timestamp"])
            if body and row.get("Body"):
                yield from json_times_ns(json.loads(row["Body"]))

    return _max(times())


def objects_end_ns(snapshot: Snapshot) -> int | None:
    """Latest collection time (``Timestamp``) of a cluster object row.

    Object bodies are not scanned: they can legitimately hold future-dated
    values (expiry dates and the like) that are not "now" in the snapshot.
    """
    return _row_times(snapshot, "objects", body=False)


def events_end_ns(snapshot: Snapshot) -> int | None:
    """Latest watch time or event time (any RFC 3339 field in ``Body``)."""
    return _row_times(snapshot, "events", body=True)


def logs_end_ns(snapshot: Snapshot) -> int | None:
    """Latest log record ``Timestamp``."""
    return _row_times(snapshot, "logs", body=False)


def snapshot_end_times(snapshot: Snapshot) -> dict[str, int | None]:
    """End time (epoch ns) of each of the six classes, ``None`` when absent."""
    return {
        "metrics": metrics_end_ns(snapshot),
        "traces": traces_end_ns(snapshot),
        "alerts": alerts_end_ns(snapshot),
        "objects": objects_end_ns(snapshot),
        "events": events_end_ns(snapshot),
        "logs": logs_end_ns(snapshot),
    }


# -- applying the offset to raw rows ---------------------------------------------

_ROW_TIME_COLUMNS = ("Timestamp", "TimestampTime")


def shift_log_row(row: Mapping[str, str], offset_seconds: int) -> dict[str, str]:
    """A log row with its time columns moved; ``Body`` text is not rewritten."""
    out = dict(row)
    for column in _ROW_TIME_COLUMNS:
        if out.get(column):
            out[column] = shift_clickhouse_timestamp(out[column], offset_seconds)
    return out


def shift_object_row(row: Mapping[str, str], offset_seconds: int) -> dict[str, str]:
    """An object or event row with its time columns and ``Body`` times moved."""
    out = shift_log_row(row, offset_seconds)
    if out.get("Body"):
        out["Body"] = json.dumps(
            shift_json_times(json.loads(out["Body"]), offset_seconds),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return out


shift_event_row = shift_object_row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot")
    parser.add_argument("--delivery-at", required=True, help="RFC 3339, with zone")
    args = parser.parse_args(argv)
    delivery = datetime.fromisoformat(args.delivery_at.replace("Z", "+00:00"))
    snapshot = load_snapshot(args.snapshot)
    ends = snapshot_end_times(snapshot)
    offset = compute_offset_seconds(ends, delivery)
    check_not_future(ends, offset, delivery)
    report = {
        "offset_seconds": offset,
        "ends": {
            k: None if v is None else from_ns(v).isoformat() for k, v in ends.items()
        },
        "latest_shifted": from_ns(latest_shifted_ns(ends, offset)).isoformat(),
    }
    json.dump(report, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
