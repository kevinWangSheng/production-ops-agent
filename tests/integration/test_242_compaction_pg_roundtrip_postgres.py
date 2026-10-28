"""#242 compaction PG round trip, against real PostgreSQL.

Written from the #242 contract pre-review (2026-10-24, section 5; U-1 = A,
R-1..R-6 defaults) by an author who did not read the implementation branch.
Deterministic doubles only: no model HTTP, no real data source.

2. a Run whose input snapshot narrows only ``context_tokens`` is claimed and
   runs like a default one: not ``LIMITS_EXCEED_FREEZE``/``INCOMPATIBLE_STATE``,
   same prompt revision;
4. ``scripts/m1_live_runner.py --context-tokens`` outside
   ``(output_tokens, frozen ceiling]`` exits non-zero and writes no Run row;
5. after an accepted compaction, the Run rebuilt from its PG rows sits on the
   last accepted compaction's ``to_segment``, counts exactly the accepted
   compaction rows, still delivers the evidence folded into the compaction,
   and does not raise -- both after a published Run and across a real attempt
   boundary right after the compaction row; the live runner's frozen
   ``summary.json`` reports the compaction count and that offline rebuild;
7. a second claimer with no switch of its own resumes a narrowed Run and
   sends the same mechanism-1 stub the first attempt sent (from the snapshot
   limits, not process defaults).

6. a compaction row whose ``folded_step_ids`` were edited, or a round after
   the compaction whose recorded snapshot hash was edited, fails closed:
   the offline rebuild raises ``INCOMPATIBLE_STATE`` and a fresh claimer
   blocks the Run with the same code without calling the model.

Item 3 is covered by an existing regression:
``tests/integration/test_m1_loop_resume_postgres.py::
test_limits_above_the_freeze_are_refused_at_the_product_boundary``. Item 6
was listed as covered by ``tests/test_m1_investigation_compaction.py`` /
``tests/test_f13_recovery_compaction_contract.py``; neither edits a
compaction row (the latter is the postmortem input policy), so it is here.

ASSUMED summary field names (R-3 is implemented on another branch; align
these two constants with it, do not loosen the assertions):

- ``summary["compactions"] == {"accepted": int, "attempted": int, ...}``
- ``summary["offline_rebuild"] == {"segment": str, "compactions": int,
  "error": None | str, ...}``
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.investigation.context import (
    CONTEXT_POLICY,
    ContextError,
    InvestigationInput,
    estimate_tokens,
    rebuild_transcript,
)
from opspilot.investigation.limits import M1_FROZEN_LIMITS, RunLimits
from opspilot.investigation.loop import DISCIPLINE_VARIANT, prompt_revision_versions
from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore
from opspilot.tools import TransportResponse
from opspilot.tools.fixture import FIXTURE_TOOL
from opspilot.tools.registry import canonical
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN
from tests.m1_investigation_support import (
    TOOL_SCHEMAS,
    ScriptedModel,
    reply,
    report_json,
    tool_call,
)
from tests.m1_tool_support import WINDOW_END, WINDOW_START, body, build, registration

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

# Assumed R-3 summary fields; see the module docstring.
SUMMARY_COMPACTIONS = "compactions"
SUMMARY_OFFLINE_REBUILD = "offline_rebuild"

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = {
    **prompt_revision_versions(DISCIPLINE_VARIANT),
    "tool_schema_revision": "t242",
}
TARGET = "checkout-prod"
LEASE = 1
COMPACTED = "[context compacted rev="


class Crash(RuntimeError):
    pass


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()


def _rounds(count, *, start=1, name="metrics.range_query"):
    # Distinct arguments per round: an exact repeat is not re-dispatched.
    return [
        reply(
            tool_calls=[
                tool_call(
                    call_id=f"call-{start + i}",
                    name=name,
                    arguments=f'{{"expr":"rate(http_errors[{start + i}m])"}}',
                )
            ],
            finish="tool_calls",
        )
        for i in range(count)
    ]


def _summary_reply():
    return reply(
        content="Facts: checkout errors observed. Unknown: cause. Next: report.",
        finish="stop",
    )


def _input(run_id, *, model_requests, limits):
    schemas = [dict(s) for s in TOOL_SCHEMAS]
    return {
        "version": "opspilot-investigation-input-v1",
        "question": "Why is checkout erroring?",
        "model_requests": model_requests,
        "limits": limits.as_json(),
        "tool_schemas": schemas,
        "tool_face_sha256": hashlib.sha256(canonical(schemas).encode()).hexdigest(),
        "evidence_context": {
            "type": "opspilot-evidence-context-v4",
            "run_id": str(run_id),
            "time_policies": [
                {
                    "id": "policy-window-1",
                    "mode": "historical_window",
                    "all_authorized_targets": True,
                    "window": {
                        "start": WINDOW_START.isoformat(),
                        "end": WINDOW_END.isoformat(),
                    },
                }
            ],
        },
        "variant_id": DISCIPLINE_VARIANT,
        "bound_target_id": None,
        "scope_facts": {},
    }


class Lane:
    """One incident/Run accepted with the given limits in its input snapshot."""

    def __init__(self, *, limits, model_requests=8, large_views=False):
        self.store = DurableStore(DSN)
        self.store.install()
        self.limits = limits
        self.incident, self.run = uuid4(), uuid4()
        self.clock = SystemClock()
        self.deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.large_views = large_views
        self.store.accept(
            self.incident,
            self.run,
            f"t242-{self.incident}",
            deadline=self.deadline,
            budget_limit=model_requests,
            versions=VERSIONS,
            input=_input(self.run, model_requests=model_requests, limits=limits),
        )

    def executor_factory(self, lease, input):
        registrations = None
        rows = [{"metric": "checkout", "value": 3}]
        if self.large_views:
            # Let a large result through the registration so the loop's own
            # single-view cap (from the snapshot limits) is what bites.
            registrations = [
                registration(max_result_bytes=2_000_000, max_view_tokens=25_000)
            ]
            rows = [
                {"metric": "checkout", "value": i, "note": "n" * 40} for i in range(200)
            ]
        executor, transport, _sink, _clock = build(
            clock=self.clock,
            registrations=registrations,
            scope_overrides={"run_id": str(lease.run_id), "deadline": self.deadline},
        )
        transport.response = TransportResponse(body=body(rows), data_as_of=WINDOW_START)
        return executor

    def runner(self, model):
        # A claimer with no switch of its own: fresh Worker, default wiring.
        return InvestigationRunner(
            store=self.store,
            worker=Worker.create(self.store, dict(VERSIONS)),
            model=model,
            executor_factory=self.executor_factory,
            clock=self.clock,
            lease_seconds=LEASE,
        )

    def wait_lease(self):
        time.sleep(LEASE + 0.3)

    def rows(self):
        return self.store.rebuild(self.incident)

    def offline_rebuild(self):
        """U-1 A: rebuild the next call's context from the PG rows alone."""
        snapshot = self.rows()
        recorded = InvestigationInput.from_json(snapshot["run"]["input"])
        assert recorded.limits == self.limits  # the switch survived the PG trip
        transcript = rebuild_transcript(
            snapshot,
            run_id=str(self.run),
            authorized_targets=frozenset({TARGET}),
        )
        return snapshot, transcript


