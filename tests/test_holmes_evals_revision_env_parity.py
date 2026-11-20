"""#722 (PR #725): the eval driver starts web and worker with the same
revision-bearing settings. ``opspilot.tools.profiles`` requires both
processes to agree on ``OPSPILOT_CLUSTER_NAME`` (it picks the prompt variant
and so ``prompt_revision``) and on the tool switches; a mismatch blocks the
claim (``INCOMPATIBLE_STATE``) before any model request. Driven through
``run_trial`` so the call site, not just the env builders, is covered."""

from __future__ import annotations

from datetime import timedelta

from tests.holmes_evals_support import mod
from tests.test_holmes_evals_trial_contract import (  # noqa: F401 - fixture
    Q262,
    START,
    _load,
    world,
)

#: Settings both processes must hold with the same value (profiles.py).
SAME_VALUE = (
    "OPSPILOT_TOOL_PROFILE",
    "OPSPILOT_CLUSTER_NAME",
    "OPSPILOT_KNOWLEDGE_TOOL",
    "OPSPILOT_TODO_WRITE",
    "OPSPILOT_K8S_LIVE_METRICS",
    "OPSPILOT_HELM_RELEASES",
    "OPSPILOT_ARGOCD",
)
#: Data-plane addresses both processes must set, or neither.
SAME_PRESENCE = (
    "OPSPILOT_ES_URL",
    "OPSPILOT_LOKI_URL",
    "OPSPILOT_VICTORIALOGS_URL",
    "OPSPILOT_SQL_URL",
    "OPSPILOT_MONGODB_URL",
    "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS",
)


def test_cluster_question_starts_web_and_worker_with_same_revision_env(
    world,  # noqa: F811 - fixture
) -> None:
    driver = mod("driver")
    h, cfg, _ = world(START, (Q262,))
    recent = (START - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    h.seed[Q262] = [
        ("app-262-prod-logs", {"timestamp": {"type": "date"}},
         [{"timestamp": recent}]),
    ]  # fmt: skip
    driver.run_trial(cfg, h.backends(), Q262, _load(cfg, Q262), 1)
    (web,) = h.web_envs
    (worker,) = h.worker_envs
    assert web.get("OPSPILOT_CLUSTER_NAME") == "production"
    for name in SAME_VALUE:
        assert web.get(name) == worker.get(name), name
    for name in SAME_PRESENCE:
        assert (name in web) == (name in worker), name
