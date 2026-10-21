"""Shared builders for the independent F11 PR-3 acceptance tests.

Written from ``docs/tasks/2026-10-21-f11-pr3-contract.md`` (「建议公开接口草案」,
D3 error mapping and the 16 observable behaviours) and the PR-3 section of
``docs/tasks/2026-10-12-f11-post-release.md`` without reading the
implementation. Only existing public interfaces are used: ``create_app``,
``Workbench``, ``MappingTargetRegistry``, ``ReleaseStore`` and the ASGI driver
of ``tests.m1_web_support``.

Ambiguity A0 (wiring): the contract does not say how ``create_app`` obtains
the ``ReleaseStore``. :func:`build_app` therefore passes the store to the
first ``create_app`` (else ``Workbench``) parameter whose name contains
``release``. If neither has one the app is built without it, so every PR-3
test fails on the missing route (404) instead of erroring.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from opspilot.observer import PROFILE_DIRECTORY, load_health_profile
from opspilot.persistence import PersistenceError
from opspilot.web import (
    AuthConfig,
    Authenticator,
    MemoryEventLog,
    MemoryEvidenceStore,
    MemoryWebLedger,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from opspilot.web.store import MappingTargetRegistry
from tests.m1_tool_support import NOW, FakeClock
from tests.m1_web_support import (
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    MemoryIncidentStore,
    call,
)

ROUTE = "/intake/releases"
RELEASE_ACTOR = "release-deployer"
RELEASE_TOKEN = "release-deployer-token-for-tests-only"
ALERT_ACTOR = "alertmanager"
ALERT_TOKEN = "alertmanager-token-for-tests-only"
OTHER_ACTOR = "ops-bot"
OTHER_TOKEN = "other-event-token-for-tests-only"

PROFILE = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
DEADLINE_SECONDS = PROFILE.session.deadline_seconds  # 3600
#: Lead decision 2026-10-21 ("M 值用户选 1500"): min_tracking_seconds.
MIN_TRACKING_SECONDS = 1500

TARGET = {
    "resource_uid": "deployment/checkout-pr3",
    "integration_id": "kind-lab",
    "cluster_uid": "opspilot-kind",
    "namespace": "otel-demo",
    "workload": "checkout",
}
REGISTRY = {
    TARGET["resource_uid"]: {
        "integration_id": TARGET["integration_id"],
        "cluster_uid": TARGET["cluster_uid"],
        "namespace": TARGET["namespace"],
        "workload": TARGET["workload"],
        "health_profile_id": PROFILE.profile_id,
    },
    "deployment/payment-pr3": {
        "integration_id": TARGET["integration_id"],
        "cluster_uid": TARGET["cluster_uid"],
        "namespace": TARGET["namespace"],
        "workload": "payment",
        "health_profile_id": PROFILE.profile_id,
    },
}


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def release_payload(
    *,
    released_at: datetime | None = None,
    release_id: str = "u2-configuration-001",
    source: str = "kind-harness",
    target: dict[str, str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """The contract's example payload, with ``released_at`` five minutes ago."""
    payload: dict[str, Any] = {
        "format_version": 1,
        "source": source,
        "source_release_id": release_id,
        "released_at": iso(released_at or utc_now() - timedelta(minutes=5)),
        "target": dict(TARGET if target is None else target),
        "before": {
            "images": {"checkout": digest("before-image")},
            "pod_template_digest": digest("before-template"),
        },
        "after": {
            "images": {"checkout": digest("after-image")},
            "pod_template_digest": digest("after-template"),
        },
        "auxiliary": {
            "deployment_uid": "dep-uid-1",
            "before_replicaset_uid": "rs-uid-before",
            "after_replicaset_uid": "rs-uid-after",
            "before_pod_template_hash": "5f6b7c8d9e",
            "after_pod_template_hash": "7d4b9c8f6d",
            "deployment_generation": 12,
        },
    }
    payload.update(overrides)
    return payload


