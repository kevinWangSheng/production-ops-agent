"""Synthetic PostgreSQL M0 ledger. No network/model authorization is conveyed."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from scripts.m0.contracts import (
    BudgetError,
    RequestIdentity,
    Reservation,
    RunContext,
    amount,
)


class PostgresBudget:
    def __init__(self, dsn: str):
        self._dsn = dsn

    @contextmanager
    def _transaction(self):
        failed = False
        try:
            with psycopg.connect(
                self._dsn, connect_timeout=3, row_factory=dict_row
            ) as conn:
                conn.execute("SET LOCAL statement_timeout = '5000ms'")
                conn.execute("SET LOCAL lock_timeout = '4000ms'")
                yield conn
        except (psycopg.Error, OSError, ValueError):
            failed = True
        # Outside the exception handler: no raw exception context containing a DSN.
        if failed:
            raise BudgetError("STORAGE_UNAVAILABLE")

    def install(self):
        """Explicit isolated experiment setup; never called by reserve."""
        with self._transaction() as conn:
            conn.execute(Path(__file__).with_suffix(".sql").read_text())

    def initialize(
        self, experiment_id: UUID, limit: int, deadline: datetime, *, synthetic=True
    ):
        amount(limit)
        if (
            not isinstance(experiment_id, UUID)
            or synthetic is not True
            or not isinstance(deadline, datetime)
            or deadline.tzinfo is None
            or deadline.utcoffset() is None
        ):
            raise BudgetError("INVALID_INPUT")
        deadline = deadline.astimezone(timezone.utc)
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO m0_experiments(id,ceiling,deadline,synthetic) VALUES(%s,%s,%s,true) ON CONFLICT DO NOTHING",
                (experiment_id, limit, deadline),
            )
            row = self._experiment(conn, experiment_id)
            if row["ceiling"] != limit or row["deadline"] != deadline:
                raise BudgetError("IDENTITY_CONFLICT")

    def register_run(self, run: RunContext):
        if not isinstance(run, RunContext):
            raise BudgetError("INVALID_INPUT")
        with self._transaction() as conn:
            exp = self._experiment(conn, run.experiment_id)
            if run.deadline > exp["deadline"]:
                raise BudgetError("IDENTITY_CONFLICT")
            conn.execute(
                "INSERT INTO m0_runs VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (run.run_id, run.experiment_id, run.provider, run.deadline),
            )
            self._run(conn, run)

    @staticmethod
    def _experiment(conn, identity):
        row = conn.execute(
            "SELECT * FROM m0_experiments WHERE id=%s FOR UPDATE", (identity,)
        ).fetchone()
        if row is None:
            raise BudgetError("UNKNOWN_IDENTITY")
        return row

    @staticmethod
    def _run(conn, run):
        row = conn.execute(
            "SELECT * FROM m0_runs WHERE id=%s", (run.run_id,)
        ).fetchone()
        if row is None:
            raise BudgetError("UNKNOWN_IDENTITY")
        if (row["experiment_id"], row["provider"], row["deadline"]) != (
            run.experiment_id,
            run.provider,
            run.deadline,
        ):
            raise BudgetError("IDENTITY_CONFLICT")

    @staticmethod
    def _snapshot(conn, experiment_id):
        return conn.execute(
            """SELECT COALESCE(sum(actual) FILTER(WHERE state='settled'),0) AS settled,
            COALESCE(sum(reserved) FILTER(WHERE state='reserved'),0) AS reserved,
            COALESCE(sum(reserved) FILTER(WHERE state='unknown'),0) AS unknown
            FROM m0_requests q JOIN m0_runs r ON r.id=q.run_id WHERE experiment_id=%s""",
            (experiment_id,),
        ).fetchone()

    def snapshot(self, experiment_id: UUID):
        if not isinstance(experiment_id, UUID):
            raise BudgetError("INVALID_INPUT")
        with self._transaction() as conn:
            exp = self._experiment(conn, experiment_id)
            result = {
                key: int(value)
                for key, value in self._snapshot(conn, experiment_id).items()
            }
            result.update(limit=int(exp["ceiling"]), blocked=exp["blocked"])
        return result

    def reserve(self, request: RequestIdentity, upper_bound: int) -> Reservation:
        return self._change(request, "reserve", amount(upper_bound, positive=True))

    def settle(self, request: RequestIdentity, actual: int) -> Reservation:
        return self._change(request, "settle", amount(actual))

    def retain_unknown(self, request: RequestIdentity) -> Reservation:
        return self._change(request, "unknown")

    def _change(self, request, operation, value=None):
        if not isinstance(request, RequestIdentity):
            raise BudgetError("INVALID_INPUT")
        with self._transaction() as conn:
            exp = self._experiment(conn, request.run.experiment_id)
            self._run(conn, request.run)
            row = conn.execute(
                "SELECT * FROM m0_requests WHERE id=%s", (request.request_id,)
            ).fetchone()
            created = False
            if row is not None and row["run_id"] != request.run.run_id:
                raise BudgetError("IDENTITY_CONFLICT")
            if operation == "reserve":
                if row is not None:
                    if row["reserved"] != value:
                        raise BudgetError("IDENTITY_CONFLICT")
                else:
                    now = conn.execute("SELECT clock_timestamp() AS now").fetchone()[
                        "now"
                    ]
                    if now >= min(exp["deadline"], request.run.deadline):
                        raise BudgetError("DEADLINE_EXCEEDED")
                    totals = self._snapshot(conn, request.run.experiment_id)
                    if (
                        exp["blocked"]
                        or sum(int(total) for total in totals.values()) + value
                        > exp["ceiling"]
                    ):
                        raise BudgetError("BUDGET_EXHAUSTED")
                    # Global request identity conflicts across experiments also fail closed.
                    row = conn.execute(
                        "INSERT INTO m0_requests VALUES(%s,%s,%s,'reserved',NULL) ON CONFLICT DO NOTHING RETURNING *",
                        (request.request_id, request.run.run_id, value),
                    ).fetchone()
                    if row is None:
                        raise BudgetError("IDENTITY_CONFLICT")
                    created = True
            elif row is None:
                raise BudgetError("UNKNOWN_IDENTITY")
            elif operation == "settle":
                if row["state"] == "settled" and row["actual"] != value:
                    raise BudgetError("SETTLEMENT_CONFLICT")
                row = conn.execute(
                    "UPDATE m0_requests SET state='settled',actual=%s WHERE id=%s RETURNING *",
                    (value, request.request_id),
                ).fetchone()
                if value > row["reserved"]:
                    conn.execute(
                        "UPDATE m0_experiments SET blocked=true WHERE id=%s",
                        (request.run.experiment_id,),
                    )
            elif row["state"] != "settled":
                row = conn.execute(
                    "UPDATE m0_requests SET state='unknown' WHERE id=%s RETURNING *",
                    (request.request_id,),
                ).fetchone()
            result = Reservation(
                request,
                int(row["reserved"]),
                row["state"],
                None if row["actual"] is None else int(row["actual"]),
                created,
            )
        # Commit has returned successfully before created=True is visible.
        return result
