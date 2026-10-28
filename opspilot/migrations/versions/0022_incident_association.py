"""0022 incident association: rebinding the target and the merge pointer (#245).

Revision ID: 0022_incident_association
Revises: 0021_release_incident_links
Create Date: 2026-10-27

#245 contract r1 (task record ``docs/tasks/2026-10-27-245-incident-association.md``,
U6, G12). A human may rebind an incident to another registered target
(``rebind_target``, PR-1) or merge it into a surviving incident of the same
target (``merge_into``, PR-2). Both migrate here at once:

* ``opspilot_incidents.merged_into_incident_id``: nullable, references an
  incident, never the incident itself. A merged incident is ``closed`` plus
  this pointer; no lifecycle value is added (G3).
* The web role may UPDATE ``merged_into_incident_id`` and ``target_id`` on
  ``opspilot_incidents``. The worker and the Observer get nothing new; no
  guard trigger is added (G9): the rules live in ``control()``'s transaction,
  as close and reopen do.

``downgrade()`` refuses while a merge pointer exists: the older schema would
lose the association. A rebound ``target_id`` is representable before 0022
and stays as it is.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0022_incident_association"
down_revision: str | Sequence[str] | None = "0021_release_incident_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MERGED_CHECK = "opspilot_incidents_merged_into_incident_check"
MERGED_FK = "opspilot_incidents_merged_into_incident_id_fkey"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE opspilot_incidents
          ADD COLUMN merged_into_incident_id uuid
            CONSTRAINT {MERGED_FK} REFERENCES opspilot_incidents(incident_id),
          ADD CONSTRAINT {MERGED_CHECK}
            CHECK (merged_into_incident_id <> incident_id);
        GRANT UPDATE (merged_into_incident_id, target_id) ON opspilot_incidents TO opspilot_web;
        """
    )


def downgrade() -> None:
    # A control in flight could otherwise commit a pointer after the count.
    op.execute("LOCK TABLE opspilot_incidents IN ACCESS EXCLUSIVE MODE")
    merged = int(
        op.get_bind()
        .execute(
            text(
                "SELECT count(*) FROM opspilot_incidents WHERE merged_into_incident_id IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if merged:
        raise RuntimeError(
            f"{merged} merged incident(s); the schema before 0022 cannot "
            "represent them, refusing to downgrade"
        )
    op.execute(
        f"""
        REVOKE UPDATE (merged_into_incident_id, target_id) ON opspilot_incidents FROM opspilot_web;
        ALTER TABLE opspilot_incidents
          DROP CONSTRAINT {MERGED_CHECK},
          DROP COLUMN merged_into_incident_id;
        """
    )
