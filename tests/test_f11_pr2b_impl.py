"""PR-2b implementation checks: fake HTTP, clocks, scope and source failures."""

import io
import json
import socket
import threading
import urllib.error
from datetime import timedelta
from http.client import IncompleteRead
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opspilot.domain.subjects import SubjectRef
from opspilot.observation.release_types import ReleaseSampleLease, ReleaseSampleReceipt
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import (
    RESPONSE_LIMIT_BYTES,
    PrometheusReadOnlySource,
    PrometheusSeries,
)
from opspilot.observer.release_evaluation import (
    build_release_query_plan,
    evaluate_release_sample,
)
from opspilot.observer.release_sampling import take_release_sample
from opspilot.persistence import PersistenceError
from tests.f11_release_support import EvidenceBuilder, pods_for
from tests.test_f11_release_evaluation import WINDOW_END, _context
from tests.test_m1_observer import lease as incident_lease

AT = WINDOW_END.replace(microsecond=123456)


def body(rows):
    return json.dumps(
        {"status": "success", "data": {"resultType": "vector", "result": rows}}
    ).encode()


def row(value="1", *, labels=None, stamp=None):
    return {
        "metric": labels or {},
        "value": [AT.timestamp() if stamp is None else stamp, value],
    }


class Response(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, raw=b"", status=200, error=None, response=None):
        self.raw, self.status, self.error, self.response = raw, status, error, response
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        response = self.response or Response(self.raw)
        response.status = self.status
        return response


def read(raw, **kwargs):
    opener = Opener(raw, **kwargs)
    source = PrometheusReadOnlySource(
        "http://prom.example", opener=opener, clock=lambda: AT
    )
    return source.instant_vector("up", at=AT, timeout_seconds=3), opener


def test_vector_millisecond_floor_time_auth_and_exact_raw():
    raw = body([row(labels={"pod": "one"}), row("2", labels={"pod": "two"})])
    opener = Opener(raw)
    source = PrometheusReadOnlySource(
        "http://prom.example", token="synthetic", opener=opener, clock=lambda: AT
    )
    result = source.instant_vector("up", at=AT, timeout_seconds=3)
    request, timeout = opener.calls[0]
    parsed = urlsplit(request.full_url)
    assert parsed.path == "/api/v1/query" and request.get_method() == "GET"
    assert parse_qs(parsed.query) == {
        "query": ["up"],
        "time": [f"{int(AT.timestamp())}.123"],  # r8: floored to the millisecond
        "timeout": ["3s"],
    }
    assert request.get_header("Authorization") == "Bearer synthetic" and timeout == 3
    assert result.status == "ok" and result.body == raw and result.sent_at == AT
    assert [(s.labels, s.timestamp, s.value) for s in result.series] == [
        ({"pod": "one"}, AT, 1.0),
        ({"pod": "two"}, AT, 2.0),
    ]
    assert result.body_complete and len(opener.calls) == 1


@pytest.mark.parametrize(
    "rows,status,count",
    [
        ([], "no_data", 0),
        ([row(), row()], "ok", 1),
        ([row(), row("2")], "failed", 0),
        ([row(), row(stamp=AT.timestamp() - 1)], "failed", 0),
        ([row("NaN")], "failed", 0),
        ([row("+Inf")], "failed", 0),
        ([row("-Inf")], "failed", 0),
        ([row(stamp=float("nan"))], "failed", 0),
        ([row(True)], "failed", 0),
        ([row(stamp=True)], "failed", 0),
        ([row(labels={"p": 1})], "failed", 0),
        ([{"metric": {}, "value": [1]}], "failed", 0),
        ([{"metric": {}, "value": "12"}], "failed", 0),
        ([{"metric": {"p": "\x00"}, "value": [1, "1"]}], "failed", 0),
    ],
)
def test_vector_shapes(rows, status, count):
    result, _ = read(body(rows))
    assert result.status == status and len(result.series) == count


@pytest.mark.parametrize(
    "raw",
    [
        b"broken",
        b"[]",
        b"null",
        b"{}",
        b'{"status":"success","data":null}',
        b'{"status":"success","data":{"resultType":"scalar","result":[1,"2"]}}',
        b'{"status":"success","data":{"resultType":"vector","result":{}}}',
    ],
)
def test_invalid_payload_is_failed(raw):
    result, _ = read(raw)
    assert result.status == "failed" and result.series == ()


