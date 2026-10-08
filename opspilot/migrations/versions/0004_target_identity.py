"""0004 target identity: the full immutable Target on ``opspilot_targets``.

Revision ID: 0004_target_identity
Revises: 0003_observation_store
Create Date: 2026-10-07

M1-02 step 3 (issue #85; user decisions 2026-10-07). A target's identity is
``integration_id``, ``cluster_uid``, ``namespace`` and ``resource_uid``
(``opspilot.domain.intake.Target`` minus ``revision``, which evolves with
every rollout and is only snapshotted on an observation session for the
audit). The registry so far recorded ``resource_uid`` alone, and the
observation store fixed the other fields from the first session authorized
on the target (PR #114 stop-gap). From this revision the registry can carry
the whole identity, and authorizing an observation reads it back instead of
trusting a caller.

Intake keeps registering a target by ``resource_uid`` alone (M1-01 intake
contract unchanged, user decision 2026-10-07 on the PR #120 review): the
three new columns are nullable, existing rows stay as they are and the
migration needs no input. A complete identity is required only where it is
used -- registering a remediation / authorizing an observation -- which
fails closed on a row that lacks it (``TARGET_IDENTITY_MISSING``) and
completes it from the configured identity file the first time; once
written, the identity never changes in place (``TARGET_MISMATCH``). A
present value must be a non-empty string. ``workload`` travels with the
identity and is what the HealthProfile's ``subject`` is compared with.
"""

from collections.abc import Sequence

from alembic import op

from opspilot.schema import TARGET_IDENTITY_FIELDS

# The workload (Deployment / service name) the target's HealthProfile
# observes: not identity, but the field an authorization compares with the
# profile's ``subject`` so one profile cannot certify another target's
# recovery (PR #120 bot review P1). Nullable like the identity columns,
# completed with them.
BINDING_COLUMNS = ("workload",)

revision: str = "0004_target_identity"
down_revision: str | Sequence[str] | None = "0003_observation_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in TARGET_IDENTITY_FIELDS + BINDING_COLUMNS:
        op.execute(
            f"ALTER TABLE opspilot_targets ADD COLUMN {column} text "
            f"CONSTRAINT opspilot_targets_{column}_check CHECK ({column} IS NULL OR {column} <> '')"
        )


def downgrade() -> None:
    for column in TARGET_IDENTITY_FIELDS + BINDING_COLUMNS:
        op.execute(f"ALTER TABLE opspilot_targets DROP COLUMN {column}")
