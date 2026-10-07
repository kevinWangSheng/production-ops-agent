"""Harness-owned I/O witnesses, never a recovery implementation.

The future recovery_runtime fixture must install these as its ONLY telemetry
and environment transports (no network fallback). Raw responses are configured
by that adapter from the scenario stimuli, not from expected recovery verdicts.
"""

from copy import deepcopy


class RecoveryBoundaries:
    def __init__(self):
        self.telemetry_responses = {}
        self._environment_writes = []
        self.telemetry_calls_during_replay = 0
        self.replaying = False

    @property
    def environment_writes(self):
        return tuple(self._environment_writes)

    def environment_write(self, method, endpoint, payload=None):
        self._environment_writes.append((method, endpoint, deepcopy(payload)))
        raise AssertionError("F6 harness rejected an environment write")

    def telemetry_query(self, query):
        if self.replaying:
            self.telemetry_calls_during_replay += 1
            raise AssertionError("F6 harness rejected telemetry during replay")
        if query not in self.telemetry_responses:
            raise AssertionError("F6 telemetry stub has no configured response")
        return deepcopy(self.telemetry_responses[query])


class GuardedRecoveryDriver:
    def __init__(self, runtime, boundaries):
        self.runtime = runtime
        self.boundaries = boundaries

    @property
    def environment_writes(self):
        return self.boundaries.environment_writes

    @property
    def telemetry_calls_during_replay(self):
        return self.boundaries.telemetry_calls_during_replay

    def run(self, scenario, **stimuli):
        outcome = self.runtime.run(scenario, **stimuli)
        assert outcome.subject_id == scenario.subject_id
        return outcome

    def replay(self, **artifacts):
        assert not self.boundaries.replaying
        self.boundaries.replaying = True
        try:
            return self.runtime.replay(**artifacts)
        finally:
            self.boundaries.replaying = False

    def seed_incident(self, subject_id, **state):
        """Harness setup of OpsPilot records, never environment actuation."""
        self.runtime.seed_incident(subject_id, **state)

    def snapshot_incident(self, subject_id):
        """Read lifecycle, observation sessions and samples by incident id."""
        return deepcopy(self.runtime.snapshot_incident(subject_id))

    def read_raw_payload(self, evidence_id):
        """Optional captured bytes; None means retrieval is not exposed yet."""
        reader = getattr(self.runtime, "read_raw_payload", None)
        return None if reader is None else reader(evidence_id)
