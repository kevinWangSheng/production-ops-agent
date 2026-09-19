"""Jinja/SSE workbench and the two authenticated intake channels (C3 sections 6, 9).

Public surface: the app factory, the authenticator and its hashing helpers,
the workbench service with its investigator seam, and the memory/durable
implementations of the event log, evidence store, incident store and web
ledger. Nothing here calls a model or resolves a target.
"""

from .app import create_app
from .auth import AuthConfig, Authenticator, AuthError, hash_password, token_digest
from .events import (
    CursorTooOld,
    DurableEventLog,
    EventLog,
    MemoryEventLog,
    SubjectEvent,
)
from .evidence import (
    DurableEvidenceStore,
    EvidenceStore,
    MemoryEvidenceStore,
    StoredEvidence,
)
from .service import (
    CONTROL_ACTIONS,
    ControlResult,
    IntakeResult,
    Investigator,
    RunContext,
    Workbench,
    WorkbenchError,
    categorize_report,
)
from .store import (
    DurableClock,
    DurableIncidentStore,
    DurableWebLedger,
    IncidentStore,
    IncidentSummary,
    MemoryWebLedger,
    WebLedger,
)

__all__ = [
    "CONTROL_ACTIONS",
    "AuthConfig",
    "AuthError",
    "Authenticator",
    "ControlResult",
    "CursorTooOld",
    "DurableClock",
    "DurableEventLog",
    "DurableEvidenceStore",
    "DurableIncidentStore",
    "DurableWebLedger",
    "EventLog",
    "EvidenceStore",
    "IncidentStore",
    "IncidentSummary",
    "IntakeResult",
    "Investigator",
    "MemoryEventLog",
    "MemoryEvidenceStore",
    "MemoryWebLedger",
    "RunContext",
    "StoredEvidence",
    "SubjectEvent",
    "WebLedger",
    "Workbench",
    "WorkbenchError",
    "categorize_report",
    "create_app",
    "hash_password",
    "token_digest",
]
