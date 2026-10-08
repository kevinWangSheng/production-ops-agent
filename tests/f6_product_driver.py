"""F6 adapter over public storage and Observer APIs; no recovery algorithm.

Only scheduling/time setup and fault injection use owner SQL. Real lease,
readings, adoption and lifecycle writes always go through the product APIs.
The sampler clock is synthetic; PostgreSQL lease checks still use its clock.
This is a bounded integration test, not a production timing proof.
"""

import base64
import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from opspilot.acceptance import IncidentScenario, RecoveryRecords, recovery_outcome
from opspilot.observation import ObservationStore
from opspilot.observer import (
    HealthProfile,
    canonical_content,
)
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import PrometheusReadOnlySource
from opspilot.observer.replay import replay_history
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

    def open(self, request, timeout=None):
        if request.get_method() != "GET":
            self.boundaries.environment_write(request.get_method(), request.full_url)
        params = parse_qs(urlsplit(request.full_url).query)
        expr = params["query"][0]
        at = datetime.fromtimestamp(float(params["time"][0]), UTC)
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
        payload = self.boundaries.telemetry_query(
            f"synthetic_{signal.name}{{service='checkout'}}",
            window_start=at - timedelta(seconds=self.profile.evaluation_window_seconds),
            window_end=at,
            query_kind=kind,
        )
        if payload is None:
            payload = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
        response = BytesIO(payload)
        response.status = 200
        return response

    def instant(self, expr, *, at, timeout_seconds):
        # Product decoding both online and on replay; this opener is the
        # sole transport, with no live endpoint or fallback.
        return PrometheusReadOnlySource("http://f6.invalid", opener=self).instant(
            expr,
            at=at,
            timeout_seconds=timeout_seconds,
        )


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
        self.scenarios = {}

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
        self.scenarios[scenario.subject_id] = scenario
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
        native = HealthProfile.model_validate_json(
            self.controller.health_profile(session["health_profile_revision"])[
                "content"
            ]
        )
        # Simulate passage of the ORIGINAL duration as a pair of scheduling
        # timestamps, never a verdict/watermark write. Replay rejects a
        # deadline moved before creation; keep the frozen positive span.
        with self.owner.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET deadline_at=clock_timestamp()-interval '1 second',created_at=clock_timestamp()-%s WHERE session_id=%s",
                (
                    timedelta(seconds=native.session.deadline_seconds + 1),
                    session["session_id"],
                ),
            )
        self.observer.sweep_expired_sessions()

    def register_again(self, scenario, *, profile, handled_at, identity):
        native = encode_profile(profile, handled_at)
        return self.controller.register_remediation(
            self.ids[scenario.subject_id],
            expected_generation=self.owner.recovery_metadata(
                self.ids[scenario.subject_id]
            )["control_generation"],
            actor="f6-acceptance",
            revision=profile["target"]["revision"],
            deadline_at=datetime.now(UTC)
            + timedelta(seconds=native.session.deadline_seconds),
            max_samples=native.session.max_samples,
            sample_interval_seconds=native.session.sample_interval_seconds,
            sustained_window_seconds=native.session.sustained_window_seconds,
            health_profile_revision=native.revision,
            health_profile=canonical_content(native),
            identity={**identity, "workload": native.subject.service},
        )

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
        # New storage driver, one public consistent snapshot. No original
        # outcome object supplies any profile, sample, control or ending.
        with_store = ObservationStore(self.dsn, pool=POOL)
        try:
            records = with_store.incident_records(self.ids[subject_id])
        finally:
            with_store.close()
        history = next(
            h
            for h in records["sessions"]
            if str(h["session"]["session_id"]) == session_id
        )
        return dict(
            subject_id=subject_id,
            session_id=session_id,
            history=history,
            records=records,
        )

    @staticmethod
    def _payload(reading):
        raw = bytes(reading["raw"])
        assert sha256(raw).hexdigest() == reading["raw_sha256"]
        query = json.loads(raw)["query"]
        payload = base64.b64decode(query["body_b64"], validate=True)
        assert sha256(payload).hexdigest() == query["body_sha256"]
        return payload

    def _mapped_samples(
        self, subject_id, projected, native, *, profiles=None, reader=None
    ):
        saved = []
        for sample in projected.recovery_samples:
            sample_profile = (profiles or {}).get(
                sample.health_profile_revision, native
            )
            assert sample_profile.revision == sample.health_profile_revision
            frozen = decode_profile(sample_profile)
            row = asdict(sample)
            assert sample.subject_id == str(self.ids[subject_id])
            row["subject_id"] = subject_id
            row["health_profile_revision"] = frozen["revision"]
            # Target is the PRODUCT session binding, never telemetry metadata.
            row["target"] = dict(sample.target)
            row["signals"] = {}
            for name, signal in sample.signals.items():
                payload = (reader or self.read_raw_payload)(signal.evidence_id)
                body = json.loads(payload)
                points = body["data"]["result"]
                if not points:
                    assert signal.status == "no_data"
                    continue  # absent telemetry placeholder -> sparse harness signals
                defined = sample_profile.signal(name)
                assert defined is not None
                assert (
                    signal.query == defined.query
                    and signal.source == sample_profile.source
                )
                value = float(points[0]["value"][1])
                if signal.status == "ok":
                    assert signal.value == value
                row["signals"][name] = dict(
                    value=signal.value if signal.value is not None else value,
                    source="prometheus:" + signal.source,
                    query=f"synthetic_{name}{{service='checkout'}}",
                    observed_at=signal.observed_at,
                    evidence_id=signal.evidence_id,
                    raw_sha256=signal.body_sha256,
                )
            saved.append(row)
        return tuple(saved)

    def _normalize(self, subject_id, projected, *, records=None, reader=None):
        result = asdict(projected)
        assert projected.subject_id == str(self.ids[subject_id])
        native = (
            HealthProfile.model_validate_json(projected.recovery_profile_content)
            if projected.recovery_profile_content
            else None
        )
        profiles = {
            history["session"][
                "health_profile_revision"
            ]: HealthProfile.model_validate_json(history["health_profile"]["content"])
            for history in (records or {}).get("sessions", ())
            if history["health_profile"]
        }
        result["subject_id"] = subject_id
        result["recovery_profile"] = decode_profile(native) if native else None
        result["recovery_samples"] = (
            self._mapped_samples(
                subject_id, projected, native, profiles=profiles, reader=reader
            )
            if native
            else ()
        )
        for view in result["observation_sessions"]:
            view["subject"]["id"] = subject_id
            view["health_profile_revision"] = decode_profile(
                profiles.get(view["health_profile_revision"], native)
            )["revision"]
        if result["observation_authorization"]:
            auth = result["observation_authorization"]
            auth["subject"]["id"] = subject_id
            auth["subject_id"] = subject_id
            auth["health_profile_revision"] = decode_profile(
                profiles.get(auth["health_profile_revision"], native)
            )["revision"]

        def reason_alias(codes):
            return tuple(
                dict.fromkeys(
                    "DEPENDENCY_UNHEALTHY"
                    if code == "DEGRADED_SIGNAL:dependencies"
                    and native is not None
                    and native.signal("dependencies") is not None
                    and native.signal("dependencies").required
                    else code
                    for code in codes
                )
            )

        result["recovery_reasons"] = reason_alias(projected.recovery_reasons)
        result["handoff_reasons"] = reason_alias(projected.handoff_reasons)
        result["external_queries"] = (
            projected.replay.external_queries if projected.replay else ()
        )
        # permissions/actions/verdict/health/handoff are copied unchanged.
        return SimpleNamespace(**result)

    def _product_projection(self, subject_id, records=None):
        records = records or self.controller.incident_records(self.ids[subject_id])
        requested = self.scenarios.get(
            subject_id,
            IncidentScenario("F6:snapshot", "F6", "1", "snapshot", subject_id),
        )
        product_scenario = replace(requested, subject_id=str(self.ids[subject_id]))
        return recovery_outcome(
            product_scenario,
            RecoveryRecords(
                incident=records["incident"],
                sessions=tuple(records["sessions"]),
                controls=tuple(records["controls"]),
                grants=self.observer.table_privileges(),
            ),
        )

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
                    return self._payload(reading)
        raise AssertionError("persisted evidence reference not found")

    def snapshot_incident(self, subject_id):
        records = self.controller.incident_records(self.ids[subject_id])
        outcome = self._normalize(
            subject_id, self._product_projection(subject_id, records), records=records
        )
        last = records["sessions"][-1]["session"] if records["sessions"] else {}
        return dict(
            target=dict(outcome.target or self.targets[subject_id]),
            incident_lifecycle=outcome.incident_lifecycle,
            control_state=deepcopy(records["incident"]),
            authority_history=deepcopy(records),
            recovery_samples=outcome.recovery_samples,
            observation_sessions=outcome.observation_sessions,
            observation_authorization=outcome.observation_authorization,
            handling_audit=outcome.handling_audit,
            sample_jobs=outcome.sample_jobs,
            adopted_sequence=last.get("adopted_sequence", 0),
            adopted_window_end=last.get("adopted_window_end"),
            healthy_window_seconds=outcome.healthy_window_seconds,
            used_sample_count=outcome.used_sample_count,
        )

    def outcome(self, subject_id):
        records = self.controller.incident_records(self.ids[subject_id])
        return self._normalize(
            subject_id, self._product_projection(subject_id, records), records=records
        )

    def replay(self, *, persisted, allow_telemetry=False, allow_model=False):
        if allow_telemetry or allow_model:
            raise ValueError("REPLAY_IS_OFFLINE")
        subject_id = persisted["subject_id"]
        history = deepcopy(persisted["history"])
        # Explicit offline product API; no lookup of original outcome.
        replayed = replay_history(history)
        records = deepcopy(persisted["records"])
        records["sessions"] = [history]
        result = self._product_projection(subject_id, records)
        assert result.replay == replayed
        evidence = {
            f"{sample['sample_id']}:{reading['signal_name']}": reading
            for sample in history["samples"]
            for reading in sample["readings"]
        }
        return self._normalize(
            subject_id,
            result,
            records=records,
            reader=lambda ref: self._payload(evidence[ref]),
        )
