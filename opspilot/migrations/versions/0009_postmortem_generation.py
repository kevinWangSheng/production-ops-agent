"""0009 postmortem generation: candidate schedule, lease and attempts.

Revision ID: 0009_postmortem_generation
Revises: 0008_postmortem_knowledge
Create Date: 2026-10-09

M1-03 step 2 (F13, contract r3 in the M1-03 task record). Two tables of
worker scheduling state, outside the append-only F13 store of 0008:

* ``opspilot_postmortem_generation_jobs`` -- one row per incident the worker
  has scheduled: the dedicated generation lease (D17: owner, epoch, expiry;
  never an investigation Run lease), the "regenerate" marker a return for
  revision writes in its own transaction (D19:
  ``pending_regeneration_version``), and the backoff after failed attempts
  (D22): consecutive failures at one watermark and the earliest next attempt.
* ``opspilot_postmortem_generation_attempts`` -- one row per generation
  attempt (D22), with its own bounded budget (D23: model requests reserved
  before each call, never more than ``max_model_requests``), the versions it
  ran with (D20), the size and hash of its model input (D21), token usage,
  and how it ended (a version, or an error code). A version's content
  carries the same generation record; the attempt row also covers attempts
  that wrote no version.

Both tables are updated in place (they are schedule state, not audit), and
like every table the owner creates, the Observer role has no privilege on
them (D8). Rows reference the incident; nothing deletes them.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_postmortem_generation"
down_revision: str | Sequence[str] | None = "0008_postmortem_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Equal to ``opspilot.knowledge.contract`` (tests/test_knowledge_contract.py).
ATTEMPT_STATUSES = ("running", "succeeded", "failed", "superseded", "abandoned")
ATTEMPT_ERRORS = (
    "MODEL_REJECTED",
    "MODEL_UNAVAILABLE",
    "INPUT_TOO_LARGE",
    "OUTPUT_INVALID",
    "WATERMARK_MOVED",
    "GENERATION_CONFLICT",
    "LEASE_LOST",
)


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE opspilot_postmortem_generation_jobs (
          incident_id uuid PRIMARY KEY REFERENCES opspilot_incidents,
          lease_owner uuid,
          lease_epoch integer NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
          lease_until timestamptz,
          -- D19: the returned version the worker must regenerate
          pending_regeneration_version integer CHECK (pending_regeneration_version > 0),
          -- the watermark (canonical JSON) the failure count belongs to
          failure_watermark text,
          consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
          next_attempt_at timestamptz,
          last_error_code text CHECK (last_error_code IS NULL OR last_error_code IN ({_in(ATTEMPT_ERRORS)})),
          updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          CONSTRAINT opspilot_postmortem_generation_jobs_lease_check
            CHECK ((lease_owner IS NULL) = (lease_until IS NULL))
        );
        CREATE TABLE opspilot_postmortem_generation_attempts (
          attempt_id uuid PRIMARY KEY,
          incident_id uuid NOT NULL REFERENCES opspilot_incidents,
          lease_owner uuid NOT NULL,
          lease_epoch integer NOT NULL,
          status text NOT NULL DEFAULT 'running' CHECK (status IN ({_in(ATTEMPT_STATUSES)})),
          error_code text CHECK (error_code IS NULL OR error_code IN ({_in(ATTEMPT_ERRORS)})),
          -- the watermark read at claim (canonical JSON of the store Watermark)
          watermark jsonb NOT NULL,
          revises_version integer CHECK (revises_version > 0),
          version integer CHECK (version > 0),
          model text NOT NULL,
          response_model text,
          model_profile text NOT NULL,
          prompt_version text NOT NULL,
          output_schema_version text NOT NULL,
          input_policy_version text NOT NULL,
          input_sha256 text CHECK (input_sha256 IS NULL OR input_sha256 ~ '^[0-9a-f]{{64}}$'),
          input_bytes integer CHECK (input_bytes >= 0),
          -- D23: this attempt's own budget, reserved before each call
          max_model_requests integer NOT NULL CHECK (max_model_requests > 0),
          model_requests integer NOT NULL DEFAULT 0
            CHECK (model_requests >= 0 AND model_requests <= max_model_requests),
          prompt_tokens bigint NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
          completion_tokens bigint NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
          started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          finished_at timestamptz,
          CONSTRAINT opspilot_postmortem_generation_attempts_finished_check
            CHECK ((status = 'running') = (finished_at IS NULL)),
          CONSTRAINT opspilot_postmortem_generation_attempts_outcome_check
            CHECK ((status IN ('failed', 'superseded', 'abandoned')) = (error_code IS NOT NULL)),
          CONSTRAINT opspilot_postmortem_generation_attempts_succeeded_check
            CHECK ((status = 'succeeded') = (version IS NOT NULL))
        );
        -- one attempt in flight per incident
        CREATE UNIQUE INDEX opspilot_postmortem_generation_attempts_running_idx
          ON opspilot_postmortem_generation_attempts(incident_id) WHERE status = 'running';
        CREATE INDEX opspilot_postmortem_generation_attempts_incident_idx
          ON opspilot_postmortem_generation_attempts(incident_id, started_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE opspilot_postmortem_generation_attempts;
        DROP TABLE opspilot_postmortem_generation_jobs;
        """
    )
