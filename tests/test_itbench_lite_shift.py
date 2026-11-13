"""#581: one offset for all six data classes, taken from the latest end over
all of them, so nothing lands in the future. Synthetic data only."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.itbench_lite import shift
from scripts.itbench_lite.alerts import EntryTarget, entry_webhook
from scripts.itbench_lite.metrics import convert_metrics
from scripts.itbench_lite.snapshot import load_snapshot
from scripts.itbench_lite.traces import iter_otlp_requests

NS = 10**9
DELIVERY = datetime(2026, 11, 13, 20, 0, 0, tzinfo=UTC)
LOG_COLUMNS = ["Timestamp", "TimestampTime", "Body", "ResourceAttributes"]
TRACE_COLUMNS = [
    "Timestamp", "TraceId", "SpanId", "ParentSpanId", "SpanName", "SpanKind",
    "ServiceName", "ResourceAttributes", "SpanAttributes", "Duration",
    "StatusCode", "StatusMessage", "Events.Timestamp", "Events.Name",
    "Events.Attributes",
]  # fmt: skip


def _ns(text: str) -> int:
    return shift.parse_clickhouse_ns(text)


def _tsv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _scenario(tmp_path: Path) -> Path:
    """Six classes; traces end latest (17:59:30 start + 20 s duration)."""
    root = tmp_path / "s"
    (root / "metrics").mkdir(parents=True)
    (root / "metrics" / "p.tsv").write_text(
        "timestamp\tmetric_name\tvalue\ttags\tnamespace\tpod_name\tservice_name\n"
        "2025-12-15 17:58:00.000\tm\t1\t{}\t\t\t\n"
        "2025-12-15 18:30:00.000\tm\t\t{}\t\t\t\n"  # empty value: not data
    )
    _tsv(
        root / "otel_traces_raw.tsv",
        TRACE_COLUMNS,
        [
            {
                "Timestamp": "2025-12-15 17:59:30.000000000",
                "TraceId": "0af7651916cd43dd8448eb211c80319c",
                "SpanId": "b7ad6b7169203331",
                "ParentSpanId": "",
                "SpanName": "op",
                "SpanKind": "Server",
                "ServiceName": "checkout",
                "ResourceAttributes": "{}",
                "SpanAttributes": "{}",
                "Duration": str(20 * NS),
                "StatusCode": "Unset",
                "StatusMessage": "",
                "Events.Timestamp": "['2025-12-15 17:59:40.000000000']",
                "Events.Name": "['e']",
                "Events.Attributes": "[{}]",
            }
        ],
    )
    (root / "alerts").mkdir()
    (root / "alerts" / "alerts_in_alerting_state_2025-12-15T175900Z.json").write_text(
        json.dumps(
            {
                "data": {
                    "alerts": [
                        {
                            "labels": {
                                "alertname": "RequestErrorRate",
                                "service_name": "checkout",
                            },
                            "state": "firing",
                            "activeAt": "2025-12-15T17:50:00.123456789Z",
                        }
                    ]
                }
            }
        )
    )
    obj = {
        "kind": "Pod",
        "metadata": {"name": "checkout-1", "creationTimestamp": "2025-12-15T17:00:00Z"},
        "data": {"expires": "2030-01-01T00:00:00Z"},  # future value inside a body
    }
    _tsv(
        root / "k8s_objects_raw.tsv",
        LOG_COLUMNS,
        [
            {
                "Timestamp": "2025-12-15 17:55:00.5",
                "TimestampTime": "2025-12-15 17:55:00",
                "Body": json.dumps(obj),
                "ResourceAttributes": "{}",
            }
        ],
    )
    event = {
        "type": "ADDED",
        "object": {
            "kind": "Event",
            "lastTimestamp": "2025-12-15T17:57:10Z",
            "eventTime": None,
        },
    }
    _tsv(
        root / "k8s_events_raw.tsv",
        LOG_COLUMNS,
        [
            {
                "Timestamp": "2025-12-15 17:57:00.0",
                "TimestampTime": "2025-12-15 17:57:00",
                "Body": json.dumps(event),
                "ResourceAttributes": "{}",
            }
        ],
    )
    _tsv(
        root / "otel_logs_raw.tsv",
        LOG_COLUMNS,
        [
            {
                "Timestamp": "2025-12-15 17:56:00.987654321",
                "TimestampTime": "2025-12-15 17:56:00",
                "Body": "at 2025-12-15T17:56:00Z",
                "ResourceAttributes": "{}",
            }
        ],
    )
    return root


def test_end_times_per_class(tmp_path):
    ends = shift.snapshot_end_times(load_snapshot(_scenario(tmp_path)))
    assert ends == {
        "metrics": _ns("2025-12-15 17:58:00"),
        "traces": _ns("2025-12-15 17:59:50"),  # span end, not start
        "alerts": shift.parse_rfc3339_ns("2025-12-15T17:50:00.123456789Z"),
        "objects": _ns("2025-12-15 17:55:00.5"),  # body's future value ignored
        "events": _ns("2025-12-15 17:57:10"),  # event time inside the body counts
        "logs": _ns("2025-12-15 17:56:00.987654321"),
    }


def test_offset_puts_the_latest_end_a_minute_before_delivery(tmp_path):
    ends = shift.snapshot_end_times(load_snapshot(_scenario(tmp_path)))
    offset = shift.compute_offset_seconds(ends, DELIVERY)
    latest = shift.latest_shifted_ns(ends, offset)
    target = shift.to_ns(DELIVERY - timedelta(seconds=60))
    assert target - NS < latest <= target
    shift.check_not_future(ends, offset, DELIVERY)
    for value in ends.values():
        assert value + offset * NS <= target


def test_offset_rounds_down_on_fractional_ends():
    ends = {"metrics": _ns("2025-12-15 17:00:00.4"), "logs": None}
    delivery = datetime(2025, 12, 15, 18, 1, 0, tzinfo=UTC)
    assert shift.compute_offset_seconds(ends, delivery) == 3599
    with pytest.raises(ValueError):
        shift.check_not_future(ends, 3601, delivery - timedelta(seconds=60))
    with pytest.raises(ValueError):
        shift.compute_offset_seconds({"metrics": None}, delivery)
    with pytest.raises(ValueError):
        shift.compute_offset_seconds(ends, datetime(2025, 12, 15, 18, 1))


def test_every_converted_class_stays_before_delivery(tmp_path):
    snap = load_snapshot(_scenario(tmp_path))
    offset = shift.compute_offset_seconds(shift.snapshot_end_times(snap), DELIVERY)
    limit = shift.to_ns(DELIVERY)
    buf = io.StringIO()
    stats = convert_metrics(snap, buf, offset_seconds=offset)
    assert stats.end_ms * 1_000_000 <= limit
    for body in iter_otlp_requests(snap, offset_seconds=offset):
        for rs in body["resourceSpans"]:
            for span in rs["scopeSpans"][0]["spans"]:
                assert int(span["endTimeUnixNano"]) <= limit
                assert all(int(e["timeUnixNano"]) <= limit for e in span["events"])
    hook = entry_webhook(
        snap, offset_seconds=offset, target=EntryTarget({"service_name": "checkout"})
    )
    assert shift.parse_rfc3339_ns(hook["alerts"][0]["startsAt"]) <= limit
    for kind in ("objects", "events", "logs"):
        for row in snap.iter_tsv(snap.tsv_path(kind)):
            moved = (
                shift.shift_object_row(row, offset)
                if kind != "logs"
                else shift.shift_log_row(row, offset)
            )
            assert _ns(moved["Timestamp"]) == _ns(row["Timestamp"]) + offset * NS
            assert _ns(moved["TimestampTime"]) <= limit


def test_object_rows_shift_body_times_and_log_rows_do_not():
    row = {
        "Timestamp": "2025-12-15 17:55:00.500",
        "TimestampTime": "2025-12-15 17:55:00",
        "Body": json.dumps(
            {
                "metadata": {"t": "2025-12-15T17:00:00Z", "n": "2025-12-15"},
                "x": [None, "0001-01-01T00:00:00Z"],
            }
        ),
    }
    moved = shift.shift_object_row(row, 86400)
    assert moved["Timestamp"] == "2025-12-16 17:55:00.500"
    assert moved["TimestampTime"] == "2025-12-16 17:55:00"
    assert json.loads(moved["Body"]) == {
        "metadata": {"t": "2025-12-16T17:00:00Z", "n": "2025-12-15"},
        "x": [None, "0001-01-01T00:00:00Z"],
    }
    log = shift.shift_log_row({**row, "Body": "at 2025-12-15T17:00:00Z"}, 86400)
    assert log["Body"] == "at 2025-12-15T17:00:00Z"
    assert row["Timestamp"] == "2025-12-15 17:55:00.500"  # input untouched


@pytest.mark.parametrize(
    ("text", "offset", "expected"),
    [
        ("2025-12-15T17:30:00Z", 3600, "2025-12-15T18:30:00Z"),
        ("2025-12-15T23:59:59.123456789Z", 2, "2025-12-16T00:00:01.123456789Z"),
        ("2025-12-15T17:30:00+02:00", -60, "2025-12-15T17:29:00+02:00"),
        ("2025-12-15t17:30:00z", 1, "2025-12-15t17:30:01z"),
        ("0001-01-01T00:00:00Z", 3600, "0001-01-01T00:00:00Z"),
    ],
)
def test_rfc3339_shift_keeps_format(text, offset, expected):
    assert shift.shift_rfc3339(text, offset) == expected


@pytest.mark.parametrize(
    ("text", "offset", "expected"),
    [
        ("2025-12-15 23:59:30.000001", 31, "2025-12-16 00:00:01.000001"),
        ("2025-12-15 17:30:00", -1, "2025-12-15 17:29:59"),
        ("2025-12-15T17:30:00.5Z", 1, "2025-12-15T17:30:01.5Z"),
    ],
)
def test_clickhouse_shift_keeps_format(text, offset, expected):
    assert shift.shift_clickhouse_timestamp(text, offset) == expected
    assert _ns(expected) == _ns(text) + offset * NS
