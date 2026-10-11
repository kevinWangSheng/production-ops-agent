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

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from unicodedata import category
from urllib.parse import parse_qs, urlsplit

from opspilot.domain.intake import delivery_key
from opspilot.intake import _reject_ambiguous_identifier
from opspilot.investigation.context import (
    ALERT_CONTEXT_KEY_CHARS,
    ALERT_CONTEXT_TOTAL_CHARS,
    ALERT_CONTEXT_UNPRINTABLE,
    ALERT_CONTEXT_VALUE_CHARS,
)
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
    """``value`` with every credential-named key's value replaced whole --
    a string, number or nested object alike (M1-04 step 4 review P1-1) --
    and every other string passed through ``redact_credentials``."""
    if key is not None and value is not None and _credential_key(key):
        return REDACTED_CREDENTIAL
    if isinstance(value, dict):
        return {
            k: _redacted(v, k if isinstance(k, str) else None) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    if isinstance(value, str):
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


# -- untrusted alert context (step 4, K1-K4) -------------------------------


def _credential_key(key: str) -> bool:
    return key.lower() in _AUTHENTICATION_KEYS or _authentication_name(key)


def _json_redacted(text: str) -> str:
    """``text`` that is a JSON object or array, redacted by key like the
    audit record (``_redacted``): a key the text rules cannot see (spelled
    with a ``\\u`` escape) still names a credential once parsed. Re-written
    canonically only when that changed something; any other text as is.
    A function of the already ``redact_credentials``-ed text, so the
    fallback rebuild from the audit record reaches the same bytes (K6).

    JSON-looking text that does not parse (cut, malformed, nested too deep)
    is kept as the shared text rules left it: they redact a credential key's
    value in any spelling, escaped or not, without parsing (M1-04 step 4
    recheck P1-1)."""
    if not text.lstrip().startswith(("{", "[")):
        return text
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return text
    if not isinstance(parsed, dict | list):
        return text
    try:
        redacted = _redacted(parsed)
    except RecursionError:
        return REDACTED_CREDENTIAL  # too deep to check: fail closed
    return text if redacted == parsed else canonical(redacted)


def _context_text(text: str, limit: int) -> tuple[str, bool]:
    """K2: redacted, made printable (I6 rule), then bounded; and whether cut."""
    printable = "".join(
        "\ufffd" if category(char) in ALERT_CONTEXT_UNPRINTABLE else char
        for char in _json_redacted(redact_credentials(text))
    )
    if len(printable) > limit:
        return printable[:limit] + TRUNCATED, True
    return printable, False


def _context_entries(
    entries: Mapping[str, str],
) -> tuple[list[tuple[str, str]], bool, int]:
    """One object's processed entries in key order, whether any was cut, and
    how many collided with an earlier entry's processed key (dropped)."""
    processed: list[tuple[str, str, str]] = []
    cut = False
    for raw_key, raw_value in entries.items():
        key, key_cut = _context_text(raw_key, ALERT_CONTEXT_KEY_CHARS)
        if _credential_key(raw_key):
            value, value_cut = REDACTED_CREDENTIAL, False
        else:
            value, value_cut = _context_text(raw_value, ALERT_CONTEXT_VALUE_CHARS)
        cut = cut or key_cut or value_cut
        processed.append((key, raw_key, value))
    processed.sort()
    kept: list[tuple[str, str]] = []
    collided = 0
    for key, _raw_key, value in processed:
        if kept and kept[-1][0] == key:
            collided += 1
            continue
        kept.append((key, value))
    return kept, cut, collided


def alert_context(
    labels: Mapping[str, str], annotations: Mapping[str, str]
) -> dict[str, Any]:
    """The ``alert_context`` scope fact of an alert Run (K2-K4).

    Only ``labels`` and ``annotations``. Keys and values are redacted by the
    audit record's rule (a credential-named key's value is the placeholder,
    everything else passes ``redact_credentials``), unprintable characters
    become U+FFFD, then keys are cut at 128 and values at 1024 characters
    with the truncation marker. Whole entries are then taken, labels first
    and annotations after, each in key order, until the next one would put
    ``canonical`` of the fact over 8192 characters; the rest are dropped and
    counted in ``omitted``. Two raw keys that process to the same key keep
    the one whose raw key sorts first; the other counts as omitted.
    Deterministic: the same maps give the same bytes.
    """
    label_entries, label_cut, label_collided = _context_entries(labels)
    note_entries, note_cut, note_collided = _context_entries(annotations)
    ordered = [("labels", entry) for entry in label_entries] + [
        ("annotations", entry) for entry in note_entries
    ]
    collided = label_collided + note_collided
    cut = label_cut or note_cut

    def fact(count: int) -> dict[str, Any]:
        chosen: dict[str, dict[str, str]] = {"labels": {}, "annotations": {}}
        for name, (key, value) in ordered[:count]:
            chosen[name][key] = value
        omitted = collided + len(ordered) - count
        return {**chosen, "truncated": cut or omitted > 0, "omitted": omitted}

    count = 0
    while count < len(ordered) and (
        len(canonical(fact(count + 1))) <= ALERT_CONTEXT_TOTAL_CHARS
    ):
        count += 1
    return fact(count)


def alert_context_of_record(alert_json: str) -> dict[str, Any] | None:
    """K6: the alert context rebuilt from a delivery's stored audit text.

    The audit record keeps the alert redacted by the same rule, which K2
    applies again without changing it, so this equals ``alert_context`` of
    the alert as received. ``None`` when the record was cut at its byte
    bound (it is then not JSON) or lacks the two objects.
    """
    try:
        alert = json.loads(alert_json)
    except ValueError:
        return None
    if not isinstance(alert, dict):
        return None
    labels = alert.get("labels")
    annotations = alert.get("annotations")
    if annotations is None:
        annotations = {}
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        return None
    if any(
        not isinstance(k, str) or not isinstance(v, str)
        for entries in (labels, annotations)
        for k, v in entries.items()
    ):
        return None
    return alert_context(labels, annotations)
