"""Independent contract r12 tests: compact recovery readings in the model payload.

Written from tmp/ruling-r12.md (user ruling 2026-10-15) and the public
interfaces of ``opspilot.knowledge.generation`` only. The old per-reading
shape is reconstructed here from the authoritative ``built.catalog`` so the
"would have exceeded the limit" premise does not depend on old code.
"""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest

from opspilot.knowledge import contract
from opspilot.knowledge.generation import (
    MAX_INPUT_BYTES,
    MAX_RECOVERY_READINGS,
    InputTooLarge,
    build_input,
    citation_errors,
    messages,
    parse_model_output,
)
from opspilot.knowledge.store import canonical_json
from tests.test_f13_view_compaction_contract import T0, _raw, _view

SIGNALS = (
    "dependency_deployments_available",
    "error_ratio",
    "request_rate_per_second",
    "latency_p99_seconds",
    "pod_restarts_total",
    "dependency_error_ratio",
    "saturation_cpu_ratio",
    "upstream_availability_ratio",
)
GROUP_KEYS = {
    "signal_name",
    "source",
    "evidence_ids",
    "sample_sequence",
    "status",
    "value",
    "sample_count",
}
COLUMNS = ("evidence_ids", "sample_sequence", "status", "value", "sample_count")
OLD_READING_KEYS = (
    "evidence_id",
    "sample_count",
    "sample_sequence",
    "signal_name",
    "source",
    "status",
    "value",
    "window_end",
    "window_start",
)


def _size(value):
    return len(canonical_json(value).encode("utf-8"))


def _sample_id(index):
    return UUID(f"3f2a9c4e-7b1d-4e8a-9c3f-{index:012x}")


def _investigation_view(index, items=16):
    content = [
        {"span_id": f"{index:02d}{i:04d}", "note": "upstream timeout " * 13}
        for i in range(items)
    ]
    return _view(evidence_id=f"inv-{index}", content=content)


def _raw_with_recovery(n_samples, views=(), signals=SIGNALS, source="prometheus"):
    raw = _raw(*views)
    samples, readings = [], []
    for s in range(n_samples):
        sid = _sample_id(s)
        start = T0 + timedelta(minutes=s, microseconds=123456)
        samples.append(
            {
                "sample_id": sid,
                "sequence": s + 1,
                "window_start": start,
                "window_end": start + timedelta(minutes=5),
                "outcome": "healthy",
                "disposition": "adopted" if s % 3 else "rejected",
                "reason": "adopted" if s % 3 else "not_adopted",
                "confirms_health": True,
                "health_basis": "confirmed",
                "readings_consistent": True,
                "transition": None,
                "submitted_at": start + timedelta(minutes=5, seconds=1),
            }
        )
        for name in signals:
            no_data = s % 5 == 0
            readings.append(
                {
                    "sample_id": sid,
                    "signal_name": name,
                    "status": "no_data" if no_data else "ok",
                    "value": None if no_data else round(0.0123456789 * (s + 1), 9),
                    "sample_count": None if no_data else 12,
                    "window_start": start,
                    "window_end": start + timedelta(minutes=5),
                    "source": source,
                    "raw_sha256": "b" * 64,
                }
            )
    raw["observation"]["samples"] = samples
    raw["observation"]["readings"] = readings
    raw["observation"]["session"]["max_samples"] = n_samples
    return raw


def _build(raw):
    return build_input(raw, secrets=[])


def _recovery_catalog(built):
    return [e for e in built.catalog if e["kind"] == "recovery"]


def _flatten(groups):
    """Readings in group order: (signal, source, id, seq, status, value, count)."""
    out = []
    for g in groups:
        for i in range(len(g["evidence_ids"])):
            out.append(
                (
                    g["signal_name"],
                    g["source"],
                    g["evidence_ids"][i],
                    g["sample_sequence"][i],
                    g["status"][i],
                    g["value"][i],
                    g["sample_count"][i],
                )
            )
    return out


def _old_shape_payload(built):
    """The pre-r12 payload: one dict per reading, full recovery catalog."""
    full = {e["evidence_id"]: e for e in built.catalog}
    old_readings = []
    for sig, src, eid, seq, status, value, count in _flatten(
        built.payload["recovery_readings"]
    ):
        old_readings.append(
            {
                "evidence_id": eid,
                "sample_count": count,
                "sample_sequence": seq,
                "signal_name": sig,
                "source": src,
                "status": status,
                "value": value,
                "window_end": full[eid]["window_end"],
                "window_start": full[eid]["window_start"],
            }
        )
    old = dict(built.payload)
    old["recovery_readings"] = old_readings
    old["evidence_catalog"] = list(built.catalog)
    return old


