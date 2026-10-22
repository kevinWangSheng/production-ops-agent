"""0018 alert correlation: attached episodes and related incidents (F2 PR-C).

Revision ID: 0018_alert_correlation
Revises: 0017_web_suspension_grants
Create Date: 2026-10-21

F2 U1-A (contract r2 in the F2 task record, decision table C1-C3, R-C5,
R-C8). A firing alert whose identity is new but whose fingerprint already
opened a non-closed incident on the same target is attached to that
incident instead of opening another: one ``opspilot_alert_deliveries`` row
with the new outcome ``attached`` and one event input, no identity row and
no Run (C1-A, so identities and incidents stay one to one). When every such
incident is closed, the new incident records the latest of them in
``opspilot_incidents.related_incident_id``; the closed one is not reopened.

* ``opspilot_alert_deliveries.outcome`` accepts ``attached`` besides the six
  outcomes of 0010.
* ``opspilot_incidents.related_incident_id``: nullable, references an
  incident, never the incident itself. Only the web role may insert it; no
  role may update it. Observer's column-level SELECT stays as it is.

``downgrade()`` refuses while an ``attached`` delivery or a related incident
exists: the older schema would lose the correlation.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0018_alert_correlation"
down_revision: str | Sequence[str] | None = "0017_web_suspension_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (do not import 0010 or the evolving code); equal to
# ``opspilot.persistence.alerts.OUTCOMES`` (tests/test_f2_c_impl_correlation.py).
OUTCOMES_0010 = (
    "created",
    "replayed",
    "handoff_created",
    "handoff_replayed",
    "resolved_attached",
    "resolved_recorded",
)
OUTCOMES = (*OUTCOMES_0010, "attached")
OUTCOME_CHECK = "opspilot_alert_deliveries_outcome_check"
RELATED_CHECK = "opspilot_incidents_related_incident_check"
RELATED_FK = "opspilot_incidents_related_incident_id_fkey"


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace_outcome_check(values: Sequence[str]) -> None:
    # 0010 left the CHECK unnamed; drop whichever check constrains
    # ``outcome`` (PostgreSQL's default name, looked up rather than assumed)
    # and re-add it under that name with the new vocabulary.
    names: Sequence[str] = (
        op.get_bind()
        .execute(
            text(
                "SELECT c.conname FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey) "
                "WHERE c.conrelid='opspilot_alert_deliveries'::regclass "
                "AND c.contype='c' AND a.attname='outcome'"
            )
        )
        .scalars()
        .all()
    )
    for name in names:
        op.execute(f'ALTER TABLE opspilot_alert_deliveries DROP CONSTRAINT "{name}"')
    op.execute(
        f"ALTER TABLE opspilot_alert_deliveries ADD CONSTRAINT {OUTCOME_CHECK} "
        f"CHECK (outcome IN ({_in(values)}))"
    )


def upgrade() -> None:
    _replace_outcome_check(OUTCOMES)
    op.execute(
        f"""
        ALTER TABLE opspilot_incidents
          ADD COLUMN related_incident_id uuid
            CONSTRAINT {RELATED_FK} REFERENCES opspilot_incidents(incident_id),
          ADD CONSTRAINT {RELATED_CHECK}
            CHECK (related_incident_id <> incident_id);
        GRANT INSERT (related_incident_id) ON opspilot_incidents TO opspilot_web;
        """
    )


def downgrade() -> None:
    # Same table locks as 0010: an intake in flight could otherwise commit an
    # attached delivery or a related incident after the counts.
    op.execute(
        "LOCK TABLE opspilot_incidents, opspilot_alert_deliveries IN ACCESS EXCLUSIVE MODE"
    )
    bind = op.get_bind()
    attached = int(
        bind.execute(
            text(
                "SELECT count(*) FROM opspilot_alert_deliveries WHERE outcome='attached'"
            )
        ).scalar_one()
    )
    related = int(
        bind.execute(
            text(
                "SELECT count(*) FROM opspilot_incidents WHERE related_incident_id IS NOT NULL"
            )
        ).scalar_one()
    )
    if attached or related:
        raise RuntimeError(
            f"{attached} attached alert delivery(ies) and {related} related "
            "incident(s); the schema before 0018 cannot represent them, "
            "refusing to downgrade"
        )
    op.execute(
        f"""
        REVOKE INSERT (related_incident_id) ON opspilot_incidents FROM opspilot_web;
        ALTER TABLE opspilot_incidents
          DROP CONSTRAINT {RELATED_CHECK},
          DROP COLUMN related_incident_id;
        """
    )
    _replace_outcome_check(OUTCOMES_0010)
