"""One target identity for every test that registers or submits a target.

Since migration 0004 the registry (``opspilot_targets``) carries the whole
immutable identity (integration, cluster, namespace, resource uid), and the
workbench resolves an operator's ``target_id`` through a configured
registry before intake registers it. Tests that pre-register a target and
then submit an incident for it must use the same identity on both paths,
or the registration is an ``IDENTITY_CONFLICT``; this module is that one
place. The values are synthetic test labels, not the lab's real identity.
"""

from __future__ import annotations

from opspilot.web.store import TargetIdentity, TargetRegistry

IDENTITY = {
    "integration_id": "test-integration",
    "cluster_uid": "test-cluster",
    "namespace": "test-namespace",
}


def register(store, resource_uid):
    """``store.register_target`` with the shared identity."""
    return store.register_target(resource_uid, **IDENTITY)


class AnyTargetRegistry(TargetRegistry):
    """Resolves every operator target id to the shared identity (tests only;
    the product registry refuses an id it does not know)."""

    def resolve(self, target_id: str) -> TargetIdentity | None:
        return TargetIdentity(resource_uid=target_id, **IDENTITY)


ANY_TARGETS = AnyTargetRegistry()
