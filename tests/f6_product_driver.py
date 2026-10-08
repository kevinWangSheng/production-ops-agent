"""F6 adapter over public storage and Observer APIs; no recovery algorithm.

Only scheduling/time setup and fault injection use owner SQL. Real lease,
readings, adoption and lifecycle writes always go through the product APIs.
The sampler clock is synthetic; PostgreSQL lease checks still use its clock.
This is a bounded integration test, not a production timing proof.
"""

import base64
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

from opspilot.observation import ObservationStore
from opspilot.observer import (
    HealthProfile,
    SignalReading,
    canonical_content,
    evaluate_readings,
)
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import InstantResult
from opspilot.persistence import DurableStore, PoolConfig

POOL = PoolConfig(min_size=1, max_size=2, timeout=5)


def encode_profile(profile, handled_at):
    """Reversible fixture vocabulary mapping, preserving all numeric bounds."""
    assert profile["sample_interval_seconds"] == 60
    assert profile["max_coverage_gap_seconds"] == 0
    assert all(
        profile[key] is True
        for key in (
            "required_deployment_available",
            "required_pods_ready",
            "required_dependencies_healthy",
        )
    )
    target = profile["target"]
    thresholds = {
        "deployment": {"min": 1},
        "request_volume": {"min": 0},
        "errors": {"min": 0, "max": profile["max_error_ratio"]},
        "latency": {"min": 0, "max": profile["max_latency_ms"]},
        "pods": {"min": 1},
        "dependencies": {"min": 1},
    }
    signals = []
    for name in profile["required_signals"]:
        selector = (
            f'synthetic_{name}{{namespace="{target["namespace"]}",'
            f'service="{target["resource_uid"]}"}}'
        )
        signals.append(
            dict(
                name=name,
                description=name,
                query=selector,
                coverage_query=f"count_over_time({selector}[60s])",
                freshness_query=f"min(timestamp({selector}))",
                minimum_samples=1,
                traffic_dependent=name in {"errors", "latency"},
                healthy=thresholds[name],
                scope=dict(namespace_label="namespace", workload_label="service"),
            )
        )
    frozen = deepcopy(profile)
    frozen["deadline"] = frozen["deadline"].isoformat()
    return HealthProfile.model_validate(
        dict(
            format_version=1,
            profile_id="f6-synthetic",
            description="Independent synthetic F6 boundaries",
            subject=dict(
                service=target["resource_uid"],
                kubernetes_namespace=target["namespace"],
            ),
            source="synthetic-lab",
            # Store the inverse vocabulary alongside the frozen native profile.
            calibration_source=json.dumps(frozen, sort_keys=True),
            evaluation_window_seconds=60,
            freshness_seconds=profile["freshness_seconds"],
            query_timeout_seconds=1,
            session=dict(
                deadline_seconds=int(
                    (profile["deadline"] - handled_at).total_seconds()
                ),
                max_samples=profile["max_samples"],
                sample_interval_seconds=profile["sample_interval_seconds"],
                sustained_window_seconds=profile["healthy_window_seconds"],
            ),
            effective_traffic=dict(
                signal="request_volume", minimum=profile["minimum_requests"]
            ),
            signals=signals,
        )
    )


def decode_profile(native):
    result = json.loads(native.calibration_source)
    result["deadline"] = datetime.fromisoformat(result["deadline"])
    result["required_signals"] = tuple(result["required_signals"])
    return result


class BoundarySource:
    """InstantSource injected into Observer; only the harness supplies bytes.

    The product requests value, raw coverage and freshness independently.
    Fixture JSON is decoded here just as a Prometheus source decodes vectors.
    No structured observation enters the sampler or store.
    """

    def __init__(self, boundaries, profile):
        self.boundaries = boundaries
        self.profile = profile

    def instant(self, expr, *, at, timeout_seconds):
        signal = next(
            s
            for s in self.profile.signals
            if expr in (s.query, s.coverage_query, s.freshness_query)
        )
        kind = (
            "query"
            if expr == signal.query
            else "coverage"
            if expr == signal.coverage_query
            else "freshness"
        )
        query = f"synthetic_{signal.name}{{service='checkout'}}"
        start = at - timedelta(seconds=self.profile.evaluation_window_seconds)
        payload = self.boundaries.telemetry_query(
            query, window_start=start, window_end=at, query_kind=kind
        )
        if payload is None:
            return InstantResult(expr, "no_data", None, b"", 200)
        row = json.loads(payload)
        value = (
            float(row["value"])
            if kind == "query"
            else 1.0
            if kind == "coverage"
            else datetime.fromisoformat(row["observed_at"]).timestamp()
        )
        return InstantResult(expr, "ok", value, payload, 200)


