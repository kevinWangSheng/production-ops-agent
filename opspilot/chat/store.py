"""The two Chat tables (migration 0026, workbench contract E1 r10).

The web half (``ask``, ``sessions``, ``session``, ``record_forward``,
``forward_candidates``) runs as ``opspilot_web``; the worker half
(``expire``, ``claim``, ``history``, ``finish``) as ``opspilot_worker``.
Each writes only the columns its role is granted (``grants.py``). Every
time is the database clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from opspilot.persistence.base import PersistenceError, _StoreBase

#: P7-11: questions per author per window, counted at insert.
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 600
#: P7-12: the History tab lists this many sessions.
HISTORY_SESSIONS = 50
#: P7-6: a claim's lease, the attempts a turn gets, and when a pending turn
#: is given up.
LEASE_SECONDS = 180
MAX_ATTEMPTS = 2
PENDING_TIMEOUT_SECONDS = 600
#: P7-8: the earlier exchanges of a session the model sees.
HISTORY_TURNS = 10
TITLE_CHARS = 200

_NAMESPACE = UUID("8f3d0b52-5a8e-4c5f-9a57-6f0c1e0c2a11")

_TURN_COLUMNS = (
    "t.turn_id,t.session_id,t.seq,t.author,t.question,t.asked_at,t.status,t.answer,"
    "t.steer,t.refs,t.error_code,t.answered_at,t.forward_state,t.forward_code,"
    "t.forwarded_at,t.forwarded_input_sequence,t.context_sha256,"
    "(extract(epoch FROM t.answered_at - t.asked_at) * 1000)::bigint AS elapsed_ms"
)


def session_id_for(incident_id: UUID, author: str, idempotency_key: str) -> UUID:
    """A new session's id: a retried first question finds the same one."""
    return uuid5(_NAMESPACE, f"chat-session:{incident_id}:{author}:{idempotency_key}")


def turn_id_for(session_id: UUID, idempotency_key: str) -> UUID:
    return uuid5(_NAMESPACE, f"chat-turn:{session_id}:{idempotency_key}")


def _title(question: str) -> str:
    title = " ".join(question.split()) or "Chat"
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1] + "…"