def test_status_error_200_is_failed_without_provider_detail():
    result, _ = read(b'{"status":"error","error":"private"}')
    assert result.status == "failed" and result.detail == "STATUS_NOT_SUCCESS"


@pytest.mark.parametrize("status", [201, 302, 401, 500])
def test_non_200_is_http_failure_and_never_retried(status):
    result, opener = read(body([row()]), status=status)
    assert (
        result.status == "failed"
        and result.detail == "HTTP"
        and result.http_status == status
    )
    assert len(opener.calls) == 1


def test_http_error_and_timeout_and_unreachable():
    for error, status, detail in [
        (
            urllib.error.HTTPError(
                "http://prom.example", 401, "secret", {}, io.BytesIO(b"error")
            ),
            "failed",
            "HTTP",
        ),
        (socket.timeout(), "timeout", "TIMEOUT"),
        (urllib.error.URLError(socket.timeout()), "timeout", "TIMEOUT"),
        (OSError("secret"), "failed", "UNREACHABLE"),
    ]:
        result, opener = read(b"", error=error)
        assert (result.status, result.detail) == (status, detail)
        assert len(opener.calls) == 1


@pytest.mark.parametrize(
    "error,status",
    [(socket.timeout(), "timeout"), (IncompleteRead(b"tail", 1), "failed")],
)
def test_cut_body_keeps_prefix_and_is_never_parsed(error, status):
    class Cut(Response):
        def read(self, size):
            if self.tell() == 0:
                return super().read(size)
            raise error

        read1 = read

    raw = body([row()])
    result, _ = read(b"", response=Cut(raw))
    assert result.status == status and not result.body_complete
    assert result.series == () and result.body.startswith(raw)


def test_size_limit_does_not_parse_complete_json_prefix():
    result, _ = read(body([row()]) + b" " * RESPONSE_LIMIT_BYTES)
    assert result.status == "failed" and result.detail == "TOO_LARGE"
    assert not result.body_complete and result.series == ()


def test_legacy_nan_stays_no_data_and_millisecond_encoding_stays():
    opener = Opener(body([row("NaN")]))
    source = PrometheusReadOnlySource("http://prom.example", opener=opener)
    result = source.instant("up", at=AT, timeout_seconds=3)
    assert result.status == "no_data"
    assert parse_qs(urlsplit(opener.calls[0][0].full_url).query)["time"] == [
        f"{AT.timestamp():.3f}"
    ]


def test_new_dtos_are_strict_frozen_and_reject_nonfinite():
    with pytest.raises(ValidationError):
        PrometheusSeries(labels={}, timestamp=AT, value=float("nan"))
    with pytest.raises(ValidationError):
        PrometheusSeries(labels={}, timestamp=AT, value=1.0, extra="unexpected")
    value = PrometheusSeries(labels={}, timestamp=AT, value=1.0)
    with pytest.raises(ValidationError):
        value.value = 2.0


def fixture():
    context = _context()
    pods = pods_for(context.release.after_revision.images)
    plan = build_release_query_plan(
        context, window_end=AT, instance_uids=[p.uid for p in pods]
    )
    builder = EvidenceBuilder(window_end=AT, texts=plan)
    builder.sufficient(pods)
    lease = ReleaseSampleLease(
        session_id=context.session_id,
        observation_id=context.release.observation_id,
        purpose="release_observation",
        subject=SubjectRef(
            kind="release_observation", id=str(context.release.observation_id)
        ),
        job_id=uuid4(),
        sequence=1,
        owner=uuid4(),
        epoch=1,
        lease_until=AT + timedelta(seconds=120),
        subject_control_generation=0,
        observation_generation=1,
        health_profile_revision=context.health_profile.revision,
        calibrated_policy_revision=context.policy.revision,
        deadline_at=context.release.deadline_at,
        sample_interval_seconds=60,
        adopted_window_end=None,
        global_suspension_generation=0,
        target_suspension_generation=0,
    )
    return context, builder, lease


