"""Harness-owned I/O witnesses, never a recovery implementation.

The recovery_runtime fixture installs these as its ONLY telemetry,
environment and model transports (no network fallback). Raw responses come
from scenario stimuli, not from expected recovery verdicts.
"""

from collections import Counter
from copy import deepcopy


class ContractInterfaceConflict(AssertionError):
    """Only a reproduced interface mismatch, never an unrelated failure."""


class RecoveryBoundaries:
    def __init__(self):
        self.telemetry_responses = {}
        self._environment_writes = []
        self.telemetry_calls_during_replay = 0
        self.model_calls = 0
        self.replaying = False
        self.telemetry_calls = []
        self.telemetry_windows = set()

    @property
    def environment_writes(self):
        return tuple(self._environment_writes)

    def environment_write(self, method, endpoint, payload=None):
        self._environment_writes.append((method, endpoint, deepcopy(payload)))
        raise AssertionError("F6 harness rejected an environment write")

    def model_request(self, *args, **kwargs):
        self.model_calls += 1
        raise AssertionError("F6 harness rejected a model call")

    def configure_telemetry(self, observations):
        self.telemetry_responses = {}
        self.telemetry_windows = set()
        for row in observations:
            self.telemetry_windows.add((row["window_start"], row["window_end"]))
            for signal in row["signals"].values():
                payload = signal["raw_payload"]
                assert isinstance(payload, bytes)
                key = (signal["query"], row["window_start"], row["window_end"])
                assert key not in self.telemetry_responses
                self.telemetry_responses[key] = payload

    def telemetry_query(
        self, query, *, window_start=None, window_end=None, query_kind="query"
    ):
        if self.replaying:
            self.telemetry_calls_during_replay += 1
            raise AssertionError("F6 harness rejected telemetry during replay")
        self.telemetry_calls.append((query, window_start, window_end, query_kind))
        key = (query, window_start, window_end)
        if key not in self.telemetry_responses:
            if (window_start, window_end) in self.telemetry_windows:
                return None  # configured missing signal, never a network fallback
            raise AssertionError("F6 telemetry stub has no configured response")
        return self.telemetry_responses[key]


class GuardedRecoveryDriver:
    def __init__(self, runtime, boundaries):
        self.runtime = runtime
        self.boundaries = boundaries
        self.incident_targets = {}

    @property
    def environment_writes(self):
        return self.boundaries.environment_writes

    @property
    def telemetry_calls_during_replay(self):
        return self.boundaries.telemetry_calls_during_replay

    @property
    def model_calls(self):
        return self.boundaries.model_calls

    def _dispatch(self, method, scenario, stimuli):
        observations = stimuli.pop("observations", ())
        self.boundaries.configure_telemetry(observations)
        before = len(self.boundaries.telemetry_calls)
        # Only schedule metadata enters the runtime. Values/raw responses are
        # accessible exclusively through the harness-owned telemetry boundary.
        schedule = [
            {key: row[key] for key in ("sequence", "window_start", "window_end")}
            for row in observations
        ]
        outcome = method(scenario, schedule=schedule, **stimuli)
        assert outcome.subject_id == scenario.subject_id
        if observations:
            profile = outcome.recovery_profile
            expected = [
                (
                    f"synthetic_{name}{{service='checkout'}}",
                    row["window_start"],
                    row["window_end"],
                    kind,
                )
                for row in observations
                for name in profile["required_signals"]
                for kind in ("query", "coverage", "freshness")
            ]
            assert Counter(self.boundaries.telemetry_calls[before:]) == Counter(
                expected
            )
        return outcome

    def run(self, scenario, **stimuli):
        return self._dispatch(self.runtime.run, scenario, stimuli)

    def continue_observation(self, scenario, **stimuli):
        """Submit to the existing session, without handling/re-authorizing it."""
        return self._dispatch(self.runtime.continue_observation, scenario, stimuli)

    def persisted_replay_input(self, subject_id, session_id):
        return self.runtime.persisted_replay_input(subject_id, session_id)

    def prepare_submission(self, subject_id):
        self.runtime.prepare_submission(subject_id)

    def fault_at_submission(self, fault):
        assert fault in {"revoke", "expire"}
        self.runtime.submission_fault = (
            self.runtime.revoke_observation
            if fault == "revoke"
            else self.runtime.expire_observation
        )

    def submission_snapshots(self):
        return deepcopy((self.runtime.submission_before, self.runtime.submission_after))

    def submitted_job_witness(self):
        return deepcopy(self.runtime.submitted_job_witness)

    def claim_available(self, subject_id):
        return self.runtime.claim_available(subject_id)

    def replay(self, **artifacts):
        assert not self.boundaries.replaying
        persisted = artifacts.get("persisted")
        subject_id = persisted["subject_id"] if persisted else None
        before = self.snapshot_incident(subject_id) if subject_id else None
        self.boundaries.replaying = True
        try:
            result = self.runtime.replay(**artifacts)
            return result
        finally:
            self.boundaries.replaying = False
            if subject_id:
                assert self.snapshot_incident(subject_id) == before

    def seed_incident(self, subject_id, **state):
        """Harness setup of OpsPilot records, never environment actuation."""
        self.incident_targets[subject_id] = deepcopy(state["target"])
        self.runtime.seed_incident(subject_id, **state)

    def snapshot_incident(self, subject_id):
        """Read lifecycle, observation sessions and samples by incident id."""
        return deepcopy(self.runtime.snapshot_incident(subject_id))

    def read_raw_payload(self, evidence_id):
        """Every evidence reference must resolve to its original bytes."""
        reader = getattr(self.runtime, "read_raw_payload", None)
        assert callable(reader), "F6 requires read_raw_payload"
        payload = reader(evidence_id)
        assert isinstance(payload, bytes), "F6 evidence must resolve to original bytes"
        return payload
