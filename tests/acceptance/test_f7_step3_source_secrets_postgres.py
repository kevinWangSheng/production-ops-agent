"""F7 step 3 (S3c, S3d, contract section 3 class 6) on the product path.

``feature_list.json`` F7 step 3: "Inspect model inputs, evidence exports and
traces and verify execution credentials and access secrets are absent".
U7-a with the r1 supplement (``docs/tasks/2026-10-15-f7-contract.md``): the
secret is redacted before persistence, so the assertions target what is
stored and exported, not only the view.

One worker Run under the ``otel-demo`` profile reads a Jaeger body whose span
log carries synthetic secrets (including two straddling the 600-byte detail
cut) and a Prometheus body whose label values carry synthetic secrets. Zero
hits are then required in: the model input the provider double received,
the tool views, ``opspilot_evidence.raw``/``view``, the evidence store read,
the persisted Run rows, the web evidence page (JSON and HTML), the exported
trace spans, and a ``scripts/lab_evidence.freeze`` summary (and its ledger)
built from the Run. Class 6: a Run recorded under the pre-bump tool schema
revision is refused by the new worker with ``INCOMPATIBLE_STATE``.

Run with ``M1_DURABLE_POSTGRES=1`` and ``OPSPILOT_LAB_DSN``; a scratch
database is created and dropped.
"""

from __future__ import annotations

