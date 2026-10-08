"""The Observer's polling loop: sweep, claim, sample, until stopped.

Shape of ``opspilot.worker_main.WorkerLoop`` without sharing code with it:
the Observer process must not import the investigation worker, the model
client or the tool gateway (C3 section 3, decision D3). One sample at a time;
``stop`` is checked between leases, never inside a sample (the lease and the
store's atomic submission own the sample's fences).

Every lease is sampled under the profile *revision the session was
authorized with*: the content comes from ``opspilot_health_profiles`` (the
store keeps it once per revision), is validated as a ``HealthProfile`` and
must reproduce the revision; otherwise the sample is filed as ``failed``
without any query, so a session whose profile cannot be read still ends at
its budget or deadline instead of spinning.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from pydantic import ValidationError

from opspilot.observation.store import (
    OBSERVER_LEASE_SECONDS,
    ObservationStore,
    SampleLease,
    SampleReceipt,
)
from opspilot.observer.health_profile import HealthProfile
from opspilot.observer.sampler import (
    InstantSource,
    submit_without_readings,
    take_sample,
)
from opspilot.persistence.base import PersistenceError

__all__ = ["ObserverLoop"]

_log = logging.getLogger("opspilot.observer")

# Window length filed with a sample that could not be taken: there is no
# profile to say, so the span is nominal and the sample is never healthy.
_NOMINAL_WINDOW_SECONDS = 60


@dataclass
class ObserverLoop:
    store: ObservationStore
    source: InstantSource
    stop: threading.Event = field(default_factory=threading.Event)
    owner: UUID = field(default_factory=uuid4)
    poll_seconds: float = 5.0
    batch: int = 20
    # how long each claimed job is leased; the sampler keeps every sample
    # inside it (``sampler.SUBMIT_MARGIN_SECONDS``)
    lease_seconds: int = OBSERVER_LEASE_SECONDS

    def poll_once(self) -> list[tuple[UUID, SampleReceipt]]:
        """One pass: sweep deadlines, claim due jobs, sample each. Storage
        refusals and crashed samples are logged by code/type and skipped; the
        lease lapses and the next claim retries the same logical sequence."""
        try:
            for session_id in self.store.sweep_expired_sessions(limit=self.batch):
                _log.info("swept session=%s", session_id)
        except PersistenceError as exc:
            _log.warning("sweep refused code=%s", exc)
        # One lease at a time (codex round 3, P1): a lease is issued with its
        # clock running, and a sample may legitimately use most of it (hung
        # source, lease budget). Leasing a batch up front would let the later
        # leases age -- or expire -- while the earlier samples run, and their
        # submissions would be filed as lease_revoked. Claiming the next job
        # only after the previous sample is submitted keeps every sample at
        # the start of its own lease; ``batch`` bounds one pass.
        results: list[tuple[UUID, SampleReceipt]] = []
        for _ in range(self.batch):
            if self.stop.is_set():
                break
            try:
                leases = self.store.claim_due_samples(
                    self.owner, limit=1, lease_seconds=self.lease_seconds
                )
            except PersistenceError as exc:
                _log.warning("claim refused code=%s", exc)
                break
            if not leases:
                break
            lease = leases[0]
            try:
                receipt = self.sample(lease)
            except PersistenceError as exc:
                _log.warning("sample refused session=%s code=%s", lease.session_id, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - keep the observer alive
                _log.error(
                    "sample crashed session=%s error=%s",
                    lease.session_id,
                    type(exc).__name__,
                )
                continue
            results.append((lease.session_id, receipt))
        return results

    def sample(self, lease: SampleLease) -> SampleReceipt:
        profile = self.profile_for(lease)
        if profile is None:
            outcome = "no_data" if lease.health_profile_revision is None else "failed"
            receipt = submit_without_readings(
                lease,
                self.store,
                outcome=outcome,
                window_seconds=_NOMINAL_WINDOW_SECONDS,
            )
            _log.info(
                "sample session=%s sequence=%s outcome=%s (no usable profile) disposition=%s state=%s",
                lease.session_id,
                lease.sequence,
                outcome,
                receipt.disposition,
                receipt.session_state,
            )
            return receipt
        return take_sample(lease, profile, self.source, self.store).receipt

    def profile_for(self, lease: SampleLease) -> HealthProfile | None:
        """The stored profile behind the lease's revision, or ``None`` when
        the session has none or the stored content is not a profile that
        reproduces its revision (never guessed from a file on disk)."""
        revision = lease.health_profile_revision
        if revision is None:
            return None
        try:
            row = self.store.health_profile(revision)
            profile = HealthProfile.model_validate(json.loads(str(row["content"])))
        except (PersistenceError, ValueError, ValidationError) as exc:
            _log.warning(
                "profile unusable session=%s revision=%s error=%s",
                lease.session_id,
                revision,
                type(exc).__name__,
            )
            return None
        if profile.revision != revision:
            _log.warning(
                "profile revision mismatch session=%s stored=%s computed=%s",
                lease.session_id,
                revision,
                profile.revision,
            )
            return None
        return profile

    def run(self) -> int:
        polls = 0
        while not self.stop.is_set():
            self.poll_once()
            polls += 1
            self.stop.wait(self.poll_seconds)
        return polls
