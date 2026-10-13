"""Shared registry identity and immutable profile persistence primitives."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from uuid import UUID

from opspilot.domain.intake import Target
from opspilot.persistence.base import Connection, PersistenceError, _StoreBase

_IDENTITY_FIELDS = ("integration_id", "cluster_uid", "namespace", "resource_uid")
_BINDING_FIELDS = ("workload",)


class _TargetProfileOps(_StoreBase):
    def _store_profile(
        self, conn: Connection, revision: str, content: str | None
    ) -> None:
        """Keep the profile content behind a revision, once per revision.

        The revision must be ``<profile_id>@<sha256(content)[:12]>``; a
        revision already stored must carry the same content hash (a 12-hex
        prefix is not unique by construction, the full hash is checked).
        """
        if not isinstance(content, str) or not content:
            raise PersistenceError("INVALID_INPUT")
        profile_id, separator, suffix = revision.rpartition("@")
        digest = hashlib.sha256(content.encode()).hexdigest()
        if not separator or not profile_id or suffix != digest[:12]:
            raise PersistenceError("HEALTH_PROFILE_REVISION_MISMATCH")
        existing = conn.execute(
            "SELECT content_sha256 FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (revision,),
        ).fetchone()
        if existing is not None:
            if existing["content_sha256"] != digest:
                raise PersistenceError("HEALTH_PROFILE_REVISION_MISMATCH")
            return
        conn.execute(
            "INSERT INTO opspilot_health_profiles(health_profile_revision,profile_id,content_sha256,content) VALUES(%s,%s,%s,%s) ON CONFLICT (health_profile_revision) DO NOTHING",
            (revision, profile_id, digest, content),
        )

    def _complete_identity(
        self, conn: Connection, target_id: UUID, identity: Mapping[str, str] | None
    ) -> None:
        """Write the identity columns of a bare registry row once; a complete
        row must agree with ``identity`` when one is given. The caller holds
        the incident row lock; the registry row is locked here."""
        fields = _IDENTITY_FIELDS + _BINDING_FIELDS
        if identity is not None and (
            set(identity) != set(fields)
            or any(
                not isinstance(identity[field], str) or not identity[field]
                for field in fields
            )
        ):
            raise PersistenceError("INVALID_INPUT")
        row = self._require_row(
            conn.execute(
                "SELECT integration_id,cluster_uid,namespace,resource_uid,workload FROM opspilot_targets WHERE target_id=%s FOR UPDATE",
                (target_id,),
            )
        )
        if identity is not None and identity["resource_uid"] != row["resource_uid"]:
            raise PersistenceError("TARGET_MISMATCH")
        complete = all(
            row[field] is not None for field in fields if field != "resource_uid"
        )
        if complete:
            if identity is not None and any(
                row[field] != identity[field] for field in fields
            ):
                raise PersistenceError("TARGET_MISMATCH")
            return
        if identity is None:
            raise PersistenceError("TARGET_IDENTITY_MISSING")
        conn.execute(
            "UPDATE opspilot_targets SET integration_id=%s,cluster_uid=%s,namespace=%s,workload=%s WHERE target_id=%s AND integration_id IS NULL AND cluster_uid IS NULL AND namespace IS NULL AND workload IS NULL",
            (
                identity["integration_id"],
                identity["cluster_uid"],
                identity["namespace"],
                identity["workload"],
                target_id,
            ),
        )

    def _registered_target(
        self, conn: Connection, target_id: UUID, revision: str | None
    ) -> Target:
        """The immutable identity the registry holds for ``target_id`` plus
        the revision snapshot of this authorization; a row without the
        identity cannot be observed (``TARGET_IDENTITY_MISSING``)."""
        row = self._require_row(
            conn.execute(
                "SELECT integration_id,cluster_uid,namespace,resource_uid FROM opspilot_targets WHERE target_id=%s",
                (target_id,),
            )
        )
        if any(row[field] is None for field in _IDENTITY_FIELDS):
            raise PersistenceError("TARGET_IDENTITY_MISSING")
        return Target(
            integration_id=row["integration_id"],
            cluster_uid=row["cluster_uid"],
            namespace=row["namespace"],
            resource_uid=row["resource_uid"],
            revision=revision,
        )