def _compaction_rows(snapshot):
    return [
        s
        for s in snapshot["steps"]
        if (s.get("response") or {}).get("kind") == "compaction"
    ]


def _accepted(snapshot):
    return [
        s for s in _compaction_rows(snapshot) if s["response"]["compaction"]["accepted"]
    ]


def _folded_evidence(snapshot):
    ids = []
    for row in _accepted(snapshot):
        ids.extend(row["response"]["compaction"]["digest"]["evidence_ids"])
    return ids


def _cite_folded(lane):
    """Final reply citing evidence that only exists before the compaction."""

    def final(call):
        folded = _folded_evidence(lane.rows())
        return reply(content=report_json(evidence_id=folded[0] if folded else "none"))

    return final


def _narrowed_between_rounds(k=2):
    """Limits whose compaction threshold falls between k-1 and k tool groups.

    Measured on a dry PG Run with frozen limits, so the value does not depend
    on the byte size of the discipline text.
    """
    dry = Lane(limits=replace(M1_FROZEN_LIMITS, model_requests=16), model_requests=16)
    model = ScriptedModel([*_rounds(k + 1)])  # runs dry afterwards: a handoff
    dry.runner(model).resume(dry.incident)
    before = estimate_tokens(model.calls[k - 1].messages, model.calls[k - 1].tools)
    after = estimate_tokens(model.calls[k].messages, model.calls[k].tools)
    assert after > before
    output = 64
    budget = int((before + after) / 2 / CONTEXT_POLICY.compaction_pct)
    return RunLimits(
        model_requests=16, context_tokens=budget + output, output_tokens=output
    )


