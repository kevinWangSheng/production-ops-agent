"""0006 healthy streak from the window end: convert the mark of every open session.

Revision ID: 0006_healthy_streak_window_end
Revises: 0005_incident_mode
Create Date: 2026-10-09

Issue #157 (user decision 2026-10-08): the sustained healthy window is
measured from the END of the first healthy sample's evaluation window, no
longer from its start. ``opspilot_observation_sessions.healthy_since`` is
that start under the previous code, and the fold only assigns it for a new
or gapped streak, so a session authorized before this change would keep
the old mark and could still confirm recovery under the shorter rule (bot
review of PR #161, P1). The mark is recomputed from the session's own
adopted samples, which carry both window bounds: the trailing run of
consecutive adopted samples that confirm health, stopped by a gap (a later
window starting after the earlier one ended, as the fold does), gives the
first sample of the streak, whose window end is the new mark (``upgrade``)
and whose window start was the old one (``downgrade``). Only sessions still
``authorized`` are converted: an ended session's row is the record of the
decisions taken under the rule in force at the time, so it keeps its
marks; the offline replay recomputes such a session under the new rule and
reports ``WATERMARK_MISMATCH`` for a streak that started at a window start,
which is the documented limit of this change (no production data exists at
this stage; the superseded lab evidence says so).
"""

from collections.abc import Sequence
from typing import Any

from alembic import op

revision: str = "0006_healthy_streak_window_end"
down_revision: str | Sequence[str] | None = "0005_incident_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN_SESSIONS = (
    "SELECT session_id FROM opspilot_observation_sessions WHERE state='authorized'"
)
_ADOPTED_SAMPLES = (
    "SELECT confirms_health, window_start, window_end "
    "FROM opspilot_observation_samples "
    "WHERE session_id=%s AND disposition='adopted' ORDER BY sequence DESC"
)
_SET_MARK = (
    "UPDATE opspilot_observation_sessions "
    "SET healthy_since=%s, updated_at=clock_timestamp() WHERE session_id=%s"
)


def recompute_open_streaks(conn: Any, *, from_window_end: bool) -> int:
    """Set ``healthy_since`` of every ``authorized`` session from its adopted
    samples: the first sample of the trailing healthy streak, its window end
    (``from_window_end``) or its window start (the previous rule); ``NULL``
    when the latest adopted sample does not confirm health. ``conn`` is a
    DB-API connection (psycopg) inside the caller's transaction. Returns the
    number of sessions written."""
    with conn.cursor() as cursor:
        cursor.execute(_OPEN_SESSIONS)
        sessions = [row[0] for row in cursor.fetchall()]
        written = 0
        for session_id in sessions:
            cursor.execute(_ADOPTED_SAMPLES, (session_id,))
            mark = None
            newer_start = None
            for confirms, window_start, window_end in cursor.fetchall():
                if not confirms:
                    break
                if newer_start is not None and newer_start > window_end:
                    break  # a gap: the streak started with the newer sample
                mark = window_end if from_window_end else window_start
                newer_start = window_start
            cursor.execute(_SET_MARK, (mark, session_id))
            written += 1
    return written


def _dbapi_connection() -> Any:
    bind = op.get_bind()
    return bind.connection.dbapi_connection


def upgrade() -> None:
    recompute_open_streaks(_dbapi_connection(), from_window_end=True)


def downgrade() -> None:
    recompute_open_streaks(_dbapi_connection(), from_window_end=False)