class ProductRecoveryRuntime:
    def __init__(self, dsn, observer_dsn, boundaries, monkeypatch):
        self.dsn = dsn
        self.owner = DurableStore(dsn, pool=POOL)
        self.controller = ObservationStore(dsn, pool=POOL)
        self.observer = ObservationStore(observer_dsn, pool=POOL)
        for store in (self.owner, self.controller, self.observer):
            store.install()
        self.boundaries = boundaries
        self.monkeypatch = monkeypatch
        self.ids = {}
        self.targets = {}
        self.clock = None
        self.monkeypatch.setattr(self.observer, "current_time", lambda: self.clock)
        self.pending = {}

    def close(self):
        for store in (self.observer, self.controller, self.owner):
            store.close()

    def seed_incident(self, subject_id, *, target, lifecycle):
        assert lifecycle == "open"
        if subject_id in self.ids:
            assert target == self.targets[subject_id]
            return
        incident, run = uuid4(), uuid4()
        target_id = self.owner.register_target(target["resource_uid"])
        self.owner.accept(
            incident,
            run,
            f"f6-{incident}",
            deadline=datetime.now(UTC) + timedelta(hours=1),
            budget_limit=10,
            versions={"test": "F6"},
            target_id=target_id,
        )
        self.ids[subject_id] = incident
        self.targets[subject_id] = deepcopy(target)

    def run(self, scenario, *, profile, handled_at, schedule, until):
        native = encode_profile(profile, handled_at)
        session = uuid4()
        generation = self.owner.recovery_metadata(self.ids[scenario.subject_id])[
            "control_generation"
        ]
        self.controller.register_remediation(
            self.ids[scenario.subject_id],
            expected_generation=generation,
            actor="f6-acceptance",
            revision=profile["target"]["revision"],
            deadline_at=datetime.now(UTC)
            + timedelta(seconds=(profile["deadline"] - handled_at).total_seconds()),
            max_samples=native.session.max_samples,
            sample_interval_seconds=native.session.sample_interval_seconds,
            sustained_window_seconds=native.session.sustained_window_seconds,
            health_profile_revision=native.revision,
            health_profile=canonical_content(native),
            session_id=session,
            identity={
                **{k: v for k, v in profile["target"].items() if k != "revision"},
                "workload": native.subject.service,
            },
        )
        # Date of handling is fixture setup; never a new control action.
        with self.owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET authorized_at=%s WHERE session_id=%s",
                (handled_at, session),
            )
        return self.continue_observation(scenario, schedule=schedule, until=until)

    def _lease(self, subject_id):
        session = self.controller.incident_sessions(self.ids[subject_id])[-1]
        with self.owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET active_sample_due_at=clock_timestamp() WHERE session_id=%s AND state='authorized'",
                (session["session_id"],),
            )
        leases = self.observer.claim_due_samples(uuid4(), limit=100)
        mine = [lease for lease in leases if lease.incident_id == self.ids[subject_id]]
        assert len(mine) == 1, "authorized session must have one claimable job"
        return mine[0]

    def prepare_submission(self, subject_id):
        """Hold the real lease before the human/expiry fault is injected."""
        self.pending[subject_id] = self._lease(subject_id)

    def revoke_observation(self, subject_id):
        self.controller.revoke_sessions(self.ids[subject_id])

    def expire_observation(self, subject_id):
        session = self.controller.incident_sessions(self.ids[subject_id])[-1]
        with self.owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET deadline_at=clock_timestamp()-interval '1 second' WHERE session_id=%s",
                (session["session_id"],),
            )
        self.observer.sweep_expired_sessions()

    def continue_observation(self, scenario, *, schedule, until):
        subject_id = scenario.subject_id
        session = self.controller.incident_sessions(self.ids[subject_id])[-1]
        native = HealthProfile.model_validate_json(
            self.controller.health_profile(session["health_profile_revision"])[
                "content"
            ]
        )
        loop = ObserverLoop(
            store=self.observer, source=BoundarySource(self.boundaries, native)
        )
        for entry in schedule:
            if entry["window_end"] > until:
                break
            self.clock = entry["window_end"]
            lease = self.pending.pop(subject_id, None)
            if lease is None:
                lease = self._lease(subject_id)
            # After revoke/expiry, sampling a held lease would stop before I/O.
            # The race test submits a result that finished just before the fault.
            # Gather via the real Observer using the still-current lease, then
            # inject the fault at the public atomic submission boundary below.
            if getattr(self, "submission_fault", None):
                fault = self.submission_fault
                self.submission_fault = None
                submit = self.observer.submit_sample

                def submit_after_fault(held, sample, readings):
                    self.submitted_job_witness = dict(
                        job_id=str(held.job_id),
                        session_id=str(held.session_id),
                        sequence=held.sequence,
                    )
                    fault(subject_id)
                    self.submission_before = self.snapshot_incident(subject_id)
                    result = submit(held, sample, readings)
                    self.submission_after = self.snapshot_incident(subject_id)
                    return result

                with self.monkeypatch.context() as patch:
                    patch.setattr(self.observer, "submit_sample", submit_after_fault)
                    loop.sample(lease)
            else:
                loop.sample(lease)
        # Advance the synthetic scheduling horizon, not any judgement state.
        frozen = decode_profile(native)
        session = self.controller.session(session["session_id"])
        if until >= frozen["deadline"] and session["state"] == "authorized":
            self.expire_observation(subject_id)
        return self.outcome(subject_id)

    def claim_available(self, subject_id):
        return tuple(
            lease
            for lease in self.observer.claim_due_samples(uuid4(), limit=100)
            if lease.incident_id == self.ids[subject_id]
        )

    def _history(self, subject_id, *, fresh=False):
        if fresh:
            with_store = ObservationStore(self.dsn, pool=POOL)
            try:
                sessions = with_store.incident_sessions(self.ids[subject_id])
                return [with_store.session_history(s["session_id"]) for s in sessions]
            finally:
                with_store.close()
        return [
            self.controller.session_history(s["session_id"])
            for s in self.controller.incident_sessions(self.ids[subject_id])
        ]

    def persisted_replay_input(self, subject_id, session_id):
        histories = self._history(subject_id, fresh=True)
        history = next(
            h for h in histories if str(h["session"]["session_id"]) == session_id
        )
        return dict(subject_id=subject_id, session_id=session_id, history=history)

    def _samples(self, subject_id, histories):
        saved = []
        for history in histories:
            session = history["session"]
            native = HealthProfile.model_validate_json(
                history["health_profile"]["content"]
            )
            for sample in history["samples"]:
                signals = {}
                target = deepcopy(session["target"])
                for reading in sample["readings"]:
                    raw = bytes(reading["raw"])
                    assert sha256(raw).hexdigest() == reading["raw_sha256"]
                    bundle = json.loads(raw)
                    payload = base64.b64decode(
                        bundle["query"]["body_b64"], validate=True
                    )
                    assert sha256(payload).hexdigest() == bundle["query"]["body_sha256"]
                    if not payload:
                        continue  # explicit no_data placeholder has no fixture signal
                    stimulus = json.loads(payload)
                    target = stimulus["target"]
                    defined = native.signal(reading["signal_name"])
                    assert defined is not None
                    assert reading["query"] == defined.query
                    assert reading["source"] == native.source
                    expected_query = (
                        f"synthetic_{reading['signal_name']}{{service='checkout'}}"
                    )
                    assert stimulus["query"] == expected_query
                    assert stimulus["source"] == "prometheus:synthetic-lab"
                    freshness_body = base64.b64decode(
                        bundle["freshness"]["body_b64"], validate=True
                    )
                    if freshness_body:
                        assert (
                            json.loads(freshness_body)["observed_at"]
                            == stimulus["observed_at"]
                        )
                    if reading["status"] == "ok":
                        assert reading["value"] == float(stimulus["value"])
                    # Unknown/stale readings clear their interpreted value;
                    # the original value remains authoritative in saved bytes.
                    signals[reading["signal_name"]] = dict(
                        value=reading["value"]
                        if reading["status"] == "ok"
                        else stimulus["value"],
                        source="prometheus:" + reading["source"],
                        query=expected_query,
                        observed_at=datetime.fromisoformat(stimulus["observed_at"]),
                        evidence_id=f"{sample['sample_id']}:{reading['signal_name']}",
                        raw_sha256=bundle["query"]["body_sha256"],
                    )
                saved.append(
                    dict(
                        subject_id=subject_id,
                        sample_id=str(sample["sample_id"]),
                        session_id=str(session["session_id"]),
                        sequence=sample["sequence"],
                        target=target,
                        subject_control_generation=sample["subject_control_generation"],
                        observation_generation=sample["observation_generation"],
                        health_profile_revision=decode_profile(native)["revision"],
                        window_start=sample["window_start"],
                        window_end=sample["window_end"],
                        disposition=sample["disposition"],
                        outcome=sample["outcome"],
                        signals=signals,
                    )
                )
        return tuple(saved)

    def read_raw_payload(self, evidence_id):
        sample_id, name = evidence_id.split(":", 1)
        for subject_id in self.ids:
            for history in self._history(subject_id, fresh=True):
                for sample in history["samples"]:
                    if str(sample["sample_id"]) != sample_id:
                        continue
                    reading = next(
                        r for r in sample["readings"] if r["signal_name"] == name
                    )
                    raw = bytes(reading["raw"])
                    assert sha256(raw).hexdigest() == reading["raw_sha256"]
                    query = json.loads(raw)["query"]
                    payload = base64.b64decode(query["body_b64"], validate=True)
                    assert sha256(payload).hexdigest() == query["body_sha256"]
                    return payload
        raise AssertionError("persisted evidence reference not found")

    def snapshot_incident(self, subject_id):
        histories = self._history(subject_id)
        sessions = []
        jobs = set()
        for history in histories:
            s = history["session"]
            native = HealthProfile.model_validate_json(
                history["health_profile"]["content"]
            )
            sessions.append(
                dict(
                    session_id=str(s["session_id"]),
                    purpose=s["purpose"],
                    subject=dict(kind="incident", id=subject_id),
                    target=s["target"],
                    subject_control_generation=s["subject_control_generation"],
                    observation_generation=s["observation_generation"],
                    state=s["state"],
                    authorized=s["authorized"] and s["state"] == "authorized",
                    health_profile_revision=decode_profile(native)["revision"],
                    adopted_sequence=s["adopted_sequence"],
                    adopted_window_end=s["adopted_window_end"],
                    active_sample_job_id=s["active_sample_job_id"],
                )
            )
            jobs.update(
                (str(r["job_id"]), str(s["session_id"]), r["sequence"])
                for r in history["samples"]
            )
            if s["active_sample_job_id"]:
                jobs.add(
                    (
                        str(s["active_sample_job_id"]),
                        str(s["session_id"]),
                        s["active_sample_sequence"],
                    )
                )
        with self.owner.transaction(snapshot=True) as conn:
            incident = conn.execute(
                "SELECT * FROM opspilot_incidents WHERE incident_id=%s",
                (self.ids[subject_id],),
            ).fetchone()
            audit = tuple(
                conn.execute(
                    "SELECT action,expected_generation,resulting_generation,actor,payload FROM opspilot_controls WHERE incident_id=%s ORDER BY created_at,audit_id",
                    (self.ids[subject_id],),
                ).fetchall()
            )
        last = histories[-1]["session"] if histories else {}
        since, end = last.get("healthy_since"), last.get("adopted_window_end")
        authorization = deepcopy(sessions[-1]) if sessions else None
        if authorization:
            authorization["subject_id"] = subject_id
        return dict(
            target=deepcopy(last.get("target", self.targets[subject_id])),
            incident_lifecycle=incident["lifecycle"],
            control_state=incident,
            authority_history=deepcopy(histories),
            recovery_samples=self._samples(subject_id, histories),
            observation_sessions=tuple(sessions),
            observation_authorization=authorization,
            handling_audit=audit,
            sample_jobs=tuple(
                dict(zip(("job_id", "session_id", "sequence"), job, strict=True))
                for job in sorted(jobs)
            ),
            adopted_sequence=last.get("adopted_sequence", 0),
            adopted_window_end=end,
            healthy_window_seconds=int((end - since).total_seconds())
            if since and end
            else 0,
            used_sample_count=last.get("adopted_count", 0),
        )

    def outcome(self, subject_id):
        snapshot = self.snapshot_incident(subject_id)
        history = self._history(subject_id)[-1]
        session = history["session"]
        native = HealthProfile.model_validate_json(history["health_profile"]["content"])
        reasons = []
        latest = history["samples"][-1] if history["samples"] else None
        if latest:
            readings = []
            for row in latest["readings"]:
                bundle = json.loads(bytes(row["raw"]))
                timestamp = (
                    json.loads(base64.b64decode(bundle["freshness"]["body_b64"]))[
                        "observed_at"
                    ]
                    if bundle["freshness"]["body_b64"]
                    else None
                )
                readings.append(
                    SignalReading(
                        signal_name=row["signal_name"],
                        status=row["status"],
                        value=row["value"],
                        sample_count=row["sample_count"],
                        query=row["query"],
                        source=row["source"],
                        window_start=row["window_start"],
                        window_end=row["window_end"],
                        raw_sha256=row["raw_sha256"],
                        latest_sample_at=datetime.fromisoformat(timestamp)
                        if timestamp
                        else None,
                    )
                )
            evaluation = evaluate_readings(
                native, readings, sample_time=latest["window_end"]
            )
            for verdict in evaluation.verdicts:
                if verdict.verdict == "below_traffic_gate":
                    reasons.append("INSUFFICIENT_TRAFFIC")
                if verdict.verdict in {"missing", "no_data"}:
                    reasons.extend(
                        (
                            "REQUIRED_TELEMETRY_MISSING",
                            f"MISSING_SIGNAL:{verdict.signal_name}",
                        )
                    )
                if (
                    verdict.signal_name == "dependencies"
                    and verdict.verdict == "degraded"
                ):
                    reasons.append("DEPENDENCY_UNHEALTHY")
                if verdict.verdict == "stale":
                    reasons.append("STALE_TELEMETRY")
        ended = session["state"] != "authorized"
        confirmed = snapshot["incident_lifecycle"] == "resolved"
        verdict = (
            "healthy"
            if confirmed
            else "degraded"
            if latest and latest["outcome"] == "degraded"
            else "unknown"
        )
        handoff = ended and session["ended_reason"] in {
            "deadline_expired",
            "max_samples_exhausted",
        }
        handoff_reasons = (
            tuple(
                dict.fromkeys(
                    reasons
                    + (
                        ["CONTINUED_DEGRADATION"]
                        if verdict == "degraded"
                        else ["OBSERVATION_UNCONFIRMED"]
                    )
                )
            )
            if handoff
            else ()
        )
        with self.observer.transaction(snapshot=True) as conn:
            role = conn.execute(
                "SELECT pg_has_role(current_user,'opspilot_observer','member') AS observer,has_column_privilege(current_user,'opspilot_observation_sessions','health_profile_revision','UPDATE') AS can_authorize"
            ).fetchone()
        assert role["observer"] and not role["can_authorize"]
        actions = []
        for audit in snapshot["handling_audit"]:
            # Unknown controls cannot silently disappear from the full audit.
            assert audit["action"] == "register_remediation", audit["action"]
            actions.extend(("record_handling", "advance_incident_lifecycle"))
        for saved in history["samples"]:
            actions.extend(["read_only_query"] * len(saved["readings"]))
            actions.append("persist_observation")
        for ending in history["endings"]:
            if ending["lifecycle_before"] != ending["lifecycle_after"]:
                actions.append("advance_incident_lifecycle")
            if ending["ended_reason"] in {"deadline_expired", "max_samples_exhausted"}:
                actions.append("human_handoff")
        return SimpleNamespace(
            **{
                k: snapshot[k]
                for k in (
                    "incident_lifecycle",
                    "recovery_samples",
                    "healthy_window_seconds",
                    "used_sample_count",
                )
            },
            subject_id=subject_id,
            recovery_confirmed=confirmed,
            recovery_verdict=verdict,
            recovery_reasons=tuple(dict.fromkeys(reasons)),
            latest_sample_verdict=latest["outcome"] if latest else None,
            recovery_profile=decode_profile(native),
            recovery_handled_at=session["authorized_at"],
            observation_ended=ended,
            human_interaction="handoff" if handoff else "none",
            handoff_reasons=handoff_reasons,
            model_requests=(),
            external_queries=(),
            permissions=("read_only", "human_control"),
            actions=tuple(actions),
        )

    def replay(self, **artifacts):
        # Deliberately unavailable until #87 is merged and its public report
        # contract is reviewed. Persisted input + nonmutation guard are ready.
        raise NotImplementedError("待 #87 合并")