def _assert_rebuilt_across_compaction(snapshot, transcript):
    accepted = _accepted(snapshot)
    assert accepted, "no accepted compaction row was committed"
    last = accepted[-1]["response"]["compaction"]
    assert transcript.segment == last["to_segment"]
    assert transcript.compactions == len(accepted)
    folded = _folded_evidence(snapshot)
    assert folded, "the compaction folded no evidence"
    delivered = {view.evidence_id for view in transcript.delivered}
    for evidence_id in folded:
        assert evidence_id in transcript.evidence_ids
        assert evidence_id in delivered


# -- 2. a narrowed snapshot is claimed and runs like a default one -----------


def test_a_narrowed_context_window_is_claimed_and_keeps_the_prompt_revision():
    narrowed = replace(
        M1_FROZEN_LIMITS, context_tokens=M1_FROZEN_LIMITS.output_tokens + 20_000
    )
    outcomes = {}
    for label, limits in (("default", M1_FROZEN_LIMITS), ("narrowed", narrowed)):
        lane = Lane(limits=limits, model_requests=4)
        model = ScriptedModel([*_rounds(1), _cite_any()])
        outcome = lane.runner(model).resume(lane.incident)
        assert outcome.status == "published", outcome
        assert outcome.reason not in ("LIMITS_EXCEED_FREEZE", "INCOMPATIBLE_STATE")
        assert lane.rows()["run"]["state"] == "completed"
        outcomes[label] = outcome
    assert (
        outcomes["narrowed"].loop.prompt_revision
        == outcomes["default"].loop.prompt_revision
    )


def _cite_any():
    def final(call):
        for message in reversed(call.messages):
            if message.get("role") == "tool":
                evidence_id = json.loads(message["content"])["evidence_id"]
                return reply(content=report_json(evidence_id=evidence_id))
        return reply(content=report_json(evidence_id="none"))

    return final


# -- 5. PG round trip across an accepted compaction --------------------------


def test_a_published_narrowed_run_rebuilds_from_pg_across_its_compaction():
    lane = Lane(limits=_narrowed_between_rounds(2), model_requests=8)
    model = ScriptedModel([*_rounds(2), _summary_reply(), _cite_folded(lane)])
    outcome = lane.runner(model).resume(lane.incident)
    assert outcome.status == "published", outcome
    snapshot, transcript = lane.offline_rebuild()
    _assert_rebuilt_across_compaction(snapshot, transcript)
    keys = [s["logical_key"] for s in snapshot["steps"]]
    first_to = _accepted(snapshot)[0]["response"]["compaction"]["to_segment"]
    assert any(key.startswith(f"{first_to}:round-") for key in keys), keys
    # The published report cites evidence that was folded by the compaction.
    assert set(outcome.loop.evidence_ids) & set(_folded_evidence(snapshot))


def test_a_crash_right_after_the_compaction_row_resumes_on_the_new_segment():
    lane = Lane(limits=_narrowed_between_rounds(2), model_requests=8)
    with pytest.raises(Crash):
        lane.runner(
            ScriptedModel([*_rounds(2), _summary_reply(), Crash("killed")])
        ).resume(lane.incident)
    snapshot, transcript = lane.offline_rebuild()
    _assert_rebuilt_across_compaction(snapshot, transcript)
    assert transcript.compactions == 1
    assert not [
        s
        for s in snapshot["steps"]
        if s["logical_key"].startswith(f"{transcript.segment}:round-")
    ], "no round may exist yet on the new segment"

    lane.wait_lease()
    second = ScriptedModel([_cite_folded(lane)])
    outcome = lane.runner(second).resume(lane.incident)
    assert outcome.status == "published", outcome
    assert outcome.epoch == 2
    sent = list(second.calls[0].messages)
    assert any(
        isinstance(m.get("content"), str) and m["content"].startswith(COMPACTED)
        for m in sent
    )
    assert [m["role"] for m in sent].count("tool") == 0
    # Rebuilt again after the second attempt published: still consistent.
    snapshot, transcript = lane.offline_rebuild()
    _assert_rebuilt_across_compaction(snapshot, transcript)
    assert transcript.compactions == 1


# -- 6. an edited compaction row or post-compaction hash fails closed -------