import json
import os
from hashlib import sha256

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from opspilot import tracing
from opspilot.redaction import REDACTED_CREDENTIAL
from opspilot.tools.otel_demo import TARGET_ID
from scripts import lab_evidence
from tests.acceptance.f7_hostile_support import (
    BOUNDARY_BEARER,
    BOUNDARY_PASSWORD,
    LABEL_SECRETS,
    LOG_MESSAGE,
    LOG_SECRETS,
    LOG_STACKTRACE,
    SECRET_LABELS,
    jaeger_payload,
    leaks,
    prometheus_payload,
    text_of,
)
from tests.acceptance.f7_pg_support import (
    MODEL_KEY,
    ScriptedProvider,
    assistant,
    build_harness,
    evidence_rows,
    final_report,
    fn,
    scratch_database,
    tool_views,
)
from tests.acceptance.test_f7_step3_source_secrets import (
    OLD_OTEL_TOOL_SCHEMA_REVISION,
)
from tests.test_m1_otel_demo_contract import (
    GOOD_EXPR,
    JAEGER_URL,
    PROMETHEUS_URL,
    FakeOpener,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

PRODUCT_CONSTRAINT_SCENARIOS = ("F7:secret-scan", "F7:credential-boundary")

QUESTION = "Why is checkout erroring?"
ALL_SECRETS = (*LOG_SECRETS, *LABEL_SECRETS, BOUNDARY_PASSWORD, BOUNDARY_BEARER)


@pytest.fixture(scope="module")
def dsn():
    yield from scratch_database()


def _cut(prefix: str, secret: str) -> str:
    """A detail value whose 600-byte cut falls inside ``secret``'s tail."""
    tail = len(secret) - len("f7-synthetic-")
    pad = 600 - len(prefix) - len("f7-synthetic-") - tail // 2
    return "x" * pad + prefix + secret + " while charging"


def _opener() -> FakeOpener:
    return FakeOpener(
        by_query={GOOD_EXPR: prometheus_payload(SECRET_LABELS)},
        routes={
            "/api/traces": jaeger_payload(
                {"key": "exception.message", "value": LOG_MESSAGE},
                {"key": "exception.stacktrace", "value": LOG_STACKTRACE},
                {
                    "key": "error.description",
                    "value": _cut(" password=", BOUNDARY_PASSWORD),
                },
                {
                    "key": "grpc.error_message",
                    "value": _cut(" Authorization: Bearer ", BOUNDARY_BEARER),
                },
            )
        },
    )


def _lab_env() -> dict[str, str]:
    return {
        "OPSPILOT_TRACE": "lab",
        "LANGSMITH_API_KEY": "fake-langsmith-key-not-real-f7f7",
        "LANGSMITH_PROJECT": "opspilot-lab-f7-step3",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "OPSPILOT_TOOL_PROFILE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": PROMETHEUS_URL,
        "OPSPILOT_OTEL_JAEGER_URL": JAEGER_URL,
    }


def _exported_text(exporter: InMemorySpanExporter) -> str:
    tracing.shutdown()  # flush the batch processor
    parts = []
    for span in exporter.get_finished_spans():
        parts.append(span.name)
        parts.append(json.dumps(dict(span.attributes), default=str))
        for event in span.events:
            parts.append(json.dumps(dict(event.attributes), default=str))
    return "\n".join(parts)


def test_s3c_s3d_source_secrets_are_absent_from_every_input_store_and_export(
    dsn, monkeypatch, tmp_path
):
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(_lab_env(), exporter=exporter)
    assert isinstance(tracer, tracing.Tracer), "lab tracer did not start"
    opener = _opener()
    harness = build_harness(dsn, monkeypatch, opener)
    try:
        incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
        provider = ScriptedProvider(
            [
                assistant(
                    tool_calls=[
                        fn("read-traces", "traces_search", {"service": "checkout"}),
                        fn("read-metrics", "metrics_range_query", {"expr": GOOD_EXPR}),
                    ]
                ),
                final_report(TARGET_ID),
            ]
        )

        outcome = harness.run(incident, provider)

        assert len(opener.requests) == 2, outcome
        assert len(provider.requests) == 2, outcome
        sinks: dict[str, str] = {}

        # Model input: every request body the provider received, verbatim.
        sinks["model input"] = text_of(b"\n".join(provider.raw_requests))
        views = tool_views(provider.requests[-1]["messages"])
        assert {v["tool_call_id"] for v in views} == {"read-traces", "read-metrics"}
        for item in views:
            assert item["view"]["status"] == "ok", item
            assert item["view"]["trust"] == "untrusted-evidence"
            # The redaction is visible rather than the detail vanishing.
            assert REDACTED_CREDENTIAL in text_of(item["view"]), item["tool_call_id"]
        sinks["tool views"] = text_of(views)

        # PostgreSQL ``opspilot_evidence`` raw and view.
        rows = evidence_rows(harness, run)
        assert len(rows) == 2, [r["evidence_id"] for r in rows]
        for row in rows:
            assert row["raw_sha256"] == sha256(row["raw"]).hexdigest()
            assert REDACTED_CREDENTIAL in text_of(row["raw"]), row["evidence_id"]
            assert row["projection_revision"] == "m1-01-tool-view-v8"
        sinks["pg raw"] = text_of([r["raw"] for r in rows])
        sinks["pg view"] = "\n".join(r["view"] for r in rows)

        # The evidence store read and the persisted Run rows (steps carry the
        # committed tool results).
        stored = [harness.evidence.get(r["evidence_id"]) for r in rows]
        assert all(s is not None for s in stored)
        sinks["store raw"] = text_of([s.raw for s in stored])
        sinks["store view"] = text_of([dict(s.view) for s in stored])
        snapshot = harness.store.rebuild(incident)
        sinks["run rows"] = text_of(snapshot)

        # Web evidence page, JSON and HTML.
        web = []
        for row in rows:
            path = f"/incidents/{incident}/evidence/{row['evidence_id']}"
            as_json = harness.get(path)
            assert as_json.status == 200, as_json.text
            payload = as_json.json()
            assert payload["raw_sha256"] == row["raw_sha256"]
            web.append(as_json.text)
            as_html = harness.get(path, html=True)
            assert as_html.status == 200, as_html.text
            web.append(as_html.text)
        sinks["web evidence"] = "\n".join(web)

        # Exported trace spans (the runner opens the root span in lab mode).
        trace = _exported_text(exporter)
        assert "opspilot.run" in trace and "deepseek.chat.completions" in trace
        sinks["trace export"] = trace

        # ``summary.json`` (and the out-of-repo ledger) frozen from this Run.
        monkeypatch.setenv(lab_evidence.LEDGER_DIR_ENV, str(tmp_path / "ledger"))
        evidence_summary = [
            {
                "evidence_id": r["evidence_id"],
                "raw_sha256": r["raw_sha256"],
                "raw_utf8": r["raw"].decode("utf-8"),
                "view": json.loads(r["view"]),
            }
            for r in rows
        ]
        frozen, summary_path = lab_evidence.freeze(
            {"run": text_of(snapshot), "model_requests": provider.requests},
            experiment="f7-step3-acceptance",
            run_id=str(run),
            evidence_dir=tmp_path / "evidence" / str(run),
            summary={
                "incident_id": str(incident),
                "verdicts": {"status": outcome.status, "reason": outcome.reason},
                "report": snapshot.get("conclusion"),
                "evidence": evidence_summary,
            },
        )
        sinks["summary.json"] = summary_path.read_text()
        ledger_files = list((tmp_path / "ledger").rglob("ledger.json"))
        assert ledger_files
        sinks["ledger.json"] = ledger_files[0].read_text()

        found = {name: leaks(text, ALL_SECRETS) for name, text in sinks.items()}
        assert found == {name: [] for name in sinks}
        for name, text in sinks.items():
            assert MODEL_KEY not in text, name
    finally:
        tracing.reset()
        for closer in harness.closers:
            closer.close()


def test_class6_a_run_recorded_under_the_old_tool_revision_is_blocked(dsn, monkeypatch):
    opener = _opener()
    harness = build_harness(dsn, monkeypatch, opener)
    try:
        current = harness.profile.versions()
        assert current["tool_schema_revision"] != OLD_OTEL_TOOL_SCHEMA_REVISION
        # Created before the bump: same versions except the tool revision.
        old_app = harness.workbench_app(
            {**current, "tool_schema_revision": OLD_OTEL_TOOL_SCHEMA_REVISION}
        )
        incident, run = harness.submit(QUESTION, target_id=TARGET_ID, app=old_app)
        provider = ScriptedProvider([])

        outcome = harness.run(incident, provider)

        assert (outcome.status, outcome.reason) == ("blocked", "INCOMPATIBLE_STATE")
        assert provider.requests == [] and opener.requests == []
        assert evidence_rows(harness, run) == []
        assert harness.store.rebuild(incident)["run"]["state"] != "completed"
    finally:
        for closer in harness.closers:
            closer.close()
