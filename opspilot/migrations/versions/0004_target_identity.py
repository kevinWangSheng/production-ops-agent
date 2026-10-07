"""0004 target identity: the full immutable Target on ``opspilot_targets``.

Revision ID: 0004_target_identity
Revises: 0003_observation_store
Create Date: 2026-10-07

M1-02 step 3 (issue #85; user decision 2026-10-07). A target's identity is
``integration_id``, ``cluster_uid``, ``namespace`` and ``resource_uid``
(``opspilot.domain.intake.Target`` minus ``revision``, which evolves with
every rollout and is only snapshotted on an observation session for the
audit). The registry so far recorded ``resource_uid`` alone, and the
observation store fixed the other fields from the first session authorized
on the target (PR #114 stop-gap). From this revision the registry carries
the whole identity, written at intake from the configured target registry,
and authorizing an observation reads it back instead of trusting a caller.

Existing rows cannot be completed from the database itself, and the
migration never invents an identity. It reads ``OPSPILOT_TARGET_IDENTITIES``
(the same JSON file the workbench resolves intake targets from, see
``opspilot.schema.load_target_identities``) and fails closed with
:class:`opspilot.schema.TargetIdentityMissing` -- listing every uncovered
``resource_uid`` -- when a registered target has no entry, when the file is
unset while rows exist, or when an entry is malformed. Nothing is altered
then (``transaction_per_migration``); the database stays at 0003.

``resource_uid`` stays the unique registry key (it is what intake resolves
by and what ``opspilot_target_suspensions`` scopes); the three new columns
are NOT NULL and non-empty.
"""

import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from opspilot.schema import (
    TARGET_IDENTITIES_ENV,
    TARGET_IDENTITY_FIELDS,
    TargetIdentityMissing,
    load_target_identities,
)

revision: str = "0004_target_identity"
down_revision: str | Sequence[str] | None = "0003_observation_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    registered = [
        str(row[0])
        for row in bind.execute(
            text("SELECT resource_uid FROM opspilot_targets ORDER BY resource_uid")
        ).fetchall()
    ]
    identities: dict[str, dict[str, str]] = {}
    if registered:
        path = os.environ.get(TARGET_IDENTITIES_ENV)
        if not path:
            raise TargetIdentityMissing(
                registered, detail=f"{TARGET_IDENTITIES_ENV} is not set"
            )
        identities = load_target_identities(path)
        missing = [uid for uid in registered if uid not in identities]
        if missing:
            raise TargetIdentityMissing(missing)
    for column in TARGET_IDENTITY_FIELDS:
        op.execute(f"ALTER TABLE opspilot_targets ADD COLUMN {column} text")
    for uid in registered:
        bind.execute(
            text(
                "UPDATE opspilot_targets SET integration_id=:integration_id, cluster_uid=:cluster_uid, namespace=:namespace WHERE resource_uid=:uid"
            ),
            {**identities[uid], "uid": uid},
        )
    for column in TARGET_IDENTITY_FIELDS:
        op.execute(
            f"ALTER TABLE opspilot_targets ALTER COLUMN {column} SET NOT NULL, "
            f"ADD CONSTRAINT opspilot_targets_{column}_check CHECK ({column} <> '')"
        )


def downgrade() -> None:
    for column in TARGET_IDENTITY_FIELDS:
        op.execute(f"ALTER TABLE opspilot_targets DROP COLUMN {column}")
