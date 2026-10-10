"""M1-04 step 1 lab wiring: Alertmanager webhook token and the alert rule.

The token stays in the engineer-only file and the Secret Alertmanager mounts;
the workbench gets only its sha256 under the dedicated ``alertmanager`` actor
(E13). The values files carry no credential and route alerts to the pinned
Alertmanager release (E15).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
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


def _source(path: Path, existing: str | None) -> str:
    env = {"PATH": os.environ["PATH"]}
    if existing is not None:
        env["OPSPILOT_EVENT_TOKENS"] = existing
    proc = subprocess.run(
        ["sh", "-c", f'set -a; . "{path}"; printf %s "$OPSPILOT_EVENT_TOKENS"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_workbench_env_holds_hash_under_dedicated_actor(tmp_path: Path) -> None:
    token = "lab-token-value"
    path = kind_lab.workbench_event_env(token, tmp_path / "wb.env")
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert token not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _source(path, None) == f"{digest}=alertmanager"
    # review P2-1: sourcing keeps the event tokens already configured
    assert _source(path, "abc=ui-events") == f"abc=ui-events,{digest}=alertmanager"


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
    route = values["config"]["route"]
    assert route["receiver"] == receiver["name"]
    # one notification per alert identity group; every alert of a group in
    # one POST (max_alerts 0 = no truncation)
    assert route["group_by"] == ["alertname", "namespace", "service"]
    assert hook["max_alerts"] == 0
    assert route["repeat_interval"] == "1h"


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


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _roll(monkeypatch, configmap: _Proc, patch_rc: int = 0) -> tuple[int, list]:
    patches: list = []
    monkeypatch.setattr(kind_lab, "capture", lambda cmd, **kw: configmap)

    def fake_run(cmd, **kw):
        patches.append(json.loads(cmd[cmd.index("-p") + 1]))
        return _Proc(patch_rc)

    monkeypatch.setattr(kind_lab, "run", fake_run)
    return kind_lab.roll_prometheus_on_config_change(), patches


def test_roll_patches_only_the_hash_annotation(monkeypatch) -> None:
    cm = {"data": {"alerting_rules.yml": "groups: []"}}
    code, patches = _roll(monkeypatch, _Proc(stdout=json.dumps(cm)))
    assert code == 0
    # a strategic-merge patch of one annotation: other pod annotations stay,
    # an unchanged hash patches nothing (verified live, evidence run.md)
    assert patches == [
        {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            kind_lab.CONFIG_HASH_ANNOTATION: kind_lab.config_sha256(cm)
                        }
                    }
                }
            }
        }
    ]


def test_roll_fails_closed(monkeypatch) -> None:
    code, patches = _roll(monkeypatch, _Proc(returncode=1, stderr="not found"))
    assert code == 1 and patches == []
    code, patches = _roll(monkeypatch, _Proc(stdout="not json"))
    assert code == 1 and patches == []
    code, _ = _roll(monkeypatch, _Proc(stdout="{}"), patch_rc=5)
    assert code == 5


def test_readiness_covers_statefulsets(monkeypatch) -> None:
    seen: list[list[str]] = []
    items = {
        "items": [
            {
                "metadata": {"name": "checkout"},
                "spec": {"replicas": 1},
                "status": {"readyReplicas": 1},
            },
            {
                "metadata": {"name": "alertmanager"},
                "spec": {"replicas": 1},
                "status": {},
            },
        ]
    }

    def fake_capture(cmd, **kw):
        seen.append(cmd)
        return _Proc(stdout=json.dumps(items))

    monkeypatch.setattr(kind_lab, "capture", fake_capture)
    assert kind_lab.deployments_not_ready() == ["alertmanager"]
    assert "deployments,statefulsets" in seen[0]
