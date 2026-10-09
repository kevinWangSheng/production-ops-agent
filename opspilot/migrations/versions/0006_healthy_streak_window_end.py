"""0006 healthy streak from the window end: end every open observation session.

Revision ID: 0006_healthy_streak_window_end
Revises: 0005_incident_mode
Create Date: 2026-10-09

Issue #157 (user decision 2026-10-08): the sustained healthy window is
measured from the END of the first healthy sample's evaluation window, no
longer from its start. ``opspilot_observation_sessions.healthy_since`` is
that start under the previous code, and the fold only assigns it for a new
or gapped streak, so a session authorized before this change would keep
the old mark and could still confirm recovery under the shorter rule (bot
review of PR #161, P1). C3 section 10: "规则变化后，如仍获授权则创建新版本观察
会话，否则保持停止" -- a migration cannot authorize on a human's behalf, so
it does the second half (user decision B, 2026-10-08): every session that is
still ``authorized`` is ended with ``authority_revoked`` through the same
``end_session`` the human-control paths use (state ``revoked``, task slot
cleared, watermarks kept, one ending record with the incident lifecycle
unchanged, as a pause does), and the incident stays ``observing_recovery``
until a human registers the remediation again, which opens a new session
that accumulates under the new rule. ``authority_revoked`` is the existing
reason that says exactly this: the authorization this session ran under no
longer holds; ``binding_stale`` and ``scope_suspended`` name other causes.
Ended sessions are the record of decisions taken under the rule in force at
the time and are left alone; the offline replay recomputes such a session
under the new rule and reports ``WATERMARK_MISMATCH`` for a streak that
started at a window start, which is the documented limit of this change.

``downgrade()`` is the same action in the other direction: moving back to
the window-start rule is a rule change too, so every session still
``authorized`` under the new rule is ended the same way and a human
re-registers; the sessions ``upgrade()`` ended stay ended (their ending
record says why). No mark is rewritten in either direction.
"""

from collections.abc import Sequence
from typing import Any

from alembic import op

from opspilot.observation.revocation import end_session

revision: str = "0006_healthy_streak_window_end"
down_revision: str | Sequence[str] | None = "0005_incident_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN_INCIDENTS = (
    "SELECT DISTINCT incident_id FROM opspilot_observation_sessions "
    "WHERE state='authorized' ORDER BY incident_id"
)
_LOCK_INCIDENT = (
    "SELECT lifecycle FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE"
)
_OPEN_SESSIONS_OF = (
    "SELECT session_id FROM opspilot_observation_sessions "
    "WHERE incident_id=%s AND state='authorized' ORDER BY session_id FOR UPDATE"
)


def end_open_sessions(conn: Any) -> int:
    """End every ``authorized`` session (``authority_revoked``, lifecycle
    unchanged). ``conn`` is a DB-API connection (psycopg) inside the caller's
    transaction. Returns the number of sessions ended.

    Locking follows the product's own order and granularity, row locks on
    the incident first and then on its sessions (``_lock_incident`` ->
    session ``FOR UPDATE`` in ``submit_sample``, ``register_remediation``,
    pause and takeover), so an in-flight product transaction either commits
    before this one takes its rows or waits behind it, never the reverse.
    ``claim_due_samples`` takes no row lock it would wait for (``SKIP
    LOCKED``) and only table-level ROW SHARE locks, which row locks do not
    conflict with; a table-level ACCESS EXCLUSIVE lock here would instead
    deadlock against its join order (Codex recheck of PR #161, P2). A
    session authorized concurrently for an incident not in the initial scan
    was authorized under the new code and is left alone."""
    with conn.cursor() as cursor:
        cursor.execute(_OPEN_INCIDENTS)
        incidents = [row[0] for row in cursor.fetchall()]
        ended = 0
        for incident_id in incidents:
            cursor.execute(_LOCK_INCIDENT, (incident_id,))
            locked = cursor.fetchone()
            if locked is None:
                continue
            lifecycle = str(locked[0])
            cursor.execute(_OPEN_SESSIONS_OF, (incident_id,))
            for (session_id,) in cursor.fetchall():
                end_session(
                    conn,
                    session_id,
                    incident_id,
                    ended_reason="authority_revoked",
                    transition=None,
                    lifecycle_before=lifecycle,
                    lifecycle_after=lifecycle,
                )
                ended += 1
    return ended


def _dbapi_connection() -> Any:
    bind = op.get_bind()
    return bind.connection.dbapi_connection


def upgrade() -> None:
    end_open_sessions(_dbapi_connection())


def downgrade() -> None:
    # the rule changes back: open sessions stop the same way (module docstring)
    end_open_sessions(_dbapi_connection())