class Source:
    def __init__(self, builder, on_call=None):
        self.answers = {r.query: r for r in builder.results.values()}
        self.calls = []
        self.on_call = on_call

    def instant(self, *args, **kwargs):
        pytest.fail("release must use instant_vector only")

    def instant_vector(self, expr, *, at, timeout_seconds):
        self.calls.append((expr, at, timeout_seconds))
        if self.on_call:
            self.on_call(len(self.calls))
        answer = self.answers[expr]
        # Use the actual parser over a fake HTTP response, preserving raw bytes.
        result, _ = read(answer.raw)
        return result.model_copy(update={"expr": expr})


class Store:
    def __init__(self, context, check=None):
        self.context, self.check = context, check
        self.submissions = []
        self.checks = 0

    def release_context(self, session_id):
        assert session_id == self.context.session_id
        return self.context

    def current_time(self):
        return AT

    def lease_scope_current(self, lease):
        self.checks += 1
        return self.check(self.checks) if self.check else True

    def submit_release_sample(self, lease, evidence):
        self.submissions.append((lease, evidence))
        return ReleaseSampleReceipt(
            sample_id=evidence.sample_id,
            session_id=lease.session_id,
            observation_id=lease.observation_id,
            replayed=False,
            accepted=True,
            disposition="adopted",
            reason="adopted",
            evaluation=evaluate_release_sample(self.context, evidence, decision_at=AT),
            session_state="authorized",
            release_status="observing",
            used_sample_count=1,
            tracking_seconds=300.0,
            healthy_window_seconds=0.0,
            ending_id=None,
            next_sample_due_at=AT + timedelta(seconds=60),
        )


def test_mapping_context_then_after_raw_evidence_and_no_client_verdict():
    context, builder, lease = fixture()
    source, store = Source(builder), Store(context)
    taken = take_release_sample(lease, source, store)
    assert (
        taken.requests_issued == 49
        and not taken.scope_interrupted
        and not taken.budget_exhausted
    )
    assert taken.receipt.evaluation.verdict == "healthy"
    evidence = store.submissions[0][1]
    keys = [r.query_key for r in evidence.queries]
    assert all(k.startswith(("mapping:", "context:")) for k in keys[:33])
    assert all(k.startswith("after:") for k in keys[33:])
    assert all(at == AT for _, at, _ in source.calls)
    assert store.checks == 49 and len(store.submissions) == 1
    assert "verdict" not in type(evidence).model_fields
    assert all(q.raw == builder.results[q.query_key].raw for q in evidence.queries)


@pytest.mark.parametrize("failure", ["mapping", "context"])
def test_after_depends_on_mapping_not_context_verdict(failure):
    context, builder, lease = fixture()
    key = (
        "mapping:pod_owner:query"
        if failure == "mapping"
        else "context:error_ratio:query"
    )
    builder.results[key] = builder.series(key, [])
    source, store = Source(builder), Store(context)
    taken = take_release_sample(lease, source, store)
    assert taken.requests_issued == (33 if failure == "mapping" else 49)
    assert taken.receipt.evaluation.verdict == "unknown"


@pytest.mark.parametrize("allowed", [0, 5, 33, 35])
def test_scope_interruption_preserves_exact_collected_evidence(allowed):
    context, builder, lease = fixture()
    source, store = Source(builder), Store(context, lambda n: n <= allowed)
    taken = take_release_sample(lease, source, store)
    assert (
        taken.requests_issued == allowed
        and taken.scope_interrupted
        and not taken.budget_exhausted
    )
    assert (
        len(store.submissions[0][1].queries) == allowed and store.checks == allowed + 1
    )


@pytest.mark.parametrize("at_request", [0, 9, 33, 35])
def test_budget_exhaustion_fills_planned_timeouts(monkeypatch, at_request):
    context, builder, lease = fixture()
    clock = [0.0]
    monkeypatch.setattr(
        "opspilot.observer.release_sampling.time.monotonic", lambda: clock[0]
    )

    def used(n):
        if n == at_request:
            clock[0] = 114.5

    if at_request == 0:
        lease = lease.model_copy(update={"lease_until": AT + timedelta(seconds=5.5)})
    source, store = Source(builder, used), Store(context)
    taken = take_release_sample(lease, source, store)
    assert (
        taken.requests_issued == at_request
        and taken.budget_exhausted
        and not taken.scope_interrupted
    )
    evidence = store.submissions[0][1]
    expected = 49 if at_request >= 9 else 33
    assert len(evidence.queries) == expected
    for q in evidence.queries[at_request:]:
        assert q.status == "timeout" and q.detail == "LEASE_BUDGET"
        assert q.sent_at is None and not q.body_complete and q.raw == b""
    assert store.checks == at_request


