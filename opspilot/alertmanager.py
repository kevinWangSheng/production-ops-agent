"""Alertmanager webhook v4 intake: parsing, identity, target match, question.

Pure functions only (M1-04 step 2, contract r3 I1-I7): nothing here touches
storage, a clock or a model. The workbench (``opspilot.web.service``) drives
them per alert and the store commits the result.

The alert model follows HolmesGPT's ``PrometheusAlert`` (upstream
``46e3a72``): ``labels``, ``annotations``, ``startsAt``, ``endsAt``,
``fingerprint``, ``generatorURL``, plus the alert's own ``status``. The
identity is ``fingerprint:startsAt`` with ``startsAt`` normalized to UTC
seconds (E2); a repeated notification under that identity is a replay
whatever its annotations say (r1-B), so no comparator here looks at them.

Annotations are untrusted text and never reach the question (R3/I6); they
are kept only in the redacted, bounded audit record (E10) and, from step 4
on, as labelled untrusted context.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from unicodedata import category
from urllib.parse import parse_qs, urlsplit

from opspilot.domain.intake import delivery_key
from opspilot.intake import _reject_ambiguous_identifier
from opspilot.tools.registry import (
    _AUTHENTICATION_KEYS,
    REDACTED_CREDENTIAL,
    _authentication_name,
    canonical,
    canonical_hash,
    redact_credentials,
)

SOURCE = "alertmanager"
#: I7: one alert's redacted audit JSON, in UTF-8 bytes.
MAX_ALERT_RECORD_BYTES = 16 * 1024
#: I6: every interpolated field, and the PromQL expression.
MAX_FIELD_CHARS = 256
MAX_PROMQL_CHARS = 2048
TRUNCATED = " [truncated]"

AlertStatus = Literal["firing", "resolved"]
HandoffReason = Literal["TARGET_UNRESOLVED", "TARGET_AMBIGUOUS"]

#: RFC 3339 date-time (Alertmanager writes Go's ``time.Time`` JSON form):
#: a full date, ``T``, a time with optional fraction and a mandatory offset.
_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})"
)


class InvalidPayload(ValueError):
    """The webhook body as a whole is not an Alertmanager v4 object (I1)."""


class InvalidAlert(ValueError):
    """One alert cannot be identified (R2); ``code`` is safe to return."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Alert:
    """One parsed alert of a webhook, with its normalized identity."""

    status: AlertStatus
    fingerprint: str
    #: UTC seconds, ``YYYY-MM-DDTHH:MM:SSZ`` (E2, I3).
    starts_at: str
    starts_at_time: datetime
    #: The string the payload carried, kept as received (E2).
    starts_at_raw: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    generator_url: str | None
    #: The alert object exactly as parsed, for the audit hash and record.
    raw: Mapping[str, Any]

    @property
    def delivery_key(self) -> str:
        return delivery_key(
            source=SOURCE, external_event_id=f"{self.fingerprint}:{self.starts_at}"
        )


def parse_payload(payload: object) -> Sequence[object]:
    """The ``alerts`` list of a v4 webhook object, else ``InvalidPayload``."""
    if not isinstance(payload, dict):
        raise InvalidPayload("not an object")
    if payload.get("version") != "4":
        raise InvalidPayload("version")
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise InvalidPayload("alerts")
    return alerts


def parse_alert(item: object) -> Alert:
    """One alert of the ``alerts`` list (R2: missing identity -> invalid)."""
    if not isinstance(item, dict):
        raise InvalidAlert("ALERT_NOT_OBJECT")
    try:
        # Refuses lone surrogates and anything else that has no canonical
        # form: such an alert has no hash and could not be stored.
        canonical(item).encode("utf-8")
    except ValueError:
        raise InvalidAlert("ALERT_NOT_ENCODABLE") from None
    status = item.get("status")
    if status not in ("firing", "resolved"):
        raise InvalidAlert("STATUS_INVALID")
    fingerprint = item.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise InvalidAlert("FINGERPRINT_MISSING")
    try:
        _reject_ambiguous_identifier(fingerprint)
    except ValueError:
        raise InvalidAlert("FINGERPRINT_INVALID") from None
    if len(fingerprint) > 256:
        raise InvalidAlert("FINGERPRINT_INVALID")
    raw_start = item.get("startsAt")
    if not isinstance(raw_start, str) or not raw_start:
        raise InvalidAlert("STARTS_AT_MISSING")
    starts_at_time = _parse_time(raw_start)
    labels = item.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise InvalidAlert("LABELS_MISSING")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
        raise InvalidAlert("LABELS_INVALID")
    annotations = item.get("annotations")
    if annotations is None:
        annotations = {}
    if not isinstance(annotations, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in annotations.items()
    ):
        raise InvalidAlert("ANNOTATIONS_INVALID")
    generator = item.get("generatorURL")
    return Alert(
        status=status,
        fingerprint=fingerprint,
        starts_at=_seconds(starts_at_time),
        starts_at_time=starts_at_time,
        starts_at_raw=raw_start,
        labels=dict(labels),
        annotations=dict(annotations),
        generator_url=generator if isinstance(generator, str) else None,
        raw=item,
    )


