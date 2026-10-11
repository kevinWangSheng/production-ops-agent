"""0012 generation call diagnostics: one entry per model request of an attempt.

Revision ID: 0012_generation_call_diagnostics
Revises: 0011_runtime_roles
Create Date: 2026-10-11

#202 (M1-03 contract r10, D53-D56 in the M1-03 task record). Adds
``opspilot_postmortem_generation_attempts.model_calls``: a JSON array the
worker writes in the transaction that closes the attempt, one object per
model request in request order -- finish reason, prompt/completion/reasoning
token counts, outcome and problem codes (``AttemptView.model_calls`` in
``opspilot.knowledge.contract``). Never reply text, reasoning or a prompt.

Existing rows get ``[]``, which reads as "unknown": nothing is backfilled
or inferred from the aggregate token columns.

Privileges (``opspilot.persistence.grants.RUNTIME_GRANTS``): the worker may
update the new column, nothing else changes; the web reads it through its
existing table-wide ``SELECT`` and writes nothing on this table.

``downgrade()`` revokes the worker's column privilege and drops the column
(the per-request diagnostics are lost; attempts and versions are not).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_generation_call_diagnostics"
down_revision: str | Sequence[str] | None = "0011_runtime_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE opspilot_postmortem_generation_attempts
          ADD COLUMN model_calls jsonb NOT NULL DEFAULT '[]'
            CONSTRAINT opspilot_postmortem_generation_attempts_model_calls_check
            CHECK (jsonb_typeof(model_calls) = 'array');
        GRANT UPDATE (model_calls) ON opspilot_postmortem_generation_attempts
          TO opspilot_worker;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE UPDATE (model_calls) ON opspilot_postmortem_generation_attempts
          FROM opspilot_worker;
        ALTER TABLE opspilot_postmortem_generation_attempts DROP COLUMN model_calls;
        """
    )