class ChatStore(_StoreBase):
    # -- web ---------------------------------------------------------------

    def ask(
        self,
        incident_id: UUID,
        *,
        session_id: UUID | None,
        author: str,
        question: str,
        idempotency_key: str,
        context: Mapping[str, Any],
        context_sha256: str,
    ) -> tuple[dict[str, Any], bool]:
        """Insert one question (P7-5, P7-11): ``(turn, replayed)``. A key
        already used in the session replays its turn (``CHAT_KEY_CONFLICT``
        when the text differs); over the rate limit ``CHAT_RATE_LIMITED``;
        a pending turn in the session ``CHAT_BUSY``; a session of another
        incident ``CHAT_UNKNOWN_SESSION``."""
        with self.transaction() as conn:
            if session_id is None:
                session_id = session_id_for(incident_id, author, idempotency_key)
                new_session = True
            else:
                new_session = False
            row = conn.execute(
                "SELECT incident_id FROM opspilot_chat_sessions WHERE session_id=%s",
                (session_id,),
            ).fetchone()
            if row is not None and row["incident_id"] != incident_id:
                raise PersistenceError("CHAT_UNKNOWN_SESSION")
            if row is None and not new_session:
                raise PersistenceError("CHAT_UNKNOWN_SESSION")
            if row is not None:
                existing = conn.execute(
                    f"SELECT {_TURN_COLUMNS} FROM opspilot_chat_turns t "
                    "WHERE t.session_id=%s AND t.idempotency_key=%s",
                    (session_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["question"] != question or existing["author"] != author:
                        raise PersistenceError("CHAT_KEY_CONFLICT")
                    return dict(existing), True
            recent = conn.execute(
                "SELECT count(*) AS n FROM opspilot_chat_turns WHERE author=%s "
                "AND asked_at >= clock_timestamp() - make_interval(secs => %s)",
                (author, RATE_WINDOW_SECONDS),
            ).fetchone()
            if recent is not None and int(recent["n"]) >= RATE_LIMIT:
                raise PersistenceError("CHAT_RATE_LIMITED")
            if row is None:
                conn.execute(
                    "INSERT INTO opspilot_chat_sessions "
                    "(session_id,incident_id,created_by,title) VALUES (%s,%s,%s,%s)",
                    (session_id, incident_id, author, _title(question)),
                )
            elif conn.execute(
                "SELECT 1 FROM opspilot_chat_turns WHERE session_id=%s "
                "AND status='pending'",
                (session_id,),
            ).fetchone():
                raise PersistenceError("CHAT_BUSY")
            inserted = conn.execute(
                "INSERT INTO opspilot_chat_turns (turn_id,session_id,author,question,"
                "idempotency_key,context,context_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING turn_id",
                (
                    turn_id_for(session_id, idempotency_key),
                    session_id,
                    author,
                    question,
                    idempotency_key,
                    Jsonb(dict(context)),
                    context_sha256,
                ),
            ).fetchone()
            assert inserted is not None
            turn = self._require_row(
                conn.execute(
                    f"SELECT {_TURN_COLUMNS} FROM opspilot_chat_turns t WHERE t.turn_id=%s",
                    (inserted["turn_id"],),
                )
            )
            return dict(turn), False

    def sessions(
        self, incident_id: UUID, *, limit: int = HISTORY_SESSIONS
    ) -> list[dict[str, Any]]:
        """The History tab (P7-12): this incident's newest sessions."""
        with self.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT s.session_id,s.created_by,s.title,s.created_at,"
                "(SELECT count(*) FROM opspilot_chat_turns t WHERE t.session_id=s.session_id) AS turns,"
                "(SELECT max(t.asked_at) FROM opspilot_chat_turns t WHERE t.session_id=s.session_id) AS last_asked_at "
                "FROM opspilot_chat_sessions s WHERE s.incident_id=%s "
                "ORDER BY s.created_at DESC, s.session_id LIMIT %s",
                (incident_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def session(self, incident_id: UUID, session_id: UUID) -> dict[str, Any] | None:
        """One session with its turns in order, without the stored context."""
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT session_id,incident_id,created_by,title,created_at "
                "FROM opspilot_chat_sessions WHERE session_id=%s AND incident_id=%s",
                (session_id, incident_id),
            ).fetchone()
            if row is None:
                return None
            turns = conn.execute(
                f"SELECT {_TURN_COLUMNS} FROM opspilot_chat_turns t "
                "WHERE t.session_id=%s ORDER BY t.seq",
                (session_id,),
            ).fetchall()
        return {**dict(row), "turns": [dict(t) for t in turns]}

    def turn(self, incident_id: UUID, turn_id: UUID) -> dict[str, Any] | None:
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                f"SELECT {_TURN_COLUMNS} FROM opspilot_chat_turns t "
                "JOIN opspilot_chat_sessions s ON s.session_id=t.session_id "
                "WHERE t.turn_id=%s AND s.incident_id=%s",
                (turn_id, incident_id),
            ).fetchone()
        return None if row is None else dict(row)

    def record_forward(
        self,
        turn_id: UUID,
        *,
        state: str,
        code: str | None,
        input_sequence: int | None,
    ) -> bool:
        """Write the forward result once (P7-9); ``False`` when it is
        already written or the turn is not an answered steer."""
        if state not in ("sent", "refused"):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            row = conn.execute(
                "UPDATE opspilot_chat_turns SET forward_state=%s,forward_code=%s,"
                "forwarded_input_sequence=%s,forwarded_at=clock_timestamp() "
                "WHERE turn_id=%s AND forward_state IS NULL AND steer "
                "AND status='answered' RETURNING turn_id",
                (state, code, input_sequence, turn_id),
            ).fetchone()
        return row is not None

    def forward_candidates(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Answered steers not forwarded yet (the relay loop's backup pass)."""
        with self.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT t.turn_id,s.incident_id FROM opspilot_chat_turns t "
                "JOIN opspilot_chat_sessions s ON s.session_id=t.session_id "
                "WHERE t.steer AND t.forward_state IS NULL AND t.status='answered' "
                "ORDER BY t.seq LIMIT %s",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- worker ------------------------------------------------------------

    def expire(self) -> int:
        """Give up turns pending past ``PENDING_TIMEOUT_SECONDS``, or whose
        last attempt's lease lapsed (``CHAT_TIMEOUT``)."""
        with self.transaction() as conn:
            rows = conn.execute(
                "UPDATE opspilot_chat_turns SET status='failed',error_code='CHAT_TIMEOUT',"
                "answered_at=clock_timestamp(),claim_owner=NULL,claim_until=NULL "
                "WHERE status='pending' AND (asked_at < clock_timestamp() - make_interval(secs => %s) "
                "OR (attempts >= %s AND claim_until < clock_timestamp())) RETURNING turn_id",
                (PENDING_TIMEOUT_SECONDS, MAX_ATTEMPTS),
            ).fetchall()
        return len(rows)

    def claim(
        self, owner: UUID, *, lease_seconds: int = LEASE_SECONDS
    ) -> dict[str, Any] | None:
        """The oldest claimable pending turn, leased to ``owner``."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT turn_id FROM opspilot_chat_turns WHERE status='pending' "
                "AND attempts < %s AND (claim_until IS NULL OR claim_until < clock_timestamp()) "
                "ORDER BY seq LIMIT 1 FOR UPDATE SKIP LOCKED",
                (MAX_ATTEMPTS,),
            ).fetchone()
            if row is None:
                return None
            claimed = self._require_row(
                conn.execute(
                    "UPDATE opspilot_chat_turns SET claim_owner=%s,"
                    "claim_until=clock_timestamp() + make_interval(secs => %s),"
                    "attempts=attempts+1 WHERE turn_id=%s "
                    "RETURNING turn_id,session_id,seq,author,question,context,attempts",
                    (owner, lease_seconds, row["turn_id"]),
                )
            )
        return dict(claimed)

    def history(self, session_id: UUID, before_seq: int) -> list[dict[str, Any]]:
        """The session's last ``HISTORY_TURNS`` answered exchanges before
        ``before_seq``, oldest first."""
        with self.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT question,answer FROM opspilot_chat_turns WHERE session_id=%s "
                "AND seq < %s AND status='answered' ORDER BY seq DESC LIMIT %s",
                (session_id, before_seq, HISTORY_TURNS),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def finish(
        self,
        turn_id: UUID,
        owner: UUID,
        *,
        answer: str | None,
        steer: bool,
        refs: list[str],
        error_code: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> bool:
        """Close a claimed turn: ``answered`` with ``answer``, else
        ``failed`` with ``error_code``. ``False`` when the lease is gone."""
        status = "answered" if answer is not None else "failed"
        with self.transaction() as conn:
            row = conn.execute(
                "UPDATE opspilot_chat_turns SET status=%s,answer=%s,steer=%s,refs=%s,"
                "error_code=%s,prompt_tokens=%s,completion_tokens=%s,"
                "answered_at=clock_timestamp(),claim_owner=NULL,claim_until=NULL "
                "WHERE turn_id=%s AND claim_owner=%s AND status='pending' RETURNING turn_id",
                (
                    status,
                    answer,
                    bool(steer) and answer is not None,
                    Jsonb(list(refs)),
                    error_code,
                    prompt_tokens,
                    completion_tokens,
                    turn_id,
                    owner,
                ),
            ).fetchone()
        return row is not None
