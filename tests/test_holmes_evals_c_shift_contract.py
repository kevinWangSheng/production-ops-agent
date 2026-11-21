"""#666 PR-C contract: whole-day shift of the three fixed-time Loki questions
(R10-1, lead 2026-11-19), English month dates in question rewrite and
deshift, ``LokiAdmin`` and ``apply_loki_shift`` against an in-process Loki.

Independent contract tests written from the task record's "PR-C 接口"
section, without reading the PR-C implementation. Every clock is fixed:
nothing depends on today's date (#726 lesson).

Rules under test:

* ``LOKI_FIXED_TIME`` = 100a, 101, 259_loki; ``FIXED_TIME`` stays B1's 18;
* ``plan_day_shift_seconds``: whole days, the largest ``k`` with the
  incident hour ending at least ``LEAD`` before now, the incident hour
  inside the 24 h box at delivery (``SHIFT_DAY_UNAVAILABLE``), ``k <= 0`` ->
  ``SHIFT_NOT_NEEDED``;
* ``seconds_to_wait_for_shift_day``: 0 outside the infeasible daily window
  and for every other question; otherwise a wait to the earliest instant
  from which ``now .. now + setup_timeout`` is feasible; over
  ``SHIFT_WAIT_FOR_DAY``'s cap -> ``DeterministicError``;
* ``rewrite_question`` rewrites ``<Month|Mon> <D>[,] <YYYY> [at|around|,]
  HH:MM [UTC]`` in its own wording; time-only phrases stay; ISO unchanged;
* ``text.deshift`` reverses English dates in range; B1-style text without
  English dates deshifts as before;
* ``shift_line`` moves ISO stamps inside a log line;
* ``LokiAdmin``: no credentials, forward pages of at most 5000, dedupe,
  ``SHIFT_PAGE_STUCK``, ``SHIFT_TOO_MANY_ENTRIES``, push batches of at most
  5000 entries / 4 MiB, ``SHIFT_IO_FAILED``;
* ``apply_loki_shift``: fixed entries are ``[setup_started - 730 d,
  setup_started - 24 h)``, shifted with their in-line stamps, entries later
  than ``now - LEAD`` dropped, pushed back under the same labels, originals
  kept, live entries untouched; ``SHIFT_NO_FIXED_ENTRIES``,
  ``SHIFT_NO_QUESTION_TIME``; the record's new fields;
* ``shift_question`` dispatches by backend; VL and relative Loki questions
  are not shifted (``RELATIVE_TIME``);
* B1 ``ShiftRecord.as_json()`` keeps exactly its PR-B keys.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``seconds_to_wait_for_shift_day`` lives in ``shift`` or ``driver``.
* ``LokiAdmin.label_names``/``entries`` take aware ``datetime`` bounds.
* ``question_instants`` on text without a time either returns ``[]`` or
  raises ``SHIFT_NO_QUESTION_TIME``; ``apply_loki_shift`` raises it.
* ``line_stamps_rewritten`` counts rewritten stamps of at least the pushed
  entries and at most all fixed entries.
* A Loki ``ShiftRecord`` lists ``streams`` as ``{"labels", "entries",
  "pushed"}`` with ``entries`` = fixed entries of that stream.
* Exact boundary instants of the infeasible window are not tested.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from tests.holmes_evals_b_support import no_proxy_env  # noqa: F401 - fixture
from tests.holmes_evals_c_support import (
    PROMPT_FIXED,
    Q100A,
    Q101,
    Q102,
    Q257,
    Q258,
    Q259L,
    FakeLoki,
    at_ns,
    find,
    free_port,
    ns_of,
    served_loki,
)
from tests.holmes_evals_support import mod

pytestmark = pytest.mark.usefixtures("no_proxy_env")

ANCHOR = datetime(2025, 8, 2, 13, 45, tzinfo=UTC)
NOW = datetime(2026, 11, 19, 17, 0, 0, tzinfo=UTC)
K = (date(2026, 11, 19) - date(2025, 8, 2)).days  # 474
DAY = 86400
PROMPT = PROMPT_FIXED.format(ns="app-101")


def _code(fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(Exception) as caught:  # noqa: PT011 - code checked
        fn(*args, **kwargs)
    return str(getattr(caught.value, "code", caught.value))


def _wait() -> Any:
    # ASSUMPTION: module not fixed by the interface.
    return find("seconds_to_wait_for_shift_day", "shift", "driver")


# -- constants -----------------------------------------------------------------------


def test_constants() -> None:
    shift = mod("shift")
    assert set(shift.LOKI_FIXED_TIME) == {Q100A, Q101, Q259L}
    assert shift.INCIDENT_SPAN == timedelta(hours=1)
    assert shift.BOX_MARGIN == timedelta(minutes=10)
    assert shift.SHIFT_DAY_WAIT_CAP == timedelta(hours=2)
    assert shift.FIXED_ENTRY_AGE == timedelta(hours=24)
    assert shift.LEAD == timedelta(seconds=60)
    assert not set(shift.LOKI_FIXED_TIME) & set(shift.FIXED_TIME)


# -- anchor --------------------------------------------------------------------------


def test_question_instants_reads_english_dates() -> None:
    shift = mod("shift")
    got = shift.question_instants(PROMPT)
    assert [m.astimezone(UTC) for m in got] == [ANCHOR]
    two = shift.question_instants(
        "Errors on Aug 3, 2025 at 09:10 UTC and earlier on August 2, 2025 "
        "around 13:45 UTC."
    )
    assert min(m.astimezone(UTC) for m in two) == ANCHOR
    assert len(two) == 2


def test_question_instants_without_time() -> None:
    shift = mod("shift")
    try:
        got = shift.question_instants("What happened to payment-api in app-101?")
    except shift.ShiftError as error:
        assert error.code == "SHIFT_NO_QUESTION_TIME"
    else:
        assert got == []


# -- whole-day plan ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "days"),
    [
        (NOW, K),  # 17:00: today's 13:45 hour has ended
        (datetime(2026, 11, 19, 14, 50, tzinfo=UTC), K),
        (datetime(2026, 11, 19, 13, 0, tzinfo=UTC), K - 1),  # yesterday's
        (datetime(2026, 11, 19, 3, 0, tzinfo=UTC), K - 1),
        (datetime(2026, 11, 19, 23, 59, tzinfo=UTC), K),
    ],
)
def test_plan_day_shift_seconds_whole_days(now: datetime, days: int) -> None:
    shift = mod("shift")
    seconds = shift.plan_day_shift_seconds(ANCHOR, now)
    assert seconds == days * DAY
    assert seconds % DAY == 0
    incident = ANCHOR + timedelta(seconds=seconds)
    assert incident.time() == ANCHOR.time()  # time of day unchanged
    # The incident hour ends at least LEAD before now ...
    assert incident + shift.INCIDENT_SPAN <= now - shift.LEAD
    # ... and still sits inside the 24 h box when delivered by the void line.
    void = timedelta(seconds=mod("driver").VOID_SECONDS)
    assert incident >= now + void + shift.BOX_MARGIN - timedelta(hours=24)
    # The largest such k: one more day would end after now.
    assert incident + timedelta(days=1) + shift.INCIDENT_SPAN > now - shift.LEAD


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 11, 19, 13, 40, tzinfo=UTC),
        datetime(2026, 11, 19, 14, 0, tzinfo=UTC),
        datetime(2026, 11, 19, 14, 40, tzinfo=UTC),
    ],
)
def test_plan_day_shift_unavailable_in_daily_window(now: datetime) -> None:
    shift = mod("shift")
    with pytest.raises(shift.ShiftError) as caught:
        shift.plan_day_shift_seconds(ANCHOR, now)
    assert caught.value.code == "SHIFT_DAY_UNAVAILABLE"


def test_plan_day_shift_not_needed() -> None:
    shift = mod("shift")
    recent = NOW - timedelta(hours=2)
    with pytest.raises(shift.ShiftError) as caught:
        shift.plan_day_shift_seconds(recent, NOW)
    assert caught.value.code == "SHIFT_NOT_NEEDED"


# -- waiting for a feasible time of day ------------------------------------------------


def _feasible(at: datetime) -> bool:
    shift = mod("shift")
    try:
        shift.plan_day_shift_seconds(ANCHOR, at)
    except shift.ShiftError:
        return False
    return True


@pytest.mark.parametrize(
    ("now", "timeout_s"),
    [
        (datetime(2026, 11, 19, 10, 0, tzinfo=UTC), 600),
        (datetime(2026, 11, 19, 13, 20, tzinfo=UTC), 600),  # done by 13:30
        (datetime(2026, 11, 19, 13, 25, tzinfo=UTC), 60),
        (datetime(2026, 11, 19, 15, 0, tzinfo=UTC), 600),
        (datetime(2026, 11, 19, 23, 58, tzinfo=UTC), 600),
    ],
)
def test_no_wait_when_feasible_through_setup(now: datetime, timeout_s: int) -> None:
    for qid in (Q100A, Q101, Q259L):
        assert _wait()(qid, now, timeout_s) == 0


@pytest.mark.parametrize(
    ("now", "timeout_s", "until"),
    [
        # Inside the window: wait until the incident hour of today is over.
        (datetime(2026, 11, 19, 14, 0, tzinfo=UTC), 600, (14, 46)),
        (datetime(2026, 11, 19, 13, 40, tzinfo=UTC), 180, (14, 46)),
        # Feasible now but not for the whole setup (setup_timeout counts).
        (datetime(2026, 11, 19, 13, 25, tzinfo=UTC), 600, (14, 46)),
    ],
)
def test_wait_until_feasible(
    now: datetime, timeout_s: int, until: tuple[int, int]
) -> None:
    wait = _wait()(Q100A, now, timeout_s)
    earliest = datetime(2026, 11, 19, *until, tzinfo=UTC)
    assert wait >= (earliest - now).total_seconds() - 1
    assert wait <= (earliest - now).total_seconds() + 600  # small margin allowed
    ready = now + timedelta(seconds=wait)
    for step in range(0, timeout_s + 1, 30):
        assert _feasible(ready + timedelta(seconds=step))
    assert wait < mod("shift").SHIFT_DAY_WAIT_CAP.total_seconds()


def test_wait_other_questions_zero() -> None:
    inside = datetime(2026, 11, 19, 14, 0, tzinfo=UTC)
    for qid in (
        Q102,
        Q257,
        Q258,
        "282_loki_custom_stream_label",
        "275_es_field_name_correction",
        "254_elasticsearch_dr_test_log_check",
    ):
        assert _wait()(qid, inside, 600) == 0


def test_wait_over_cap_is_deterministic() -> None:
    driver = mod("driver")
    # A 3 h setup estimate makes 10:33..14:46 infeasible: from 12:00 the
    # wait would be 2 h 46 min, over the 2 h cap.
    with pytest.raises(driver.DeterministicError) as caught:
        _wait()(Q100A, datetime(2026, 11, 19, 12, 0, tzinfo=UTC), 3 * 3600)
    assert caught.value.code == "SHIFT_WAIT_FOR_DAY"


# -- question rewrite and deshift --------------------------------------------------------


def test_rewrite_english_date_whole_days() -> None:
    shift = mod("shift")
    text = PROMPT + " It recovered at 14:10."
    out, rewrites = shift.rewrite_question(text, K * DAY)
    assert out == (
        "The payment-api pod in namespace app-101 had issues on November 19, 2026 "
        "around 13:45 UTC. What happened? It recovered at 14:10."
    )
    (r,) = rewrites
    assert r.original == "August 2, 2025 around 13:45 UTC"
    assert r.rewritten == "November 19, 2026 around 13:45 UTC"
    assert r.instant.astimezone(UTC) == datetime(2026, 11, 19, 13, 45, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Aug 2, 2025 at 13:45", "Nov 19, 2026 at 13:45"),
        ("August 2 2025, 13:45:30 UTC", "November 19 2026, 13:45:30 UTC"),
        ("on August 2, 2025.", "on November 19, 2026."),
    ],
)
def test_rewrite_english_date_keeps_wording(text: str, expected: str) -> None:
    out, rewrites = mod("shift").rewrite_question(text, K * DAY)
    assert out == expected
    assert len(rewrites) == 1


def test_rewrite_english_date_with_sub_day_offset_moves_time() -> None:
    out, _ = mod("shift").rewrite_question(
        "issues on August 2, 2025 around 13:45 UTC", 90 * 60
    )
    assert out == "issues on August 2, 2025 around 15:15 UTC"


def test_rewrite_iso_unchanged() -> None:
    shift = mod("shift")
    out, rewrites = shift.rewrite_question(
        "Between 2026-05-14T12:00:00Z and 2026-05-14T12:03:00Z, stopped at 12:02.",
        3600,
    )
    assert out == (
        "Between 2026-05-14T13:00:00Z and 2026-05-14T13:03:00Z, stopped at 12:02."
    )
    assert len(rewrites) == 2


def _loki_shift() -> Any:
    text = mod("text")
    return text.Shift(
        K * DAY,
        datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
        datetime(2026, 11, 19, 16, 0, tzinfo=UTC),
    )


def test_deshift_english_dates_in_range() -> None:
    text = mod("text")
    report = (
        "payment-api failed on November 19, 2026 at 13:50 UTC; again Nov 19, 2026 "
        "around 15:02. Memory was fine on November 19, 2026. Restarted at 14:10. "
        "Unrelated: March 3, 2026 at 10:00 UTC."
    )
    out, count = text.deshift(report, _loki_shift())
    assert out == (
        "payment-api failed on August 2, 2025 at 13:50 UTC; again Aug 2, 2025 "
        "around 15:02. Memory was fine on August 2, 2025. Restarted at 14:10. "
        "Unrelated: March 3, 2026 at 10:00 UTC."
    )
    assert count == 3


def test_rewrite_then_deshift_round_trips() -> None:
    shift, text = mod("shift"), mod("text")
    out, _ = shift.rewrite_question(PROMPT, K * DAY)
    back, count = text.deshift(out, _loki_shift())
    assert back == PROMPT and count == 1


def test_b1_text_without_english_dates_deshifts_as_before() -> None:
    text = mod("text")
    seconds = 5 * 3600 + 7 * 60
    s = text.Shift(
        seconds,
        datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
        datetime(2026, 11, 18, 10, 0, tzinfo=UTC),
    )
    report = (
        "In May the index rotated. First ERROR at 2026-11-18T09:30:00Z, last at "
        "2026-11-18T09:59:00Z; on Monday 2026-01-05T08:00:00Z was quiet. 3 errors "
        "in 10:00-11:00."
    )
    out, count = text.deshift(report, s)
    assert out == (
        "In May the index rotated. First ERROR at 2026-11-18T04:23:00Z, last at "
        "2026-11-18T04:52:00Z; on Monday 2026-01-05T08:00:00Z was quiet. 3 errors "
        "in 10:00-11:00."
    )
    assert count == 2
    # An English date out of range is left alone (B1 differs only in range).
    same, n = text.deshift("Seen on March 3, 2026 at 10:00 UTC.", s)
    assert same == "Seen on March 3, 2026 at 10:00 UTC." and n == 0


# -- log lines -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected", "count"),
    [
        (
            '{"timestamp":"2025-08-02T13:45:10Z","level":"error","msg":"refused"}',
            '{"timestamp":"2026-11-19T13:45:10Z","level":"error","msg":"refused"}',
            1,
        ),
        (
            "2025-08-02T13:45:10.123Z start 2025-08-02T13:46:00Z end",
            "2026-11-19T13:45:10.123Z start 2026-11-19T13:46:00Z end",
            2,
        ),
        ("connection refused to db:5432", "connection refused to db:5432", 0),
    ],
)
def test_shift_line(line: str, expected: str, count: int) -> None:
    assert mod("shift").shift_line(line, K * DAY) == (expected, count)


# -- LokiAdmin ---------------------------------------------------------------------------

T0 = datetime(2025, 8, 2, 12, 0, tzinfo=UTC)


def _admin(url: str) -> Any:
    return mod("shift").LokiAdmin(url)


def _query_requests(store: FakeLoki) -> list[dict[str, Any]]:
    return [r for r in store.requests if r["path"].endswith("/query_range")]


def test_loki_entry_is_frozen() -> None:
    entry = mod("shift").LokiEntry(labels={"app": "a"}, ns=1, line="x")
    with pytest.raises(Exception):  # noqa: B017, PT011 - frozen dataclass
        entry.ns = 2  # type: ignore[misc]


def test_loki_admin_pages_forward_and_dedupes() -> None:
    store = FakeLoki()
    base = ns_of(T0)
    store.add({"app": "pay", "namespace": "app-101"},
              [(base + i * 1000, f"pay {i}") for i in range(3000)])  # fmt: skip
    store.add({"app": "db"}, [(base + i * 1000, f"db {i}") for i in range(3000)])
    store.add({"app": "late"}, [(ns_of(T0 + timedelta(days=3)), "outside")])
    with served_loki(store) as url:
        admin = _admin(url)
        names = admin.label_names(T0 - timedelta(hours=1), T0 + timedelta(days=1))
        assert {"app", "namespace"} <= set(names)
        got = admin.entries(T0 - timedelta(hours=1), T0 + timedelta(days=1))
    keys = [(tuple(sorted(e.labels.items())), e.ns, e.line) for e in got]
    assert len(keys) == len(set(keys)) == 6000  # pay under two names: once
    assert not [e for e in got if e.line == "outside"]
    pay = [e for e in got if e.labels.get("app") == "pay"]
    assert all(dict(e.labels) == {"app": "pay", "namespace": "app-101"} for e in pay)
    queries = _query_requests(store)
    assert queries
    for r in queries:
        assert r["query"]["direction"] == ["forward"]
        assert int(r["query"]["limit"][0]) <= 5000
    assert len(queries) >= 3  # 6000 rows across selectors need a second page
    for r in store.requests:
        assert "authorization" not in r["headers"]


def test_loki_admin_page_stuck() -> None:
    store = FakeLoki()
    same = ns_of(T0)
    store.add({"app": "burst"}, [(same, f"line {i}") for i in range(5001)])
    with served_loki(store) as url:
        code = _code(
            _admin(url).entries, T0 - timedelta(hours=1), T0 + timedelta(hours=1)
        )
    assert code == "SHIFT_PAGE_STUCK"


def test_loki_admin_too_many_entries() -> None:
    store = FakeLoki()
    store.add_synthetic({"job": "big"}, ns_of(T0), 200_001, step=1_000_000)
    with served_loki(store) as url:
        code = _code(
            _admin(url).entries, T0 - timedelta(hours=1), T0 + timedelta(days=1)
        )
    assert code == "SHIFT_TOO_MANY_ENTRIES"


def test_loki_admin_push_batches() -> None:
    shift = mod("shift")
    store = FakeLoki()
    base = ns_of(T0)
    many = [
        shift.LokiEntry(labels={"app": "pay"}, ns=base + i, line=f"m{i}")
        for i in range(12_001)
    ]
    big = [
        shift.LokiEntry(labels={"app": "big"}, ns=base + i, line="x" * 300_000 + str(i))
        for i in range(30)
    ]
    with served_loki(store) as url:
        admin = _admin(url)
        admin.push(many)
        admin.push(big)
    pushes = [r for r in store.requests if r["path"] == "/loki/api/v1/push"]
    assert len(pushes) >= 3 + 3
    for r in pushes:
        assert r.get("values", 0) <= 5000
        assert r["body_bytes"] <= 4 * 1024 * 1024
        assert "authorization" not in r["headers"]
    assert sum(r.get("values", 0) for r in pushes) == 12_031
    assert len(store.entries({"app": "pay"})) == 12_001
    assert len(store.entries({"app": "big"})) == 30


def test_loki_admin_push_transport_failure() -> None:
    shift = mod("shift")
    admin = _admin(f"http://127.0.0.1:{free_port()}")
    entry = shift.LokiEntry(labels={"app": "pay"}, ns=ns_of(T0), line="x")
    assert _code(admin.push, [entry]) == "SHIFT_IO_FAILED"


# -- apply_loki_shift ---------------------------------------------------------------------

SETUP_STARTED = NOW - timedelta(minutes=5)
S1 = {"app": "payment-api", "namespace": "app-101"}
S2 = {"app": "db", "namespace": "app-101"}
S3 = {"app": "payment-api", "kind": "live"}


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_101(store: FakeLoki) -> None:
    hourly = [T0 + timedelta(hours=h) for h in range(49)]  # to 2025-08-04 12:00
    store.add(
        S1,
        [
            (ns_of(m), json.dumps({"timestamp": _stamp(m), "level": "error", "msg": f"m{i}"}))
            for i, m in enumerate(hourly)
        ],
    )  # fmt: skip
    ancient = SETUP_STARTED - timedelta(days=800)
    store.add(S1, [(ns_of(ancient), "ancient entry")])
    store.add(
        S2,
        [
            (ns_of(datetime(2025, 8, 2, 13, 40, tzinfo=UTC)), "db slow 1"),
            (ns_of(datetime(2025, 8, 2, 13, 50, tzinfo=UTC)), "db slow 2"),
            (ns_of(datetime(2025, 8, 3, 1, 0, tzinfo=UTC)), "db slow 3"),
        ],
    )
    live = [SETUP_STARTED + timedelta(seconds=s) for s in (10, 20)]
    store.add(S3, [(ns_of(m), f"live {m.isoformat()}") for m in live])


def _as_utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(UTC)


def test_apply_loki_shift_end_to_end() -> None:
    shift = mod("shift")
    store = FakeLoki()
    _seed_101(store)
    before_live = store.entries(S3)
    with served_loki(store) as url:
        delivered, record = shift.apply_loki_shift(
            _admin(url), Q101, PROMPT, NOW, setup_started=SETUP_STARTED
        )
    seconds = K * DAY
    assert delivered == shift.rewrite_question(PROMPT, seconds)[0]
    assert "November 19, 2026 around 13:45 UTC" in delivered
    assert record.applied is True
    assert record.seconds == seconds
    assert record.backend == "loki"
    assert record.anchor_source == "question"
    assert _as_utc(record.anchor) == ANCHOR
    # 49 + 3 fixed (the 800-day-old entry is outside the 730-day read window).
    assert record.fixed_entries == 52
    # Kept: S1 12:00..16:00 (5), S2 13:40/13:50 (2); later ones end after now-60s.
    assert record.pushed == 7
    assert record.dropped_future == 45
    assert 5 <= record.line_stamps_rewritten <= 49
    streams = {
        json.dumps(dict(s["labels"]), sort_keys=True): (s["entries"], s["pushed"])
        for s in record.streams
        if s["entries"]  # a stream without fixed entries may be listed or not
    }
    assert streams == {
        json.dumps(S1, sort_keys=True): (49, 5),
        json.dumps(S2, sort_keys=True): (3, 2),
    }
    assert _as_utc(record.original_min) == T0
    assert _as_utc(record.original_max) == T0 + timedelta(hours=4)
    assert _as_utc(record.shifted_min) == T0 + timedelta(seconds=seconds)
    assert _as_utc(record.shifted_max) == T0 + timedelta(hours=4, seconds=seconds)

    # The store: shifted copies under the same labels, originals kept.
    limit = ns_of(NOW - shift.LEAD)
    s1 = store.entries(S1)
    new_s1 = [
        (ns, line) for ns, line in s1 if ns >= ns_of(datetime(2026, 1, 1, tzinfo=UTC))
    ]
    assert len(new_s1) == 5
    for ns, line in new_s1:
        assert ns <= limit
        assert json.loads(line)["timestamp"] == _stamp(at_ns(ns))  # stamp moved too
    assert (
        len([1 for ns, _ in s1 if ns < ns_of(datetime(2026, 1, 1, tzinfo=UTC))]) == 50
    )
    new_s2 = [
        line
        for ns, line in store.entries(S2)
        if ns >= ns_of(datetime(2026, 1, 1, tzinfo=UTC))
    ]
    assert sorted(new_s2) == ["db slow 1", "db slow 2"]
    assert store.entries(S3) == before_live  # live entries untouched
    for _, ns, _ in store.all_entries():
        assert ns <= limit
    assert len(store.streams) == 3  # no new label sets
    for r in store.requests:
        assert "authorization" not in r["headers"]

    out = record.as_json()
    for key in ("backend", "anchor_source", "fixed_entries", "pushed",
                "dropped_future", "line_stamps_rewritten", "streams"):  # fmt: skip
        assert key in out
    assert out["backend"] == "loki" and out["seconds"] == seconds
    s = record.as_text_shift()
    assert s.seconds == seconds
    assert _as_utc(s.shifted_min) == _as_utc(record.shifted_min)


def _span_ns(r: dict[str, Any]) -> int:
    return int(r["query"]["end"][0]) - int(r["query"]["start"][0])


def test_apply_loki_shift_reads_within_loki_query_length() -> None:
    """Loki 2.9 rejects a labels/series/query_range span over 30d1h (HTTP
    400), and its frontend runs every query_range as 30-minute sub-queries, so
    on the fixtures' 50m-CPU Loki a request costs by span (about 5 s a day,
    60 s timeout). The fixed-time read stays near the anchor, reads each
    stream under one selector, and still sees every entry the questions'
    generators write (2025-08-01 00:00 to 2025-08-04 14:00)."""
    shift = mod("shift")
    store = FakeLoki()
    store.max_query_ns = int(timedelta(days=30, hours=1).total_seconds()) * 10**9
    _seed_101(store)
    first = datetime(2025, 8, 1, 0, 0, tzinfo=UTC)
    last = datetime(2025, 8, 4, 13, 59, tzinfo=UTC)
    store.add(S2, [(ns_of(first), "db first"), (ns_of(last), "db last")])
    with served_loki(store) as url:
        _, record = shift.apply_loki_shift(
            _admin(url), Q101, PROMPT, NOW, setup_started=SETUP_STARTED
        )
    reads = [
        r for r in store.requests
        if r["path"] in ("/loki/api/v1/labels", "/loki/api/v1/series",
                         "/loki/api/v1/query_range")
    ]  # fmt: skip
    assert reads
    assert all(_span_ns(r) <= store.max_query_ns for r in reads)
    queries = _query_requests(store)
    day = int(timedelta(days=1).total_seconds()) * 10**9
    assert all(_span_ns(r) <= day for r in queries)  # each well under the timeout
    # One selector covers all three streams (all carry ``app``): at most a
    # week of query_range in total, not 730 days per label name.
    assert len({r["query"]["query"][0] for r in queries}) == 1
    assert sum(_span_ns(r) for r in queries) <= 7 * day
    # 52 from the end-to-end seed plus the two generator-edge entries, once each.
    assert record.fixed_entries == 54
    assert _as_utc(record.original_min) == first


@pytest.mark.parametrize("offset", [timedelta(hours=-63), timedelta(hours=63)])
def test_apply_loki_shift_data_at_read_edge_fails_closed(offset: timedelta) -> None:
    """Fixed data near the edge of the anchor read may continue past it: the
    shift fails instead of moving part of the timeline, and pushes nothing."""
    shift = mod("shift")
    store = FakeLoki()
    _seed_101(store)
    store.add(S2, [(ns_of(ANCHOR + offset), "db near edge")])
    with served_loki(store) as url:
        code = _code(
            shift.apply_loki_shift, _admin(url), Q101, PROMPT, NOW,
            setup_started=SETUP_STARTED,
        )  # fmt: skip
    assert code == "SHIFT_DATA_AT_READ_EDGE"
    assert not [r for r in store.requests if r["path"].endswith("/push")]


def test_apply_loki_shift_no_fixed_entries() -> None:
    shift = mod("shift")
    store = FakeLoki()
    store.add(S3, [(ns_of(SETUP_STARTED + timedelta(seconds=5)), "live only")])
    with served_loki(store) as url:
        code = _code(
            shift.apply_loki_shift, _admin(url), Q101, PROMPT, NOW,
            setup_started=SETUP_STARTED,
        )  # fmt: skip
    assert code == "SHIFT_NO_FIXED_ENTRIES"


def test_apply_loki_shift_no_question_time() -> None:
    shift = mod("shift")
    store = FakeLoki()
    _seed_101(store)
    with served_loki(store) as url:
        code = _code(
            shift.apply_loki_shift, _admin(url), Q101,
            "The payment-api pod in namespace app-101 had issues. What happened?",
            NOW, setup_started=SETUP_STARTED,
        )  # fmt: skip
    assert code == "SHIFT_NO_QUESTION_TIME"
    assert not [r for r in store.requests if r["path"].endswith("/push")]


def test_apply_loki_shift_day_unavailable_writes_nothing() -> None:
    shift = mod("shift")
    store = FakeLoki()
    _seed_101(store)
    inside = datetime(2026, 11, 19, 14, 0, tzinfo=UTC)
    with served_loki(store) as url:
        code = _code(
            shift.apply_loki_shift, _admin(url), Q101, PROMPT, inside,
            setup_started=inside - timedelta(minutes=5),
        )  # fmt: skip
    assert code == "SHIFT_DAY_UNAVAILABLE"
    assert not [r for r in store.requests if r["path"].endswith("/push")]


# -- dispatch ----------------------------------------------------------------------------


class _Untouchable:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"admin used: {name}")


@pytest.mark.parametrize(
    ("backend", "qid", "question"),
    [
        ("victorialogs", Q258, "Which services logged in the last 5 minutes?"),
        ("victorialogs", Q257, "Find errors on August 2, 2025 around 13:45 UTC."),
        ("loki", Q102, "Fetch me the logs for the inventory-service pod"),
        ("elasticsearch", "254_elasticsearch_dr_test_log_check", "Any DR logs?"),
    ],
)
def test_shift_question_relative_untouched(
    backend: str, qid: str, question: str
) -> None:
    delivered, record = mod("shift").shift_question(
        backend, es_admin=_Untouchable(), loki_admin=_Untouchable(), qid=qid,
        question=question, now=NOW, setup_started=SETUP_STARTED,
    )  # fmt: skip
    assert delivered == question
    assert record.applied is False
    assert record.reason == "RELATIVE_TIME"


def test_shift_question_loki_fixed_dispatches() -> None:
    shift = mod("shift")
    store = FakeLoki()
    _seed_101(store)
    with served_loki(store) as url:
        delivered, record = shift.shift_question(
            "loki", es_admin=_Untouchable(), loki_admin=_admin(url), qid=Q101,
            question=PROMPT, now=NOW, setup_started=SETUP_STARTED,
        )  # fmt: skip
    assert record.applied is True and record.backend == "loki"
    assert "November 19, 2026" in delivered


# -- B1 record bytes -------------------------------------------------------------------------

B1_RECORD_KEYS = {
    "applied", "reason", "seconds", "anchor", "original_min", "original_max",
    "shifted_min", "shifted_max", "indices", "store_size_before",
    "store_size_after", "question_rewrites", "time_only_phrases",
}  # fmt: skip


def test_b1_shift_record_json_unchanged() -> None:
    shift = mod("shift")
    relative = shift.ShiftRecord(
        applied=False, reason="RELATIVE_TIME", time_only_phrases=1
    )
    assert json.dumps(relative.as_json(), sort_keys=True) == json.dumps(
        {
            "applied": False, "reason": "RELATIVE_TIME", "seconds": 0,
            "anchor": None, "original_min": None, "original_max": None,
            "shifted_min": None, "shifted_max": None, "indices": [],
            "store_size_before": {}, "store_size_after": {},
            "question_rewrites": [], "time_only_phrases": 1,
        },
        sort_keys=True,
    )  # fmt: skip
    moment = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    applied = shift.ShiftRecord(
        applied=True, seconds=600, anchor=moment, original_min=moment,
        original_max=moment, shifted_min=moment + timedelta(minutes=10),
        shifted_max=moment + timedelta(minutes=10),
    )  # fmt: skip
    out = applied.as_json()
    assert set(out) == B1_RECORD_KEYS
    assert out["anchor"] == "2026-05-14T12:00:00+00:00"
