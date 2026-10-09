"""The sustained healthy window is measured from the first healthy sample's
window END (issue #157, user decision 2026-10-08).

A healthy sample's evaluation window can overlap the window of the degraded
sample before it (``rate()`` takes the first in-window raw sample as its
base, so a window that still contains the last faulty minute reads as
degraded while the next one, sixty seconds later, already reads as clean).
Counting the streak from the first healthy window's *start* therefore let
recovery be confirmed 361 s after the last degraded window ended in the
real lab (docs/evidence/m1-02-live/run.md). The streak now starts at that
window's end: the confirmed span never overlaps an earlier adopted window,
and it is at least one full ``sustained_window_seconds`` of healthy samples
after the last non-healthy one. These checks use ``fold_history`` alone so
they do not depend on how the test support files a session's ending.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from opspilot.observation.store import fold_history
from tests.m1_02_replay_support import NOW, healthy_history, stored_history, take

MINUTE = timedelta(seconds=60)


def test_the_streak_starts_at_the_first_healthy_windows_end_not_its_start():
    # 300 s windows a minute apart: the sixth sample spans 600 s since the
    # first window START, which used to confirm; measured from its END the
    # six samples cover 300 s and recovery stays unconfirmed
    history = healthy_history(6)
    report = fold_history(history)
    first = history["samples"][0]
    assert report.healthy_since == first["window_end"]
    assert report.healthy_until == history["samples"][-1]["window_end"]
    assert report.healthy_window_seconds == 5 * 60
    assert report.replayed_ended_reason is None
    assert report.expected_lifecycle != "resolved"


def test_the_healthy_span_never_overlaps_the_last_degraded_window():
    session_id = uuid4()
    authorized = NOW - timedelta(seconds=600)
    taken = [
        take("degraded", sequence=1, window_end=NOW, session_id=session_id),
        *(
            take(
                "healthy",
                sequence=index + 2,
                window_end=NOW + MINUTE * (index + 1),
                session_id=session_id,
            )
            for index in range(6)
        ),
    ]
    history = stored_history(taken, session_id=session_id, authorized_at=authorized)
    report = fold_history(history)
    degraded_end = history["samples"][0]["window_end"]
    # the streak starts where the first healthy window ends, after the
    # degraded window, although the two windows overlap by 240 s
    assert report.healthy_since == history["samples"][1]["window_end"]
    assert report.healthy_since > degraded_end
    assert history["samples"][1]["window_start"] < degraded_end
    # six healthy samples after the degraded one cover 300 s end to end:
    # the 600 s window is not reached (it used to be, start to end)
    assert report.healthy_window_seconds == 300
    assert report.replayed_ended_reason is None
    assert report.expected_lifecycle != "resolved"
