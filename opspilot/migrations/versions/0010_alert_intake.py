"""0010 alert intake: Alertmanager alert identities and the delivery audit.

Revision ID: 0010_alert_intake
Revises: 0009_postmortem_generation
Create Date: 2026-10-10

M1-04 step 2 (#167, contract r1-r3 in the M1-04 task record). Two tables:

* ``opspilot_alert_identities`` -- one row per alert identity
  ``fingerprint:startsAt`` (E3), written in the same transaction as the
  incident it opened, so a concurrent first delivery of the same identity
  finds it instead of opening a second incident (primary key on the
  delivery key, unique pair as well). It records how the target was
  resolved: the bound ``resource_uid`` with the affected service
  (``namespace`` + ``workload``, D/E11), or the reason the incident was
  handed to a human without a Run (``TARGET_UNRESOLVED`` /
  ``TARGET_AMBIGUOUS``, E5/E7); exactly one of the two.
* ``opspilot_alert_deliveries`` -- one append-only row per accepted
  delivery (I7): replays, handoffs and resolved notifications included,
  invalid alerts excluded. It keeps the redacted alert JSON bounded to
  16 KiB (``truncated`` marks a cut), the sha256 of the alert as received,
  the actor, the database receipt time, and the annotation revision of the
  identity (E9: a change appends a new revision, nothing is overwritten).
  ``incident_id`` is empty only for a resolved notification no incident of
  the same identity exists for (E8).

Handoff-only incidents need no column of their own: ``opspilot_incidents``
already allows an empty ``current_run_id`` and ``target_id``; such an
incident is ``waiting_human`` with no Run, and the claim listing joins Runs,
so no worker ever picks it up. The Observer role gets no privilege on either
table. ``downgrade()`` refuses while a handoff-only incident exists: the
older schema cannot represent an incident without a Run, and every page and
rebuild of it would fail.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0010_alert_intake"
down_revision: str | Sequence[str] | None = "0009_postmortem_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Equal to ``opspilot.alertmanager`` (tests/test_m1_04_alert_intake_impl.py).
HANDOFF_REASONS = ("TARGET_UNRESOLVED", "TARGET_AMBIGUOUS")
OUTCOMES = (
    "created",
    "replayed",
    "handoff_created",
    "handoff_replayed",
    "resolved_attached",
    "resolved_recorded",
)


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE opspilot_alert_identities (
          delivery_key text PRIMARY KEY,
          fingerprint text NOT NULL,
          starts_at timestamptz NOT NULL,
          starts_at_raw text NOT NULL,
          incident_id uuid NOT NULL UNIQUE REFERENCES opspilot_incidents,
          run_id uuid REFERENCES opspilot_runs,
          target_id text,
          namespace text,
          workload text,
          handoff_reason text CHECK (handoff_reason IN ({_in(HANDOFF_REASONS)})),
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          UNIQUE (fingerprint, starts_at),
          CHECK ((handoff_reason IS NULL) = (run_id IS NOT NULL)),
          CHECK ((handoff_reason IS NULL) = (target_id IS NOT NULL))
        );
        CREATE TABLE opspilot_alert_deliveries (
          delivery_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          delivery_key text NOT NULL,
          fingerprint text NOT NULL,
          starts_at timestamptz NOT NULL,
          starts_at_raw text NOT NULL,
          status text NOT NULL CHECK (status IN ('firing', 'resolved')),
          outcome text NOT NULL CHECK (outcome IN ({_in(OUTCOMES)})),
          incident_id uuid REFERENCES opspilot_incidents,
          actor text NOT NULL,
          received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          raw_sha256 text NOT NULL CHECK (raw_sha256 ~ '^[0-9a-f]{{64}}$'),
          annotations_sha256 text NOT NULL CHECK (annotations_sha256 ~ '^[0-9a-f]{{64}}$'),
          annotation_revision integer NOT NULL CHECK (annotation_revision >= 1),
          alert_json text NOT NULL CHECK (octet_length(alert_json) <= 16384),
          truncated boolean NOT NULL
        );
        CREATE INDEX opspilot_alert_deliveries_key_idx
          ON opspilot_alert_deliveries(delivery_key, delivery_id);
        CREATE INDEX opspilot_alert_deliveries_incident_idx
          ON opspilot_alert_deliveries(incident_id, delivery_id);
        """
    )


def downgrade() -> None:
    # Same table lock as 0005: an alert intake in flight could otherwise
    # commit a handoff-only incident after the count.
    op.execute("LOCK TABLE opspilot_incidents IN ACCESS EXCLUSIVE MODE")
    runless: int = int(
        op.get_bind()
        .execute(
            text("SELECT count(*) FROM opspilot_alert_identities WHERE run_id IS NULL")
        )
        .scalar_one()
    )
    if runless:
        raise RuntimeError(
            f"{runless} handoff-only incident(s) without a Run; the schema "
            "before 0010 cannot represent them, refusing to downgrade"
        )
    op.execute(
        """
        DROP TABLE opspilot_alert_deliveries;
        DROP TABLE opspilot_alert_identities;
        """
    )
