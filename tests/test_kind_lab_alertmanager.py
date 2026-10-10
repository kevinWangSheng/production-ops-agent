"""M1-04 step 1 lab wiring: Alertmanager webhook token and the alert rule.

The token stays in the engineer-only file and the Secret Alertmanager mounts;
the workbench gets only its sha256 under the dedicated ``alertmanager`` actor
(E13). The values files carry no credential and route alerts to the pinned
Alertmanager release (E15).
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest
import yaml

from scripts import kind_lab

CONFIG = Path(kind_lab.CONFIG)


def test_webhook_token_created_private_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "token"
    first = kind_lab.webhook_token(path)
    assert len(first) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert kind_lab.webhook_token(path) == first


def test_webhook_token_refuses_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "token"
    kind_lab.webhook_token(path)
    path.chmod(0o644)
    with pytest.raises(SystemExit):
        kind_lab.webhook_token(path)


def test_workbench_env_holds_hash_under_dedicated_actor(tmp_path: Path) -> None:
    token = "lab-token-value"
    path = kind_lab.workbench_event_env(token, tmp_path / "wb.env")
    content = path.read_text()
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert content == f"OPSPILOT_EVENT_TOKENS={digest}=alertmanager\n"
    assert token not in content
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_alertmanager_values_read_token_from_mounted_secret() -> None:
    raw = (CONFIG / "alertmanager-values.yaml").read_text()
    values = yaml.safe_load(raw)
    (receiver,) = values["config"]["receivers"]
    (hook,) = receiver["webhook_configs"]
    auth = hook["http_config"]["authorization"]
    assert auth == {
        "type": "Bearer",
        "credentials_file": "/etc/alertmanager/opspilot/token",
    }
    assert "credentials" not in auth
    assert hook["url"].endswith("/intake/alertmanager")
    assert hook["send_resolved"] is True
    (mount,) = values["extraSecretMounts"]
    assert mount["secretName"] == kind_lab.WEBHOOK_SECRET
    assert mount["mountPath"] == "/etc/alertmanager/opspilot"
    assert values["config"]["route"]["receiver"] == receiver["name"]


def test_prometheus_sends_to_pinned_release_and_carries_the_rule() -> None:
    values = yaml.safe_load((CONFIG / "values.yaml").read_text())
    prometheus = values["prometheus"]
    (target_group,) = prometheus["server"]["alertmanagers"]
    assert target_group["static_configs"][0]["targets"] == [
        f"{kind_lab.AM_RELEASE}.{kind_lab.NAMESPACE}.svc:9093"
    ]
    groups = prometheus["serverFiles"]["alerting_rules.yml"]["groups"]
    rules = [rule for group in groups for rule in group["rules"]]
    (rule,) = rules
    assert rule["alert"] == "CheckoutPlaceOrderErrorRatioHigh"
    # the labels the workbench target registry will match (E6)
    assert '"namespace"' in rule["expr"] and '"service"' in rule["expr"]
    assert rule["labels"]["cluster"] == "opspilot-m1"
    assert {"summary", "description"} <= set(rule["annotations"])


def test_alertmanager_release_is_pinned() -> None:
    assert kind_lab.AM_CHART_VERSION == "1.24.0"


def test_config_hash_tracks_configmap_data_only() -> None:
    base = {"metadata": {"resourceVersion": "1"}, "data": {"a": "1", "b": "2"}}
    same = {"metadata": {"resourceVersion": "9"}, "data": {"b": "2", "a": "1"}}
    changed = {"data": {"a": "1", "b": "3"}}
    assert kind_lab.config_sha256(base) == kind_lab.config_sha256(same)
    assert kind_lab.config_sha256(base) != kind_lab.config_sha256(changed)