def _crashed_after_a_post_compaction_round():
    lane = Lane(limits=_narrowed_between_rounds(2), model_requests=8)
    with pytest.raises(Crash):
        lane.runner(
            ScriptedModel(
                [*_rounds(2), _summary_reply(), *_rounds(1, start=3), Crash("killed")]
            )
        ).resume(lane.incident)
    snapshot, transcript = lane.offline_rebuild()  # intact rows rebuild fine
    _assert_rebuilt_across_compaction(snapshot, transcript)
    after = [
        s["logical_key"]
        for s in snapshot["steps"]
        if s["logical_key"].startswith(f"{transcript.segment}:round-")
    ]
    assert after, "expected a committed round on the new segment"
    return lane, _accepted(snapshot)[0]["logical_key"], after[0]


def _edit(lane, logical_key, path, value):
    with psycopg.connect(DSN, autocommit=True) as conn:
        changed = conn.execute(
            "UPDATE opspilot_steps SET response = jsonb_set(response, %s, %s::jsonb) "
            "WHERE run_id = %s AND logical_key = %s",
            (path, json.dumps(value), lane.run, logical_key),
        ).rowcount
    assert changed == 1


def _assert_fails_closed(lane):
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        lane.offline_rebuild()
    lane.wait_lease()
    model = ScriptedModel([_cite_folded(lane)])
    outcome = lane.runner(model).resume(lane.incident)
    assert outcome.status == "blocked" and outcome.reason == "INCOMPATIBLE_STATE", (
        outcome
    )
    assert model.calls == []
    assert lane.rows()["run"]["state"] == "blocked"


def test_an_edited_folded_step_list_on_the_compaction_row_fails_closed():
    lane, compaction_key, _ = _crashed_after_a_post_compaction_round()
    folded = lane.rows()
    (row,) = [s for s in folded["steps"] if s["logical_key"] == compaction_key]
    _edit(
        lane,
        compaction_key,
        "{compaction,folded_step_ids}",
        row["response"]["compaction"]["folded_step_ids"][:1],
    )
    _assert_fails_closed(lane)


def test_an_edited_snapshot_hash_after_the_compaction_fails_closed():
    lane, _, round_key = _crashed_after_a_post_compaction_round()
    _edit(lane, round_key, "{context,input_snapshot_hash}", "0" * 64)
    _assert_fails_closed(lane)


# -- 7. another claimer recomputes the same stub from the snapshot limits ----


def _tool_messages(call):
    return [m for m in call.messages if m["role"] == "tool"]


def _first_attempt_tool_view(lane):
    model = ScriptedModel([*_rounds(1), Crash("killed")])
    with pytest.raises(Crash):
        lane.runner(model).resume(lane.incident)
    (view,) = _tool_messages(model.calls[1])
    return view


def test_a_second_claimer_sends_the_same_stub_from_the_snapshot_limits():
    narrowed = RunLimits(model_requests=8, context_tokens=6000, output_tokens=200)
    lane = Lane(limits=narrowed, model_requests=4, large_views=True)
    first = _first_attempt_tool_view(lane)
    stub = json.loads(first["content"])
    assert stub.get("spilled") is True and stub.get("reason") == "VIEW_TOO_LARGE"

    lane.wait_lease()
    second = ScriptedModel([_cite_any()])
    outcome = lane.runner(second).resume(lane.incident)
    assert outcome.status == "published", outcome
    assert _tool_messages(second.calls[0]) == [first]

    # Control: the same view under the frozen limits is not stubbed, so the
    # stub above came from the snapshot, not from a process-wide default.
    control = Lane(limits=M1_FROZEN_LIMITS, model_requests=4, large_views=True)
    plain = json.loads(_first_attempt_tool_view(control)["content"])
    assert plain.get("spilled") is not True and "content" in plain


# -- 4. the script refuses an out-of-range window before writing anything ----


@pytest.fixture
def scratch_dsn():
    name = f"opspilot_t242_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


def _run_rows(dsn):
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT count(*) FROM opspilot_runs").fetchone()[0]