# --- size: the real 40 x 8 failure now fits ----------------------------------


def test_r12_full_40x8_observation_with_investigation_fits_under_limit():
    raw = _raw_with_recovery(40, [_investigation_view(i) for i in range(16)])
    assert len(raw["observation"]["readings"]) == 320
    built = _build(raw)
    assert built.payload_bytes < MAX_INPUT_BYTES
    # Premise: under the old per-reading shape this same input was over.
    old_bytes = _size(_old_shape_payload(built))
    assert old_bytes > MAX_INPUT_BYTES
    assert 250_000 < old_bytes < 290_000  # same order as the 265,032 B incident
    assert len(_recovery_catalog(built)) == 320
    assert len(_flatten(built.payload["recovery_readings"])) == 320


def test_r12_max_recovery_readings_with_modest_investigation_fits():
    assert MAX_RECOVERY_READINGS == 400
    raw = _raw_with_recovery(50, [_investigation_view(i) for i in range(4)])
    assert len(raw["observation"]["readings"]) == MAX_RECOVERY_READINGS
    built = _build(raw)
    assert built.payload_bytes < MAX_INPUT_BYTES
    assert len(_flatten(built.payload["recovery_readings"])) == MAX_RECOVERY_READINGS


def test_r12_over_limit_after_compaction_is_still_input_too_large():
    views = [_investigation_view(i, items=110) for i in range(16)]
    with pytest.raises(InputTooLarge) as caught:
        _build(_raw_with_recovery(40, views))
    assert caught.value.limit == "MAX_INPUT_BYTES"
    assert str(caught.value) == "MAX_INPUT_BYTES"


# --- payload["recovery_readings"] column groups -------------------------------


def test_r12_groups_have_exact_column_shape_and_equal_lengths():
    built = _build(_raw_with_recovery(6))
    groups = built.payload["recovery_readings"]
    assert isinstance(groups, list) and len(groups) == len(SIGNALS)
    for g in groups:
        assert set(g) == GROUP_KEYS
        assert isinstance(g["signal_name"], str) and isinstance(g["source"], str)
        lengths = {len(g[c]) for c in COLUMNS}
        assert lengths == {6}
        assert all(isinstance(g[c], list) for c in COLUMNS)
    text = canonical_json(groups)
    assert "window_start" not in text and "window_end" not in text


def test_r12_every_reading_is_present_at_the_same_index():
    raw = _raw_with_recovery(6)
    built = _build(raw)
    seq_of = {s["sample_id"]: s["sequence"] for s in raw["observation"]["samples"]}
    expected = sorted(
        (
            r["signal_name"],
            r["source"],
            f"{r['sample_id']}:{r['signal_name']}",
            seq_of[r["sample_id"]],
            r["status"],
            r["value"],
            r["sample_count"],
        )
        for r in raw["observation"]["readings"]
    )
    actual = _flatten(built.payload["recovery_readings"])
    assert sorted(actual, key=repr) == sorted(expected, key=repr)
    # evidence ids keep "<sample_id>:<signal_name>" and match the catalog
    assert {a[2] for a in actual} == {
        e["evidence_id"] for e in _recovery_catalog(built)
    }
    # nulls survive in place
    assert any(a[5] is None and a[6] is None for a in actual)


def test_r12_each_reading_window_is_its_samples_window():
    built = _build(_raw_with_recovery(5))
    windows = {
        s["sequence"]: (s["window_start"], s["window_end"])
        for s in built.payload["recovery"]["samples"]
    }
    full = {e["evidence_id"]: e for e in built.catalog}
    for _, _, eid, seq, *_ in _flatten(built.payload["recovery_readings"]):
        assert windows[seq] == (full[eid]["window_start"], full[eid]["window_end"])


def test_r12_group_order_is_first_appearance_and_inner_order_is_original():
    raw = _raw_with_recovery(3, signals=("zeta_ratio", "alpha_rate", "mid_count"))
    readings = raw["observation"]["readings"]
    # Same signal name from another source is its own group, appearing late.
    extra = deepcopy(readings[4])  # sample 2, alpha_rate
    extra["source"] = "otel"
    readings.insert(5, extra)
    built = _build(raw)
    groups = built.payload["recovery_readings"]
    assert [(g["signal_name"], g["source"]) for g in groups] == [
        ("zeta_ratio", "prometheus"),
        ("alpha_rate", "prometheus"),
        ("mid_count", "prometheus"),
        ("alpha_rate", "otel"),
    ]
    zeta = groups[0]
    assert zeta["sample_sequence"] == [1, 2, 3]
    assert zeta["evidence_ids"] == [f"{_sample_id(s)}:zeta_ratio" for s in range(3)]
    assert groups[3]["sample_sequence"] == [2]