def test_scope_roundtrip_consumes_budget_and_timeout_is_whole_seconds(monkeypatch):
    context, builder, lease = fixture()
    lease = lease.model_copy(update={"lease_until": AT + timedelta(seconds=8.9)})
    clock = [0.0]
    monkeypatch.setattr(
        "opspilot.observer.release_sampling.time.monotonic", lambda: clock[0]
    )

    def checked(n):
        clock[0] += 1.1
        return True

    source, store = Source(builder), Store(context, checked)
    taken = take_release_sample(lease, source, store)
    assert [timeout for _, _, timeout in source.calls] == [2, 1]
    assert taken.requests_issued == 2 and taken.budget_exhausted
    assert store.checks == 3


@pytest.mark.parametrize("stage", ["context", "plan", "source", "scope", "submit"])
def test_failures_propagate_without_fabricated_or_retried_sampling(monkeypatch, stage):
    context, builder, lease = fixture()
    source, store = Source(builder), Store(context)

    def fail(*args, **kwargs):
        raise RuntimeError("private provider text")

    if stage == "plan":
        monkeypatch.setattr(
            "opspilot.observer.release_sampling.build_release_query_plan", fail
        )
    else:
        target, method = {
            "context": (store, "release_context"),
            "source": (source, "instant_vector"),
            "scope": (store, "lease_scope_current"),
            "submit": (store, "submit_release_sample"),
        }[stage]
        monkeypatch.setattr(target, method, fail)
    with pytest.raises(RuntimeError):
        take_release_sample(lease, source, store)
    assert store.submissions == []
    if stage in ("context", "plan"):
        assert source.calls == []


@pytest.mark.parametrize("refused", [None, "incident", "release"])
def test_tick_sweeps_in_order_then_dispatches_shared_claims(
    monkeypatch, caplog, refused
):
    _, _, release = fixture()
    incident = incident_lease()
    calls = []
    pending = [incident, release]

    def sweep(kind):
        calls.append(kind)
        if refused == kind:
            raise PersistenceError("TIMEOUT")
        return []

    def claim(owner, *, limit, lease_seconds):
        calls.append("claim")
        assert limit == 1
        return [pending.pop(0)] if pending else []

    store = SimpleNamespace(
        sweep_expired_sessions=lambda **k: sweep("incident"),
        sweep_expired_releases=lambda **k: sweep("release"),
        claim_due_samples=claim,
    )
    loop = ObserverLoop(store=store, source=object(), batch=3)
    loop.profile_for = lambda lease: (object(), None, None)

    def incident_sample(*args):
        calls.append("incident_sample")
        return SimpleNamespace(receipt="incident_receipt")

    def release_sample(*args):
        calls.append("release_sample")
        return SimpleNamespace(receipt="release_receipt")

    monkeypatch.setattr("opspilot.observer.loop.take_sample", incident_sample)
    monkeypatch.setattr("opspilot.observer.loop.take_release_sample", release_sample)
    assert loop.poll_once() == [
        (incident.session_id, "incident_receipt"),
        (release.session_id, "release_receipt"),
    ]
    assert calls == [
        "incident",
        "release",
        "claim",
        "incident_sample",
        "claim",
        "release_sample",
        "claim",
    ]


def test_tick_context_failure_safe_log_no_zero_submission(caplog):
    context, builder, lease = fixture()

    class Broken(Store):
        def release_context(self, sid):
            raise ValueError("PRIVATE_CONTEXT")

        def sweep_expired_sessions(self, **kwargs):
            return []

        def sweep_expired_releases(self, **kwargs):
            return []

        def claim_due_samples(self, *args, **kwargs):
            return [lease]

    store = Broken(context)
    loop = ObserverLoop(
        store=store, source=Source(builder), batch=1, stop=threading.Event()
    )
    assert loop.poll_once() == [] and store.submissions == []
    assert "PRIVATE_CONTEXT" not in caplog.text and "ValueError" in caplog.text


def test_deeply_nested_malformed_vector_returns_failure():
    result, _ = read(b"[" * 1500 + b"0" + b"]" * 1500)
    assert result.status == "failed" and result.series == ()