def raw(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True).encode()


def bearer_for(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def post_release(
    app: Any,
    payload: Any = None,
    *,
    body: bytes | None = None,
    token: str | None = RELEASE_TOKEN,
    headers: dict[str, str] | None = None,
):
    merged = {"content-type": "application/json"}
    if token is not None:
        merged.update(bearer_for(token))
    merged.update(headers or {})
    data = (
        body
        if body is not None
        else raw(release_payload() if payload is None else payload)
    )
    return call(app, "POST", ROUTE, headers=merged, body=data)


def error_code(response: Any) -> str | None:
    """Ambiguity A2: the contract names the codes but not the JSON key; the
    existing refusals use ``code`` and the Alertmanager route uses ``error``."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    return body.get("code") or body.get("error")


def assert_route_exists(response: Any) -> None:
    """A red-on-main guard: the feature is missing, not the test broken."""
    assert response.status != 404, (
        "POST /intake/releases is not routed (F11 PR-3 feature missing); "
        f"got {response.status} {response.text[:200]!r}"
    )
    assert response.status != 405, (
        "POST /intake/releases rejects POST (feature missing)"
    )


class RecordingReleaseStore:
    """Stands in for ``ReleaseStore`` in the no-PostgreSQL tests.

    Every attribute is a recorder that answers like an unavailable database
    (``PersistenceError('STORAGE_UNAVAILABLE')``). A request that must be
    refused before storage leaves ``calls`` empty; a request that reaches
    storage and meets the failure must come back 503 (behaviour 10).
    """

    def __init__(self, code: str = "STORAGE_UNAVAILABLE") -> None:
        self.calls: list[str] = []
        self._code = code

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        def recorder(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            raise PersistenceError(self._code)

        return recorder


def config(*, extra_tokens: dict[str, str] | None = None) -> AuthConfig:
    tokens = {
        token_digest(RELEASE_TOKEN): RELEASE_ACTOR,
        token_digest(ALERT_TOKEN): ALERT_ACTOR,
        token_digest(OTHER_TOKEN): OTHER_ACTOR,
    }
    tokens.update({token_digest(t): a for t, a in (extra_tokens or {}).items()})
    return AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD, salt=b"fixed-salt-16byt")},
        event_tokens=tokens,
        auth_revision="auth-rev-pr3",
        allowed_origins=frozenset({ORIGIN}),
    )


def _release_param(func: Any) -> str | None:
    for name in inspect.signature(func).parameters:
        if "release" in name:
            return name
    return None


def build_app(
    releases: Any,
    *,
    registry: dict[str, dict[str, Any]] | None = None,
    workbench_kwargs: dict[str, Any] | None = None,
    clock: Any = None,
):
    """An app with release-deployer / alertmanager / ops-bot event tokens.

    ``workbench_kwargs`` lets the PostgreSQL tests substitute durable stores.
    """
    clock = clock or FakeClock(start=NOW)
    wb_kwargs: dict[str, Any] = {
        "incidents": MemoryIncidentStore(clock),
        "events": MemoryEventLog(),
        "evidence": MemoryEvidenceStore(),
        "ledger": MemoryWebLedger(),
        "run_versions": {"state": "v1"},
        "targets": MappingTargetRegistry(REGISTRY if registry is None else registry),
        "health_profile": PROFILE,
        "run_seconds": 600,
        "tool_face": None,
    }
    wb_kwargs.update(workbench_kwargs or {})
    app_kwargs: dict[str, Any] = {
        "sse_poll_seconds": 0.01,
        "sse_idle_seconds": 0.2,
    }
    wb_param = _release_param(Workbench.__init__)
    app_param = _release_param(create_app)
    if wb_param is not None:
        wb_kwargs[wb_param] = releases
    if app_param is not None:
        app_kwargs[app_param] = releases
    workbench = Workbench(**wb_kwargs)
    app = create_app(workbench, Authenticator(config()), clock, **app_kwargs)
    return app, workbench