# --- evidence catalog ---------------------------------------------------------


def test_r12_payload_recovery_catalog_entries_are_reduced():
    views = [_investigation_view(i) for i in range(3)]
    built = _build(_raw_with_recovery(6, views))
    payload_catalog = built.payload["evidence_catalog"]
    assert [e["evidence_id"] for e in payload_catalog] == [
        e["evidence_id"] for e in built.catalog
    ]
    full = {e["evidence_id"]: e for e in built.catalog}
    seen_true = seen_false = False
    for entry in payload_catalog:
        authoritative = full[entry["evidence_id"]]
        if authoritative["kind"] == "recovery":
            assert entry == {
                "evidence_id": entry["evidence_id"],
                "kind": "recovery",
                "citable_as_fact": authoritative["citable_as_fact"],
            }
            seen_true |= entry["citable_as_fact"] is True
            seen_false |= entry["citable_as_fact"] is False
        else:
            assert entry == authoritative
    assert seen_true and seen_false
    assert any(full[e]["kind"] != "recovery" for e in full)


def test_r12_built_catalog_keeps_full_recovery_record():
    built = _build(_raw_with_recovery(4))
    entries = _recovery_catalog(built)
    assert len(entries) == 4 * len(SIGNALS)
    for e in entries:
        assert {"evidence_id", "kind", "citable_as_fact"} <= set(e)
        assert e["scope"] == f"recovery:{e['evidence_id'].split(':', 1)[1]}"
        assert e["window_start"] and e["window_end"]
        assert e["window_start"].startswith("2026-10-11T")


# --- citation validation still reads built.catalog ----------------------------


def _fact_citing(evidence_id, impact_id):
    return parse_model_output(
        canonical_json(
            {
                "narrative_sections": [
                    {
                        "key": "impact",
                        "section": "impact_summary",
                        "body": "Checkout recovered.",
                        "evidence_ids": [impact_id],
                    }
                ],
                "conclusions": [
                    {
                        "key": "finding",
                        "section": "findings",
                        "claim": "fact",
                        "body": "Recovery signal was healthy.",
                        "evidence_ids": [evidence_id],
                    }
                ],
                "proposals": [],
                "disputes": [],
            }
        )
    )


def test_r12_citing_recovery_ids_validates_as_before():
    built = _build(_raw_with_recovery(6))
    entries = _recovery_catalog(built)
    citable = next(e["evidence_id"] for e in entries if e["citable_as_fact"])
    not_citable = next(e["evidence_id"] for e in entries if not e["citable_as_fact"])
    assert citation_errors(_fact_citing(citable, citable), built.catalog) == {}
    assert citation_errors(_fact_citing(not_citable, citable), built.catalog) == {
        "finding": ["NOT_CITABLE_AS_FACT"]
    }
    unknown = f"{_sample_id(999)}:error_ratio"
    assert (
        "UNKNOWN_EVIDENCE"
        in citation_errors(_fact_citing(unknown, citable), built.catalog)["finding"]
    )


# --- versions and prompt ------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("POSTMORTEM_PROMPT_VERSION", "f13-postmortem-prompt-v5"),
        ("POSTMORTEM_INPUT_POLICY_VERSION", "f13-postmortem-input-v4"),
        ("POSTMORTEM_OUTPUT_SCHEMA_VERSION", "f13-postmortem-output-v2"),
        ("POSTMORTEM_CONTENT_SCHEMA_VERSION", "f13-postmortem-content-v1"),
        ("CONTRACT_REVISION", "r12"),
    ],
)
def test_r12_versions(name, expected):
    assert getattr(contract, name) == expected


def test_r12_prompt_describes_column_format():
    prompt = messages(_build(_raw_with_recovery(2)), secrets=[])[0]["content"]
    assert "recovery_readings" in prompt
    assert "sample_sequence" in prompt or "sequence" in prompt


# --- determinism --------------------------------------------------------------


def test_r12_compaction_is_deterministic_and_does_not_mutate_input():
    raw = _raw_with_recovery(10, [_investigation_view(i) for i in range(2)])
    original = deepcopy(raw)
    first, second = _build(raw), _build(raw)
    assert first == second
    assert first.payload_text.encode("utf-8") == second.payload_text.encode("utf-8")
    assert first.payload_sha256 == second.payload_sha256
    assert raw == original
