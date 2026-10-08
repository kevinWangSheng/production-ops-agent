"""One target identity for the tests that register a remediation.

Intake registers a target by ``resource_uid`` alone (M1-01 contract); the
rest of the identity (migration 0004) is completed from the configured
registry when a remediation is first registered on the target. Tests that
drive that path use this one identity. The values are synthetic test
labels, not the lab's real identity.
"""

from __future__ import annotations

from opspilot.web.store import TargetIdentity, TargetRegistry

#: ``profile_id`` of the shipped checkout profile the tests register under.
PROFILE_ID = "otel-demo-checkout"
#: Namespace and workload the shipped checkout profile's ``subject`` names;
#: an authorization under that profile requires exactly these.
NAMESPACE = "otel-demo"
WORKLOAD = "checkout"
IDENTITY = {
    "integration_id": "test-integration",
    "cluster_uid": "test-cluster",
    "namespace": NAMESPACE,
}


def identity_of(resource_uid: str, *, workload: str = WORKLOAD) -> dict[str, str]:
    """The identity (plus workload) ``ObservationStore`` takes for ``resource_uid``."""
    return {**IDENTITY, "resource_uid": resource_uid, "workload": workload}


class AnyTargetRegistry(TargetRegistry):
    """Resolves every operator target id to the shared identity (tests only;
    the product registry knows only the ids its file lists)."""

    def resolve(self, target_id: str) -> TargetIdentity | None:
        return TargetIdentity(
            resource_uid=target_id,
            health_profile_id=PROFILE_ID,
            workload=WORKLOAD,
            **IDENTITY,
        )


ANY_TARGETS = AnyTargetRegistry()
