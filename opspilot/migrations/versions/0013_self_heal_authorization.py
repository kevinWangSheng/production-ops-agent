"""0013 self-heal authorization: session provenance and the automatic-observation record.

Revision ID: 0013_self_heal_authorization
Revises: 0012_generation_call_diagnostics
Create Date: 2026-10-12

#184 step 1 (contracts r3-r5 in the #184 task record, A4/A10/A11, S1-D1-S1-D3).
The revision and table keep the r3 "self-heal" name; the values make no
causal claim (r4): an automatic observation records that no remediation was
recorded, not that the fault healed itself.

* ``opspilot_observation_sessions.authorization_source``: who granted an
  incident recovery session -- ``post_remediation`` (the human "register
  remediation" action) or ``automatic`` (the Controller after a qualified
  conclusion was published at the incident's current generation). Constrained by ``purpose``: an
  ``incident_recovery`` session must carry one of the two, a
  ``release_observation`` session must carry NULL (its provenance is F11's).
  Existing incident recovery rows are backfilled ``post_remediation`` --
  before this revision only the human action could authorize one. The column
  default is that same legacy reading (A10), kept so a writer that predates
  this revision still records a valid row; the product's authorization
  primitive always names the source, and a release session must name NULL
  explicitly (the default would fail the CHECK there).
* ``opspilot_self_heal_authorizations``: one row per ``(incident_id,
  control_generation)`` the automatic-observation pass decided on -- ``authorized`` (with
  the session it created), ``skipped`` (stale or not applicable, terminal,
  the reason kept as history), ``retrying`` (an operational failure, with the
  attempt count and the next attempt time) or ``handed_off`` (retries
  exhausted; the incident stays ``open``). The primary key is the
  idempotency key: one decision per published generation.

Privileges (``opspilot.persistence.grants.RUNTIME_GRANTS``): the web, which
runs the automatic-observation pass, may insert the new session column and read and
write the new table; the worker gets nothing on the new table and keeps its
read-only view of the sessions; the Observer (0003) keeps its table-wide
``SELECT`` on the sessions and gets nothing on the new table.

``downgrade()`` revokes those privileges, drops the table and the column
(the provenance of every session and the automatic decisions are lost;
sessions, samples and endings are not).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_self_heal_authorization"
down_revision: str | Sequence[str] | None = "0012_generation_call_diagnostics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of ``opspilot.observation.store.AUTHORIZATION_SOURCES``.
SOURCES = ("post_remediation", "automatic")
STATES = ("authorized", "skipped", "retrying", "handed_off")

# Frozen copy of this revision's additions to
# ``opspilot.persistence.grants.RUNTIME_GRANTS`` (a migration never imports
# application code that moves on).
WEB_SESSION_INSERT = "authorization_source"
WEB_TABLE_INSERT = (
    "attempts,control_generation,incident_id,next_attempt_at,reason,session_id,state"
)
WEB_TABLE_UPDATE = "attempts,next_attempt_at,reason,session_id,state,updated_at"


def _in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE opspilot_observation_sessions
          ADD COLUMN authorization_source text DEFAULT 'post_remediation';
        -- ADD COLUMN ... DEFAULT filled every row; a release session has none
        UPDATE opspilot_observation_sessions SET authorization_source = NULL
          WHERE purpose <> 'incident_recovery';
        ALTER TABLE opspilot_observation_sessions
          ADD CONSTRAINT opspilot_observation_sessions_authorization_source_check CHECK (
            CASE purpose
              WHEN 'incident_recovery' THEN authorization_source IS NOT NULL
                AND authorization_source IN ({_in_list(SOURCES)})
              ELSE authorization_source IS NULL
            END
          );
        CREATE TABLE opspilot_self_heal_authorizations (
          incident_id uuid NOT NULL REFERENCES opspilot_incidents,
          control_generation integer NOT NULL CHECK (control_generation >= 0),
          state text NOT NULL,
          -- NULL only on an authorization; the refusal or last failure code otherwise
          reason text,
          -- operational failures so far (``retrying`` / ``handed_off``)
          attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
          next_attempt_at timestamptz,
          session_id uuid REFERENCES opspilot_observation_sessions,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY (incident_id, control_generation),
          CONSTRAINT opspilot_self_heal_authorizations_state_check
            CHECK (state IN ({_in_list(STATES)})),
          CONSTRAINT opspilot_self_heal_authorizations_session_check
            CHECK ((state = 'authorized') = (session_id IS NOT NULL)),
          CONSTRAINT opspilot_self_heal_authorizations_reason_check
            CHECK ((state = 'authorized') = (reason IS NULL)),
          CONSTRAINT opspilot_self_heal_authorizations_retry_check
            CHECK ((state = 'retrying') = (next_attempt_at IS NOT NULL))
        );
        -- the retry scan
        CREATE INDEX opspilot_self_heal_authorizations_retry_idx
          ON opspilot_self_heal_authorizations(next_attempt_at) WHERE state = 'retrying';
        CREATE INDEX opspilot_self_heal_authorizations_session_idx
          ON opspilot_self_heal_authorizations(session_id) WHERE session_id IS NOT NULL;
        GRANT INSERT ({WEB_SESSION_INSERT}) ON opspilot_observation_sessions TO opspilot_web;
        GRANT SELECT ON opspilot_self_heal_authorizations TO opspilot_web;
        GRANT INSERT ({WEB_TABLE_INSERT}) ON opspilot_self_heal_authorizations TO opspilot_web;
        GRANT UPDATE ({WEB_TABLE_UPDATE}) ON opspilot_self_heal_authorizations TO opspilot_web;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        REVOKE UPDATE ({WEB_TABLE_UPDATE}) ON opspilot_self_heal_authorizations FROM opspilot_web;
        REVOKE INSERT ({WEB_TABLE_INSERT}) ON opspilot_self_heal_authorizations FROM opspilot_web;
        REVOKE SELECT ON opspilot_self_heal_authorizations FROM opspilot_web;
        REVOKE INSERT ({WEB_SESSION_INSERT}) ON opspilot_observation_sessions FROM opspilot_web;
        DROP TABLE opspilot_self_heal_authorizations;
        ALTER TABLE opspilot_observation_sessions
          DROP CONSTRAINT opspilot_observation_sessions_authorization_source_check;
        ALTER TABLE opspilot_observation_sessions DROP COLUMN authorization_source;
        """
    )
