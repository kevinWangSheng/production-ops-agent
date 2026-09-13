"""A sidecar can only echo timing already validated from the raw bundle."""

import pytest


@pytest.mark.parametrize("raw_state", ["missing", "null", "known"])
@pytest.mark.parametrize("field", ["operation_started_at", "collection_completed_at"])
def test_timing_sidecar_cannot_invent_or_change_capture_clocks(raw_state, field):
    import copy

    from scripts.m0_environment.initial_evidence import validate_timing_echo

    original = {"source_time_basis": "unknown"}
    if raw_state == "null":
        original[field] = None
    elif raw_state == "known":
        original[field] = "2026-09-10T01:00:00Z"
    registered = {"e1": original}
    preserved = copy.deepcopy(registered)
    sidecar = {"e1": {"view_hash": "hash", "timing": copy.deepcopy(original)}}
    validate_timing_echo(sidecar, {"e1": "hash"}, registered)
    sidecar["e1"]["timing"][field] = "2026-09-10T02:00:00Z"
    with pytest.raises(ValueError, match="INITIAL_TIMING_ECHO_MISMATCH"):
        validate_timing_echo(sidecar, {"e1": "hash"}, registered)
    assert registered == preserved


@pytest.mark.parametrize(
    "change",
    ["erase", "source_start", "source_end", "basis", "id", "hash", "non_mapping"],
)
def test_timing_sidecar_preserves_verified_record_and_identity(change):
    import copy

    from scripts.m0_environment.initial_evidence import validate_timing_echo

    timing = {
        "operation_started_at": "2026-09-10T01:00:00Z",
        "collection_completed_at": "2026-09-10T01:01:00Z",
        "source_time_basis": "unknown",
    }
    registered = {"e1": timing}
    preserved = copy.deepcopy(registered)
    sidecar = {"e1": {"view_hash": "hash", "timing": copy.deepcopy(timing)}}
    if change == "erase":
        sidecar["e1"]["timing"]["operation_started_at"] = None
    elif change in {"source_start", "source_end"}:
        sidecar["e1"]["timing"][change + "_at"] = "2026-09-10T01:00:00Z"
    elif change == "basis":
        sidecar["e1"]["timing"]["source_time_basis"] = "event_time"
    elif change == "id":
        sidecar["other"] = sidecar.pop("e1")
    elif change == "hash":
        sidecar["e1"]["view_hash"] = "other"
    else:
        sidecar["e1"] = []
    with pytest.raises(ValueError, match="INITIAL_TIMING_ECHO_MISMATCH"):
        validate_timing_echo(sidecar, {"e1": "hash"}, registered)
    assert registered == preserved


def test_timing_sidecar_accepts_equivalent_normalized_timestamp_without_mutation():
    import copy

    from scripts.m0_environment.initial_evidence import validate_timing_echo

    timing = {"operation_started_at": "2026-09-10T01:00:00Z"}
    registered = {"e1": timing}
    preserved = copy.deepcopy(registered)
    sidecar = {
        "e1": {
            "view_hash": "hash",
            "timing": {"operation_started_at": "2026-09-10T01:00:00+00:00"},
        }
    }
    assert validate_timing_echo(sidecar, {"e1": "hash"}, registered) is None
    assert registered == preserved


@pytest.mark.parametrize("extra", ["credential", "untrusted_payload"])
def test_timing_sidecar_rejects_unknown_record_fields(extra):
    from scripts.m0_environment.initial_evidence import validate_timing_echo

    timing = {
        "operation_started_at": "2026-09-10T01:00:00Z",
        "source_time_basis": "unknown",
    }
    sidecar = {
        "e1": {
            "view_hash": "hash",
            "timing": timing,
            extra: "must not persist",
        }
    }
    with pytest.raises(ValueError, match="INITIAL_TIMING_ECHO_MISMATCH"):
        validate_timing_echo(sidecar, {"e1": "hash"}, {"e1": timing})
