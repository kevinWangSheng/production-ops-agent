"""Report to agent_output.json conversion rules (L1-L5), synthetic data only."""

from __future__ import annotations

import json

import pytest

from scripts.itbench_lite import answer

VOCAB = ["checkout", "payment", "frontend", "flagd", "flagd-config", "cart"]


def claim(kind, text, evidence_ids=("ev-1",)):
    return {
        "kind": kind,
        "text": text,
        "evidence_ids": list(evidence_ids),
        "target_refs": ["target-1"] if evidence_ids else [],
        "time_scope_ref": "window-1" if evidence_ids else None,
    }


def report(summary, claims, conclusion="supported", status="completed", gaps=()):
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": status,
        "conclusion": conclusion,
        "summary": summary,
        "claims": claims,
        "gaps": list(gaps),
        "next_steps": [],
    }


def ev(evidence_id, tool, params, *, flat=False):
    query = json.dumps(params, sort_keys=True, separators=(",", ":"))
    if flat:
        return {"evidence_id": evidence_id, "tool": tool, "query": query}
    return {"evidence_id": evidence_id, "operation": {"tool": tool, "query": query}}


def names(output):
    return [e["name"] for e in output["entities"]]


def test_no_report_is_valid_empty_result():
    result = answer.convert(None, [], VOCAB)
    assert result.agent_output == {
        "entities": [],
        "propagations": [],
        "alerts_explained": [],
    }
    assert result.meta["method"] == "heuristic"
    assert result.meta["report_present"] is False


def test_invalid_report_is_rejected_not_silently_emptied():
    with pytest.raises(ValueError):
        answer.convert({"summary": "checkout"}, [], VOCAB)


def test_first_summary_entity_is_the_only_root_cause_and_order_follows_mentions():
    rep = report(
        "Payment errors make checkout fail; frontend shows 500s.",
        [
            claim("fact", "checkout error rate rose to 40%"),
            claim("fact", "cart latency is normal", ("ev-2",)),
        ],
    )
    out = answer.convert(
        rep, [ev("ev-1", "m", {}), ev("ev-2", "m", {})], VOCAB
    ).agent_output
    assert names(out) == [
        "otel-demo/Service/payment",
        "otel-demo/Service/checkout",
        "otel-demo/Service/frontend",
        "otel-demo/Service/cart",
    ]
    assert [e["contributing_factor"] for e in out["entities"]] == [
        True,
        False,
        False,
        False,
    ]


def test_matching_is_case_insensitive_on_name_boundaries_longest_first():
    rep = report(
        "The flagd-config change broke Checkout; checkout-7f9 pod and flagdx are unrelated.",
        [claim("fact", "x")],
    )
    out = answer.convert(rep, [ev("ev-1", "m", {})], VOCAB).agent_output
    # flagd-config, not flagd; "checkout" (case-folded) yes; checkout-7f9 and flagdx no.
    assert names(out) == [
        "otel-demo/Service/flagd-config",
        "otel-demo/Service/checkout",
    ]


def test_inconclusive_marks_no_root_cause():
    rep = report(
        "payment may be involved.",
        [claim("hypothesis", "payment pool exhaustion", ())],
        conclusion="inconclusive",
        status="incomplete",
        gaps=["no traces"],
    )
    out = answer.convert(rep, [], VOCAB).agent_output
    assert names(out) == ["otel-demo/Service/payment"]
    assert out["entities"][0]["contributing_factor"] is False


def test_rejected_hypothesis_entity_is_never_root_cause_and_is_not_promoted():
    rep = report(
        "checkout looked suspicious but payment is down.",
        [
            claim("rejected_hypothesis", "checkout itself is healthy"),
            claim("fact", "error budget burned"),
        ],
    )
    out = answer.convert(rep, [ev("ev-1", "m", {})], VOCAB).agent_output
    assert [(e["name"], e["contributing_factor"]) for e in out["entities"]] == [
        ("otel-demo/Service/checkout", False),
        ("otel-demo/Service/payment", False),
    ]


def test_kind_from_cited_tool_parameters():
    rep = report(
        "flagd-config was edited; payment pods restarted; checkout slowed.",
        [
            claim("fact", "configmap flagd-config changed", ("ev-cm",)),
            claim("fact", "payment restarted 3 times", ("ev-pod",)),
            claim("fact", "checkout p95 rose", ("ev-prom",)),
        ],
    )
    evidence = [
        ev(
            "ev-cm",
            "kubernetes_jq_query",
            {
                "kind": "configmaps",
                "namespace": "shop",
                "expr": '.items[] | select(.metadata.name=="flagd-config")',
            },
        ),
        ev(
            "ev-pod",
            "pod_logs",
            {"namespace": "otel-demo", "pod": "payment"},
            flat=True,
        ),
        ev(
            "ev-prom",
            "metrics_query",
            {"expr": 'histogram_quantile(0.95, rate(x{service_name="checkout"}[5m]))'},
        ),
    ]
    out = answer.convert(rep, evidence, VOCAB).agent_output
    assert names(out) == [
        "shop/ConfigMap/flagd-config",
        "otel-demo/Pod/payment",
        "otel-demo/Service/checkout",
    ]


