"""Implementer check for #319 on PostgreSQL: a release delivery whose raw body
spells a URL userinfo with JSON-escaped slashes keeps no secret in its audit
text, while ``raw_sha256`` still covers the exact bytes sent."""

# ruff: noqa: F401, F811 - pytest fixture imported from the F11 PR-3 module
from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import pytest

from opspilot.observation.releases import ReleaseStore
from opspilot.redaction import REDACTED_CREDENTIAL
from scripts.m0.postgres_lab import DSN
from tests.test_f11_pr3_impl_intake_postgres import (
    _app,
    _payload,
    _post,
    _registry,
    web_dsn,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

SECRET = "S3cretPw-319-release"
S, U = "\\/", "\\u002f"


def test_release_audit_text_redacts_escaped_userinfo(web_dsn):
    uid = "i319-" + uuid4().hex
    registry = _registry(uid)
    app = _app(web_dsn, registry)
    head = json.dumps(_payload(uid, registry))[:-1]
    # unknown top-level keys reach only the audit text; the slashes are
    # written escaped, as a JSON producer may send them
    raw = (
        head
        + f', "note": "postgres:{S}{S}svc:{SECRET}@db{S}x",'
        + f' "mirror": "https:{U}{S}ops:{SECRET}@prom:9090"}}'
    ).encode()
    response = _post(app, None, raw=raw)
    assert response.status == 201, response.body
    observation_id = json.loads(response.body)["observation_id"]
    [event] = ReleaseStore(DSN).release_events(observation_id)
    assert event.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert SECRET not in event.audit_text
    assert f"postgres:{S}{S}{REDACTED_CREDENTIAL}@db{S}x" in event.audit_text
    assert f"https:{U}{S}{REDACTED_CREDENTIAL}@prom:9090" in event.audit_text
