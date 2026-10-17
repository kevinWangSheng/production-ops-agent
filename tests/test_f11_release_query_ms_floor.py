"""Regression for r8 B01 (``docs/tasks/2026-10-13-f11-pr2-contract.md``).

Observed in the U2 probe: Prometheus rounds the evaluation time to the
millisecond, so for a window end T whose microsecond part is >= 500 the
returned sample timestamp (e.g. ``...416.744`` for ``...416.743743``) is later
than T, and the release evaluation's strict "sample not after evaluated_at"
check turns the whole query into ``QUERY_FAILED``.

The fake below behaves like that server: it reads the ``time`` parameter of
the request and stamps its sample with that time rounded half-up to the
millisecond. With the r8 millisecond floor the sample is never after T; the
reverse control shows the same fake does break the check when the request
carries microseconds (the pre-r8 behavior), so the test can catch the defect.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest

from opspilot.observation.release_types import ReleaseQueryResult
from opspilot.observer.prometheus import PrometheusReadOnlySource
from opspilot.observer.release_evaluation import parse_vector
from tests.f11_release_support import sha256_hex
from tests.test_f11_pr2b_acceptance import FakeResponse, _exact_epoch

BASE = datetime(2026, 10, 17, 12, 0, 16, tzinfo=timezone.utc)
#: microsecond parts >= 500 inside the millisecond: Prometheus rounds them up.
ROUNDING_UP_MICROS = [743743, 500, 500500, 999500, 999999]
LABELS = {"__name__": "up", "job": "checkout"}
QUERY_KEY = "after.checkout.up"


def _prometheus_rounded(time_param: str) -> Decimal:
    return Decimal(time_param).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


class RoundingPrometheus:
    """A fake opener that answers like Prometheus: the sample timestamp is the
    request's ``time`` rounded to the millisecond."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def body_for(self, time_param: str) -> bytes:
        stamp = float(_prometheus_rounded(time_param))
        return json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": LABELS, "value": [stamp, "1"]}],
                },
            }
        ).encode()

    def open(self, request: Any, timeout: Any = None) -> FakeResponse:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        (time_param,) = query["time"]
        self.sent.append(time_param)
        return FakeResponse(self.body_for(time_param))


def _release_result(
    raw: bytes, *, at: datetime, status: str = "ok", http_status: int | None = 200
) -> ReleaseQueryResult:
    return ReleaseQueryResult(
        query_key=QUERY_KEY,
        query="up",
        source="prometheus",
        evaluated_at=at,
        sent_at=at,
        status=status,
        http_status=http_status,
        detail="",
        body_complete=True,
        raw=raw,
        raw_sha256=sha256_hex(raw),
    )


def _failed(reasons: set[str]) -> list[str]:
    return [r for r in reasons if r.startswith("QUERY_FAILED")]


@pytest.mark.parametrize("micro", ROUNDING_UP_MICROS)
def test_a_server_rounded_sample_at_a_sub_millisecond_window_end_is_not_query_failed(
    micro: int,
) -> None:
    at = BASE.replace(microsecond=micro)
    server = RoundingPrometheus()
    source = PrometheusReadOnlySource(
        "http://127.0.0.1:19090/", token="obs-token", opener=server
    )

    vector = source.instant_vector("up", at=at, timeout_seconds=5)

    assert vector.status == "ok", (vector.status, vector.detail, server.sent)
    reasons: set[str] = set()
    parsed = parse_vector(
        _release_result(vector.body, at=at, http_status=vector.http_status), reasons
    )
    assert not _failed(reasons), (server.sent, reasons)
    assert parsed, (server.sent, reasons)
    (labels, stamp, value) = parsed[0]
    assert (labels, value) == (LABELS, 1.0)
    assert Decimal(repr(stamp)) <= _exact_epoch(at), (server.sent, stamp)


@pytest.mark.parametrize("micro", ROUNDING_UP_MICROS)
def test_control_a_microsecond_time_parameter_makes_the_same_fake_fail_the_query(
    micro: int,
) -> None:
    """Pre-r8 behavior, built without the implementation: sending T with its
    microseconds makes the rounding server stamp the sample after T."""
    at = BASE.replace(microsecond=micro)
    exact = _exact_epoch(at)
    server = RoundingPrometheus()
    url = "http://127.0.0.1:19090/api/v1/query?" + urllib.parse.urlencode(
        {"query": "up", "time": f"{exact:.6f}", "timeout": "5s"}
    )

    raw = server.open(urllib.request.Request(url)).read()

    assert _prometheus_rounded(server.sent[0]) > exact
    reasons: set[str] = set()
    assert parse_vector(_release_result(raw, at=at), reasons) == []
    assert _failed(reasons) == ["QUERY_FAILED:" + QUERY_KEY]