def test_kind_fallback_only_for_single_entity_claims():
    rep = report(
        "flagd-config and cart were inspected.",
        [
            claim("fact", "flagd-config exists", ("ev-cm",)),
            claim("fact", "cart and flagd-config listed", ("ev-dep",)),
        ],
    )
    evidence = [
        ev("ev-cm", "kubernetes_jq_query", {"kind": "configmaps", "expr": ".items[]"}),
        ev(
            "ev-dep", "kubernetes_jq_query", {"kind": "deployments", "expr": ".items[]"}
        ),
    ]
    out = answer.convert(rep, evidence, VOCAB).agent_output
    # cart is only mentioned in a two-entity claim: no fallback, default Service.
    assert names(out) == ["otel-demo/ConfigMap/flagd-config", "otel-demo/Service/cart"]


def test_events_kind_is_not_an_entity_kind():
    rep = report(
        "checkout events show back-off.", [claim("fact", "checkout BackOff", ("ev-e",))]
    )
    evidence = [
        ev("ev-e", "kubernetes_jq_query", {"kind": "events", "expr": "checkout"})
    ]
    out = answer.convert(rep, evidence, VOCAB).agent_output
    assert names(out) == ["otel-demo/Service/checkout"]


def test_reasoning_and_evidence_join_and_truncate():
    long_text = "payment " + "x" * 2500
    rep = report(
        "payment failed.",
        [
            claim("fact", "payment 5xx", ("ev-1", "ev-missing")),
            claim("fact", long_text, ("ev-2",)),
        ],
    )
    evidence = [
        ev("ev-1", "traces_search", {"service": "payment"}),
        ev("ev-2", "m", {"a": 1}),
    ]
    result = answer.convert(rep, evidence, VOCAB)
    entity = result.agent_output["entities"][0]
    assert entity["reasoning"].startswith("payment 5xx\npayment xxx")
    assert len(entity["reasoning"]) == 2000
    assert entity["evidence"] == 'traces_search {"service":"payment"}; m {"a":1}'
    assert result.meta["unresolved_evidence_ids"] == ["ev-missing"]


def test_entity_only_in_summary_has_empty_reasoning():
    rep = report("payment failed.", [claim("fact", "error rate up")])
    out = answer.convert(rep, [ev("ev-1", "m", {})], VOCAB).agent_output
    assert out["entities"][0]["reasoning"] == ""
    assert out["entities"][0]["evidence"] == ""


def test_propagations_only_from_explicit_adjacent_arrows():
    rep = report(
        "flagd -> payment → `checkout` -> frontend. cart leads to payment; payment->payment.",
        [claim("fact", "x")],
    )
    out = answer.convert(rep, [ev("ev-1", "m", {})], VOCAB).agent_output
    assert [(p["source"], p["target"]) for p in out["propagations"]] == [
        ("otel-demo/Service/flagd", "otel-demo/Service/payment"),
        ("otel-demo/Service/payment", "otel-demo/Service/checkout"),
        ("otel-demo/Service/checkout", "otel-demo/Service/frontend"),
    ]
    assert all(
        set(p) == {"source", "target", "condition", "effect"}
        for p in out["propagations"]
    )


def test_no_arrow_means_empty_propagations():
    rep = report("payment caused checkout errors", [claim("fact", "x")])
    out = answer.convert(rep, [ev("ev-1", "m", {})], VOCAB).agent_output
    assert out["propagations"] == []
    assert out["alerts_explained"] == []


def test_vocabulary_is_an_input_not_built_in():
    rep = report("payment failed.", [claim("fact", "x")])
    out = answer.convert(rep, [ev("ev-1", "m", {})], []).agent_output
    assert out["entities"] == []


def test_write_trial_layout_and_sidecar(tmp_path):
    result = answer.convert(None, [], VOCAB)
    path = answer.write_trial(tmp_path, "Scenario-7", 2, result)
    assert path == tmp_path / "Scenario-7" / "2" / "outputs" / "agent_output.json"
    assert json.loads(path.read_text()) == answer.empty_output()
    meta = json.loads(
        (tmp_path / "Scenario-7" / "2" / "conversion_meta.json").read_text()
    )
    assert meta["method"] == "heuristic" and meta["propagation_chain"] == "受转换限制"


def test_unspaced_ascii_arrow_with_hyphenated_names():
    rep = report(
        "payment->checkout; flagd-config->cart; checkout-7f9->frontend.",
        [claim("fact", "x")],
    )
    out = answer.convert(rep, [ev("ev-1", "m", {})], VOCAB).agent_output
    assert [(p["source"], p["target"]) for p in out["propagations"]] == [
        ("otel-demo/Service/payment", "otel-demo/Service/checkout"),
        ("otel-demo/Service/flagd-config", "otel-demo/Service/cart"),
    ]
    # flagd is never matched inside flagd-config, nor checkout inside checkout-7f9.
    assert "otel-demo/Service/flagd" not in names(out)
    assert names(out).count("otel-demo/Service/checkout") == 1