@pytest.mark.parametrize(
    "value",
    [
        0,
        M1_FROZEN_LIMITS.output_tokens,
        M1_FROZEN_LIMITS.context_tokens + 1,
    ],
)
def test_the_script_rejects_an_out_of_range_window_and_writes_no_run(
    scratch_dsn, tmp_path, value
):
    env_file = tmp_path / "fake.env"
    env_file.write_text("DEEPSEEK_API_KEY=not-a-real-key-t242\n")
    dead = "http://127.0.0.1:9"  # any outbound HTTP fails fast, never leaves
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("LANGSMITH_")},
        "OPSPILOT_LAB_DSN": scratch_dsn,
        "M0_ENV_FILE": str(env_file),
        "M1_ACCEPTANCE_OUT": str(tmp_path / "out"),
        "OPSPILOT_LEDGER_DIR": str(tmp_path / "ledger"),
        "HTTPS_PROXY": dead,
        "HTTP_PROXY": dead,
        "ALL_PROXY": dead,
        "NO_PROXY": "",
    }
    env.pop("OPSPILOT_TRACE", None)
    assert _run_rows(scratch_dsn) == 0
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.m1_live_runner",
            "--context-tokens",
            str(value),
            "--model-requests",
            "2",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode != 0, done.stdout + done.stderr
    assert _run_rows(scratch_dsn) == 0
    assert not (tmp_path / "out").exists()


# -- 5 (R-3). the live runner's frozen summary carries the round trip --------


def _drive_live_runner(monkeypatch, tmp_path, label, model, argv):
    import scripts.m1_live_runner as live

    env_file = tmp_path / "fake.env"
    env_file.write_text("DEEPSEEK_API_KEY=not-a-real-key-t242\n")
    out = tmp_path / label
    monkeypatch.setenv("M0_ENV_FILE", str(env_file))
    monkeypatch.setenv("M1_ACCEPTANCE_OUT", str(out))
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("OPSPILOT_TRACE", raising=False)
    # The scripted model stands in for DeepSeek; nothing leaves the process.
    monkeypatch.setattr(live, "DeepSeekClient", lambda *a, **k: model)
    monkeypatch.setattr(sys, "argv", ["m1_live_runner", *argv])
    assert live.main() == 0
    (summary_path,) = out.glob("*/summary.json")
    return json.loads(summary_path.read_text())


def _cite_from_calls(model_ref):
    def final(call):
        for earlier in [*model_ref().calls, call]:
            for message in earlier.messages:
                if message.get("role") == "tool":
                    try:
                        evidence_id = json.loads(message["content"])["evidence_id"]
                    except (ValueError, KeyError, TypeError):
                        continue
                    return reply(content=report_json(evidence_id=evidence_id))
        return reply(content=report_json(evidence_id="none"))

    return final


def test_the_live_runner_summary_reports_compactions_and_the_offline_rebuild(
    monkeypatch, tmp_path
):
    if os.environ.get("OPSPILOT_LAB_DSN") is None:
        pytest.skip("the live runner targets OPSPILOT_LAB_DSN in this test")
    # Dry run with the frozen window to measure where two tool groups land.
    dry = ScriptedModel(_rounds(3, name=FIXTURE_TOOL))
    _drive_live_runner(monkeypatch, tmp_path, "dry", dry, ["--model-requests", "8"])
    before = estimate_tokens(dry.calls[1].messages, dry.calls[1].tools)
    after = estimate_tokens(dry.calls[2].messages, dry.calls[2].tools)
    assert after > before
    window = M1_FROZEN_LIMITS.output_tokens + int(
        (before + after) / 2 / CONTEXT_POLICY.compaction_pct
    )

    model = ScriptedModel(
        [
            *_rounds(2, name=FIXTURE_TOOL),
            _summary_reply(),
            _cite_from_calls(lambda: model),
        ]
    )
    summary = _drive_live_runner(
        monkeypatch,
        tmp_path,
        "narrowed",
        model,
        ["--model-requests", "8", "--context-tokens", str(window)],
    )
    assert summary["context_tokens"] == window

    # Independent check against the PG rows the Run left behind.
    rows = DurableStore(DSN).rebuild(UUID(summary["incident_id"]))
    accepted = _accepted(rows)
    assert len(accepted) == 1, [s["logical_key"] for s in rows["steps"]]

    compactions = summary[SUMMARY_COMPACTIONS]
    assert compactions["accepted"] == len(accepted)
    assert compactions["attempted"] >= compactions["accepted"]
    assert compactions["attempted"] == len(_compaction_rows(rows))

    rebuilt = summary[SUMMARY_OFFLINE_REBUILD]
    assert rebuilt["error"] is None
    assert rebuilt["segment"] == accepted[-1]["response"]["compaction"]["to_segment"]
    assert rebuilt["compactions"] == len(accepted)
