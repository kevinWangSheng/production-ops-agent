"""A2-5 / decision table r3: the A2 static resource table is a subset of the
A1 read-only ClusterRole (whichever of A1 and A2 merged later adds this test).

Every (API group, plural) the cluster object tools can list must be granted
by ``scripts/kind_lab/opspilot-readonly-rbac.yaml``; otherwise a lab Run would
see SOURCE_FORBIDDEN for a kind the tool advertises.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from opspilot.tools.kubernetes_core import RESOURCES

RBAC = (
    Path(__file__).resolve().parents[1] / "scripts/kind_lab/opspilot-readonly-rbac.yaml"
)


def _granted() -> set[tuple[str, str]]:
    granted: set[tuple[str, str]] = set()
    for doc in yaml.safe_load_all(RBAC.read_text()):
        if not doc or doc.get("kind") != "ClusterRole":
            continue
        for rule in doc.get("rules", []):
            if "list" not in rule.get("verbs", []):
                continue
            for group in rule.get("apiGroups", []):
                for resource in rule.get("resources", []):
                    granted.add((group, resource))
    return granted


def test_every_listable_kind_is_granted_list_by_the_a1_cluster_role() -> None:
    granted = _granted()
    assert granted, "the A1 ClusterRole grants nothing"
    missing = sorted(
        (r.group, r.plural)
        for r in RESOURCES.values()
        if (r.group, r.plural) not in granted
    )
    assert missing == []


def test_secrets_are_neither_listable_nor_granted() -> None:
    assert "secrets" not in RESOURCES
    assert all(resource != "secrets" for _, resource in _granted())
