"""Independent recovery observation (C3 section 10, F6).

M1-02 step 1 (#83) only defines *what counts as recovered*: the versioned
``HealthProfile`` file, its loader and the pure per-sample verdict. The
Observer process that queries Prometheus with its own read-only credentials
and the session store that adopts samples are later steps and live elsewhere;
nothing in this package performs I/O against a data source or a database.
"""

from .health_profile import (
    PROFILE_DIRECTORY,
    PROFILE_FORMAT_VERSION,
    HealthProfile,
    HealthProfileError,
    HealthSignal,
    ProfileSubject,
    ReadingStatus,
    SampleEvaluation,
    SessionParameters,
    SignalBound,
    SignalReading,
    SignalVerdict,
    TrafficGate,
    evaluate_readings,
    load_health_profile,
    profile_revision,
)

__all__ = [
    "PROFILE_DIRECTORY",
    "PROFILE_FORMAT_VERSION",
    "HealthProfile",
    "HealthProfileError",
    "HealthSignal",
    "ProfileSubject",
    "ReadingStatus",
    "SampleEvaluation",
    "SessionParameters",
    "SignalBound",
    "SignalReading",
    "SignalVerdict",
    "TrafficGate",
    "evaluate_readings",
    "load_health_profile",
    "profile_revision",
]