def _parse_time(raw: str) -> datetime:
    if not _RFC3339.fullmatch(raw):
        raise InvalidAlert("STARTS_AT_INVALID")
    try:
        parsed = datetime.fromisoformat(raw.upper())
        return parsed.astimezone(UTC).replace(microsecond=0)
    except (ValueError, OverflowError):
        raise InvalidAlert("STARTS_AT_INVALID") from None


def _seconds(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


# -- target resolution (I4) ----------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """The registry's answer for one alert: one target or a handoff reason."""

    target_id: str | None
    namespace: str | None
    workload: str | None
    handoff_reason: HandoffReason | None


def resolve_target(matches: Sequence[Any]) -> Resolution:
    """Exactly one matching entry binds; zero or several hand off (E5, E7)."""
    if len(matches) == 1:
        entry = matches[0]
        return Resolution(entry.resource_uid, entry.namespace, entry.workload, None)
    reason: HandoffReason = "TARGET_UNRESOLVED" if not matches else "TARGET_AMBIGUOUS"
    return Resolution(None, None, None, reason)


# -- question template (I6) ----------------------------------------------


def promql_of(generator_url: str | None) -> str | None:
    """The ``g0.expr`` of a Prometheus graph link, or ``None``."""
    if not generator_url:
        return None
    try:
        values = parse_qs(urlsplit(generator_url).query).get("g0.expr")
    except ValueError:
        return None
    if not values or not values[0].strip():
        return None
    return values[0]


def _field(value: str | None, limit: int = MAX_FIELD_CHARS) -> str:
    """A template field: redacted, made printable, then bounded."""
    if value is None or not value:
        return "(none)"
    text = "".join(
        "�" if category(char) in {"Cc", "Cf", "Zl", "Zp"} else char
        for char in redact_credentials(value)
    )
    if len(text) > limit:
        return text[:limit] + TRUNCATED
    return text


def question_for(alert: Alert, resolution: Resolution) -> str:
    """The deterministic first question of an alert's Run (I6).

    Only the alert name, severity, the bound target's namespace and
    workload, the normalized ``startsAt`` and the alert expression; never
    an annotation. Same input, same bytes.
    """
    lines = [
        f"Alertmanager alert {_field(alert.labels.get('alertname'))} "
        f"(severity {_field(alert.labels.get('severity'))}) is firing for "
        f"service {_field(resolution.namespace)}/{_field(resolution.workload)} "
        f"since {alert.starts_at}.",
    ]
    promql = promql_of(alert.generator_url)
    if promql is not None:
        lines.append(f"Alert expression (PromQL): {_field(promql, MAX_PROMQL_CHARS)}")
    lines.append("Investigate why this alert is firing.")
    return "\n".join(lines)


# -- audit record (I7, E9, E10) ------------------------------------------


@dataclass(frozen=True)
class AlertRecord:
    """What one delivery stores: the redacted bounded JSON and the hashes."""

    alert_json: str
    truncated: bool
    raw_sha256: str
    annotations_sha256: str


def _redacted(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            k: _redacted(v, k if isinstance(k, str) else None) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    if isinstance(value, str):
        if key is not None and (
            key.lower() in _AUTHENTICATION_KEYS or _authentication_name(key)
        ):
            return REDACTED_CREDENTIAL
        return redact_credentials(value)
    return value


def record_of(alert: Alert) -> AlertRecord:
    """The redacted, bounded audit record of one alert plus its hashes.

    ``raw_sha256`` is over the canonical JSON of the alert as received
    (before redaction); the stored text is the canonical JSON of the
    redacted alert, cut at ``MAX_ALERT_RECORD_BYTES`` on a character
    boundary when longer (``truncated``), so it is valid JSON exactly when
    it was not cut.
    """
    text = canonical(_redacted(dict(alert.raw)))
    encoded = text.encode("utf-8")
    truncated = len(encoded) > MAX_ALERT_RECORD_BYTES
    if truncated:
        text = encoded[:MAX_ALERT_RECORD_BYTES].decode("utf-8", errors="ignore")
    return AlertRecord(
        alert_json=text,
        truncated=truncated,
        raw_sha256=canonical_hash(dict(alert.raw)),
        annotations_sha256=canonical_hash(dict(alert.annotations)),
    )
