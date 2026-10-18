"""PR-C r5 CLI, resource ownership and lifecycle acceptance, entirely offline."""

from __future__ import annotations

import fcntl
import json
import socket
import subprocess
from dataclasses import replace
from datetime import timedelta

import pytest

from opspilot.evaluation import load_attempt, load_round
from scripts import eval_live, kind_lab
from scripts import eval_live_lab as lab
from tests.acceptance import eval_c_support as s


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("PR-C offline acceptance attempted a real network or process call")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    monkeypatch.setattr(socket.socket, "sendall", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(eval_live, "real_deps", forbidden)


@pytest.fixture
def h(tmp_path):
    return s.Harness(tmp_path)


def assert_no_attempt(h):
    assert not list(h.deps.evidence_root.glob("**/attempt.json"))
    assert not h.pg.started
    assert not h.flags.writes
    assert not h.investigator.requests
    assert h.deps.balance.calls == 0
    assert not list(h.deps.ledger_root.glob(".active-*.json"))


@pytest.mark.parametrize("option", ["--mode", "--round", "--n", "--parallel"])
def test_cli_requires_explicit_mode_round_n_parallel(h, option):
    argv = h.argv()
    index = argv.index(option)
    del argv[index : index + 2]
    before = {p: p.read_bytes() for p in h.root.rglob("*") if p.is_file()}
    try:
        code = eval_live.main(argv, deps=h.deps)
    except SystemExit as exc:
        code = exc.code
    assert code == 2
    assert {p: p.read_bytes() for p in h.root.rglob("*") if p.is_file()} == before
    assert_no_attempt(h)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--mode", "fixture"),
        ("--round", "../escape"),
        ("--round", "Upper"),
        ("--round", "x" * 64),
        ("--n", "0"),
        ("--n", "-1"),
        ("--n", "bad"),
        ("--parallel", "2"),
        ("--scenario", "unknown"),
        ("--flagd-url", "https://localhost:8013"),
        ("--flagd-url", "http://example.test:8013"),
        ("--flagd-url", "http://localhost:8013/path"),
        ("--flagd-url", "http://localhost:8013?x=1"),
        ("--flagd-url", "http://CANARY-CREDENTIAL@localhost:8013"),
    ],
)
def test_cli_invalid_arguments_are_usage_errors_without_writes(h, option, value):
    argv = h.argv()
    argv[argv.index(option) + 1] = value
    before = {p: p.read_bytes() for p in h.root.rglob("*") if p.is_file()}
    try:
        code = eval_live.main(argv, deps=h.deps)
    except SystemExit as exc:
        code = exc.code
    assert code == 2
    assert {p: p.read_bytes() for p in h.root.rglob("*") if p.is_file()} == before
    assert_no_attempt(h)


def test_all_cannot_mix_with_specific_scenario(h):
    try:
        code = eval_live.main(h.argv("all") + ["--scenario", s.FAULT], deps=h.deps)
    except SystemExit as exc:
        code = exc.code
    assert code == 2
    assert_no_attempt(h)


def test_dry_run_reads_only_and_never_invokes_a_seam(h, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run invoked an injected seam")

    monkeypatch.setattr(h.investigator, "begin_round", forbidden, raising=False)
    for obj, method in [
        (h.clock, "now"),
        (h.clock, "monotonic"),
        (h.flags, "read"),
        (h.flagd, "resolve"),
        (h.identity, "verify"),
        (h.ports, "in_use"),
        (h.pg, "start"),
        (h.deps.balance, "balance"),
        (h.investigator, "investigate"),
        (h.env, "check"),
        (h.provenance, "read"),
    ]:
        monkeypatch.setattr(obj, method, forbidden)
    before = {p: p.read_bytes() for p in h.root.rglob("*") if p.is_file()}
    assert eval_live.main(h.argv("all", 2) + ["--dry-run"], deps=h.deps) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "round": s.ROUND,
        "status": "dry_run",
        "slots": [
            {
                "scenario_id": spec.scenario_id,
                "repetition": repetition,
                "kind": spec.kind,
                "flag": answer.injected.flag if answer.injected else None,
            }
            for spec, answer in h.scenarios
            for repetition in (1, 2)
        ],
        "evidence": f"docs/evidence/eval/{s.ROUND}/",
        "ledger": f"tmp/lab-ledgers/{s.ROUND}/",
    }
    assert {p: p.read_bytes() for p in h.root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("missing", "LAB_RESERVATION_REQUIRED"),
        ("foreign", "LAB_OCCUPIED"),
        ("expired", "LAB_RESERVATION_EXPIRED"),
        ("equal-now", "LAB_RESERVATION_EXPIRED"),
    ],
)
def test_reservation_is_required_owned_and_strictly_unexpired(h, capsys, state, reason):
    if state == "missing":
        h.reservation.unlink()
    else:
        doc = json.loads(h.reservation.read_bytes())
        if state == "foreign":
            doc["holder"] = "CANARY-FOREIGN-HOLDER"
        else:
            doc["expected_end"] = (
                h.clock.now() - timedelta(seconds=state != "equal-now")
            ).isoformat()
        h.reservation.write_bytes(s.encode(doc))
    before = h.reservation.read_bytes() if h.reservation.exists() else None
    code, result = h.final(capsys)
    assert (code, result["status"], result["reason"]) == (3, "refused", reason)
    assert_no_attempt(h)
    assert (h.reservation.read_bytes() if h.reservation.exists() else None) == before


@pytest.mark.parametrize(
    ("conflict", "reason"),
    [
        ("fault", "FAULT_RECOVERY_REQUIRED"),
        ("identity", "LAB_IDENTITY_UNCONFIRMED"),
        ("port", "PG_PORT_IN_USE"),
        ("round", "ROUND_EXISTS"),
        ("flag", "FLAGS_NOT_NORMAL"),
    ],
)
def test_preflight_refuses_conflicts_without_starting(h, capsys, conflict, reason):
    foreign = h.deps.ledger_root / "fault-active.json"
    if conflict == "fault":
        foreign.write_bytes(b' {"owner":"CANARY-FOREIGN"} ')
    elif conflict == "identity":
        h.identity.result = ("CANARY-UNKNOWN-LAB",)
    elif conflict == "port":
        h.ports.busy = True
    elif conflict == "round":
        h.public().mkdir(parents=True)
    else:
        raw = json.loads(h.flags.current.raw)
        raw["flags"]["cartFailure"]["defaultVariant"] = "on"
        h.flags.current = replace(h.flags.current, raw=s.encode(raw))
    code, result = h.final(capsys)
    assert (code, result["reason"]) == (3, reason)
    assert_no_attempt(h)
    if conflict == "fault":
        assert foreign.read_bytes() == b' {"owner":"CANARY-FOREIGN"} '


def test_fault_guard_is_nonblocking_preflight_refusal(h, capsys):
    with (h.deps.ledger_root / ".fault.guard").open("w") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, result = h.final(capsys)
    assert (code, result["reason"]) == (3, "LAB_OPERATION_IN_PROGRESS")
    assert_no_attempt(h)


@pytest.mark.parametrize(("flag", "variant"), s.VARIANTS.items())
def test_kind_lab_mutates_only_whitelisted_flag(h, flag, variant):
    original = json.loads(s.flags_raw())
    updated = json.loads(
        kind_lab.mutate_flags(s.flags_raw(), variant, "off", flag=flag)
    )
    expected = json.loads(s.flags_raw())
    expected["flags"][flag]["defaultVariant"] = variant
    assert updated == expected
    restored = json.loads(
        kind_lab.mutate_flags(s.encode(updated), "off", variant, flag=flag)
    )
    assert restored == original


def test_kind_lab_default_remains_payment():
    raw = kind_lab.mutate_flags(s.flags_raw(), "100%", "off")
    assert json.loads(raw)["flags"]["paymentFailure"]["defaultVariant"] == "100%"
    assert json.loads(raw)["flags"]["cartFailure"]["defaultVariant"] == "off"


@pytest.mark.parametrize(
    "bad", ["unknown", "missing-flag", "missing-variant", "unexpected-current"]
)
def test_flag_mutation_rejects_non_whitelist_or_missing_configuration(bad):
    raw = json.loads(s.flags_raw())
    flag = "paymentFailure"
    if bad == "unknown":
        flag = "CANARY-UNKNOWN-FLAG"
    elif bad == "missing-flag":
        del raw["flags"][flag]
    elif bad == "missing-variant":
        del raw["flags"][flag]["variants"]["100%"]
    else:
        raw["flags"][flag]["defaultVariant"] = "100%"
    with pytest.raises(SystemExit):
        kind_lab.mutate_flags(s.encode(raw), "100%", "off", flag=flag)


def test_answer_flag_outside_whitelist_is_usage_error(h, capsys):
    path = h.deps.scenarios_root / s.FAULT / "answer.json"
    answer = json.loads(path.read_bytes())
    answer["injected"]["flag"] = "CANARY-UNKNOWN-FLAG"
    path.write_bytes(s.encode(answer))
    try:
        code = eval_live.main(h.argv(), deps=h.deps)
    except SystemExit as exc:
        code = exc.code
    assert code == 2
    assert_no_attempt(h)


@pytest.mark.parametrize(
    "scenario", [s.FAULT, "eval-cart-failure", "eval-product-catalog-failure", s.NORMAL]
)
def test_clean_lifecycle_holds_active_preserves_reservation_and_restores(
    h, capsys, scenario
):
    reservation = h.reservation.read_bytes()
    original = h.flags.current.raw
    code, result = h.final(capsys, argv=h.argv(scenario))
    assert (code, result["status"]) == (0, "finished")
    assert [slot["status"] for slot in result["slots"]] == ["ran"]
    assert len(h.investigator.requests) == 1
    assert h.flags.current.raw == original
    assert h.reservation.read_bytes() == reservation
    assert not list(h.deps.ledger_root.glob(".active-*.json"))
    assert not (h.deps.ledger_root / "fault-active.json").exists()
    assert h.pg.stopped == h.pg.started
    assert len(h.pg.started) == 1
    if scenario == s.NORMAL:
        assert not h.flags.writes
    else:
        assert len(h.flags.writes) >= 2
    summary = h.summaries()[0]
    assert summary["schema"] == "opspilot-eval-summary-1"
    assert summary["capture"]["judge"] is None
    assert summary["evaluation"]["overall"] is False
    assert summary["capture"]["outcome"]["decision"] == "handoff"
    from opspilot.evaluation import check_recording_binding, load_recording

    path, ref = h.investigator.recordings[0]
    recording = load_recording(path, expected_hash=ref.sha256)
    returned = h.investigator.results[0]
    assert recording.context.synthetic is False
    assert recording.outcome == returned.outcome
    request = h.investigator.requests[0]
    check_recording_binding(
        recording,
        replace(request.attempt, product_run_id=returned.product_run_id),
        round_id=s.ROUND,
        epoch=returned.usage["epoch"],
        manifest=request.manifest,
    )


def test_journal_exists_unknown_before_setup_and_failures_are_not_replaced(h, capsys):
    preflight = True

    def identity():
        nonlocal preflight
        if preflight:
            preflight = False
            return ()
        journals = list(h.public().glob("runs/*/attempt.json"))
        assert journals
        journal = load_attempt(journals[-1])
        assert journal.attempt.started is True
        assert journal.attempt.status == "unknown"
        assert journal.attempt.product_run_id is None
        return ("CANARY-UNCONFIRMED",)

    h.identity.callback = identity
    code, result = h.final(capsys, argv=h.argv(n=2))
    assert code == 1
    assert [slot["status"] for slot in result["slots"]] == ["setup_failed"] * 2
    assert not h.investigator.requests
    assert len(h.summaries()) == 2
    for summary in h.summaries():
        assert "SETUP_FAILED" in summary["evaluation"]["failure_reasons"]
        assert "EVIDENCE_INCOMPLETE" in summary["evaluation"]["failure_reasons"]
        assert summary["capture"]["traces"] == []
        assert summary["capture"]["attempt"]["status"] == "setup_failed"
    aggregate_path = h.public() / "aggregate.json"

    # All refs here are public journals/summaries or private attempt artifacts.
    def resolve(ref):
        candidates = [
            p
            for p in h.root.rglob("*")
            if p.is_file() and p.as_posix().endswith(ref.path)
        ]
        return next(
            p.read_bytes() for p in candidates if s.sha(p.read_bytes()) == ref.sha256
        )

    aggregate = load_round(aggregate_path, resolve=resolve)
    assert sum(row.attempted for row in aggregate.rows) == 2
    assert sum(row.failed for row in aggregate.rows) == 2
    assert sum(row.not_started for row in aggregate.rows) == 0


@pytest.mark.parametrize("failure", ["returned-error", "recording-error", "exception"])
def test_investigation_failures_are_aborted_and_keep_their_slot(h, capsys, failure):
    if failure == "returned-error":
        h.investigator.error = "CANARY_INVESTIGATION_FAILED"
    elif failure == "recording-error":
        h.investigator.recording_error = "RECORDING_INCOMPLETE"
    else:

        def crash(request):
            raise RuntimeError(s.SECRET)

        h.investigator.callback = crash
    code, result = h.final(capsys, argv=h.argv(n=2))
    assert code == 1
    assert [slot["status"] for slot in result["slots"]] == ["aborted"] * 2
    assert len(h.investigator.requests) == 2
    assert h.flags.current.raw == s.flags_raw()
    assert h.pg.stopped == h.pg.started
    if failure != "exception":
        assert all(
            doc["capture"]["outcome"]["decision"] == "handoff" for doc in h.summaries()
        )


@pytest.mark.parametrize("mode", ["normal-propagation", "wash"])
def test_restore_timeout_stops_round_retains_fault_and_leaves_slots_unstarted(
    h, capsys, mode
):
    def during_run(request):
        if mode == "normal-propagation":
            h.flagd.override = lambda flag, variant: None
        else:
            h.telemetry.bad_wash = True

    h.investigator.callback = during_run
    code, result = h.final(capsys, argv=h.argv(n=3))
    assert (code, result["status"]) == (4, "stopped")
    assert [slot["status"] for slot in result["slots"]] == [
        "aborted",
        "not_started",
        "not_started",
    ]
    assert len(h.investigator.requests) == 1
    assert len(list(h.public().glob("runs/*/attempt.json"))) == 1
    assert (h.deps.ledger_root / "fault-active.json").exists()
    assert h.reservation.exists()
    assert len(h.summaries()) == 1


def test_original_value_uses_fenced_restore_instead_of_late_overwrite(h, capsys):
    def already_original(request):
        h.flags.current = replace(
            h.flags.current, raw=s.flags_raw(), resource_version="9"
        )

    h.investigator.callback = already_original
    code, _ = h.final(capsys)
    assert code == 0
    unchanged = [
        (old, raw, fence) for old, raw, fence in h.flags.writes if old.raw == raw
    ]
    assert unchanged and all(fence for _, _, fence in unchanged)


@pytest.mark.parametrize("foreign", ["content", "uid"])
def test_restore_never_overwrites_foreign_configmap(h, capsys, foreign):
    def change(request):
        if foreign == "uid":
            h.flags.current = replace(h.flags.current, uid="CANARY-FOREIGN-UID")
        else:
            doc = json.loads(h.flags.current.raw)
            doc["CANARY-FOREIGN"] = "CANARY-FOREIGN-VALUE"
            h.flags.current = replace(
                h.flags.current, raw=s.encode(doc), resource_version="9"
            )

    h.investigator.callback = change
    code, _ = h.final(capsys, argv=h.argv(n=2))
    assert code == 4
    assert len(h.flags.writes) == 1
    assert (h.deps.ledger_root / "fault-active.json").exists()
    if foreign == "uid":
        assert h.flags.current.uid == "CANARY-FOREIGN-UID"
    else:
        assert (
            json.loads(h.flags.current.raw)["CANARY-FOREIGN"] == "CANARY-FOREIGN-VALUE"
        )


def test_unknown_write_result_still_restores_from_persisted_intent(h, capsys):
    def unknown_once():
        h.flags.after_write = None
        raise lab.LabError("FLAG_WRITE_UNKNOWN")

    h.flags.after_write = unknown_once
    code, result = h.final(capsys)
    assert code == 1
    assert result["slots"][0]["status"] == "setup_failed"
    assert h.flags.current.raw == s.flags_raw()
    assert not (h.deps.ledger_root / "fault-active.json").exists()
    assert not h.investigator.requests


@pytest.mark.parametrize("restore_fails", [False, True])
def test_interrupted_restores_before_exit_and_restore_failure_takes_precedence(
    h, capsys, restore_fails
):
    def interrupt(request):
        if restore_fails:
            h.flagd.override = lambda flag, variant: None
        raise lab.Interrupted()

    h.investigator.callback = interrupt
    code, result = h.final(capsys, argv=h.argv(n=2))
    assert code == (4 if restore_fails else 130)
    assert result["slots"][1]["status"] == "not_started"
    assert h.flags.current.raw == s.flags_raw()
    assert (h.deps.ledger_root / "fault-active.json").exists() is restore_fails
    assert h.pg.stopped == h.pg.started


@pytest.mark.parametrize("scenario", [s.FAULT, s.NORMAL])
def test_stale_preobservation_is_not_a_successful_run(h, capsys, scenario):
    h.investigator.submission_delay = 301
    h.investigator.outcome_transform = lambda outcome: replace(
        outcome,
        final_state="completed",
        decision="report_available",
        report_available=True,
        handoff_reasons=(),
    )
    code, result = h.final(capsys, argv=h.argv(scenario))
    assert code == 1
    assert result["slots"][0]["status"] == "setup_failed"
    lifecycle = json.loads((h.public() / "lifecycle.json").read_bytes())
    slot = lifecycle["slots"][0]
    assert slot["reason"] == "PRE_OBSERVATION_STALE"
    returned = h.investigator.results[0]
    summary = h.summaries()[0]
    assert summary["capture"]["attempt"]["product_run_id"] == returned.product_run_id
    assert summary["capture"]["outcome"]["decision"] == "report_available"
    assert summary["capture"]["outcome"]["handoff_reasons"] == []
    assert summary["capture"]["outcome"] == json.loads(
        s.encode_outcome(returned.outcome)
    )
    private = b"".join(
        path.read_bytes() for path in (h.deps.ledger_root / s.ROUND).rglob("*.json*")
    )
    assert returned.product_run_id.encode() in private


def test_foreign_pg_directory_is_preserved_and_not_launched(h, capsys):
    first = True
    marker = None

    def identity():
        nonlocal first, marker
        if first:
            first = False
        else:
            journal = load_attempt(next(h.public().glob("runs/*/attempt.json")))
            directory = h.deps.pg_root / s.ROUND / journal.attempt.attempt_id / "pg"
            directory.mkdir(parents=True)
            marker = directory / "CANARY-FOREIGN-DATA"
            marker.write_bytes(b"CANARY-KEEP")
        return ()

    h.identity.callback = identity
    code, result = h.final(capsys)
    assert code == 1
    assert result["slots"][0]["status"] == "setup_failed"
    assert not h.pg.started and not h.pg.stopped
    assert marker.read_bytes() == b"CANARY-KEEP"
    assert "PG_DIRECTORY_EXISTS" in (h.public() / "lifecycle.json").read_text()


def test_public_outputs_exclude_credentials_raw_http_and_private_answer(h, capsys):
    h.investigator.error = "CANARY_INVESTIGATION_FAILED"
    code, _ = h.final(capsys)
    assert code == 1
    paths = list(h.public().rglob("*.json"))
    assert {"attempt.json", "summary.json", "lifecycle.json", "aggregate.json"} <= {
        p.name for p in paths
    }
    for path in paths:
        raw = path.read_text()
        for marker in (s.SECRET, s.RAW_HTTP, s.PRIVATE_ANSWER):
            assert marker not in raw
    private = h.deps.ledger_root / s.ROUND
    assert private.is_relative_to(h.root / "tmp")
    for path in private.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    assert h.deps.recording_root.is_relative_to(h.root / "tmp")
    private_observations = b"".join(
        path.read_bytes() for path in private.rglob("pre-observation.json")
    )
    assert s.PRIVATE_ANSWER.encode() in private_observations
    recorded = b"".join(
        path.read_bytes() for path in h.deps.recording_root.rglob("*") if path.is_file()
    )
    assert s.RAW_HTTP.encode() in recorded


def test_recover_refuses_missing_state_without_starting_anything(h, capsys):
    argv = [
        "--recover",
        "--round",
        s.ROUND,
        "--holder",
        s.HOLDER,
        "--batch",
        s.BATCH,
        "--flagd-url",
        h.deps.flagd_url,
    ]
    code, result = h.final(capsys, argv=argv)
    assert (code, result["status"]) == (3, "refused")
    assert_no_attempt(h)


def test_explicit_recovery_washes_retained_state_without_rewriting_summary(h, capsys):
    h.investigator.callback = lambda request: setattr(h.telemetry, "bad_wash", True)
    code, _ = h.final(capsys)
    assert code == 4
    summaries = {
        path: path.read_bytes() for path in h.public().glob("runs/*/summary.json")
    }
    assert summaries
    lifecycle_path = h.public() / "lifecycle.json"
    lifecycle_before = lifecycle_path.read_bytes()
    h.telemetry.bad_wash = False
    argv = [
        "--recover",
        "--round",
        s.ROUND,
        "--holder",
        s.HOLDER,
        "--batch",
        s.BATCH,
        "--flagd-url",
        h.deps.flagd_url,
    ]
    code, result = h.final(capsys, argv=argv)
    assert (code, result["status"]) == (0, "recovered")
    assert not (h.deps.ledger_root / "fault-active.json").exists()
    assert {path: path.read_bytes() for path in summaries} == summaries
    assert lifecycle_path.read_bytes() == lifecycle_before
    assert json.loads(lifecycle_before)["recovered_at"] is None
    recovery_paths = list(h.public().glob("recovery-*.json"))
    assert len(recovery_paths) == 1
    recovery = json.loads(recovery_paths[0].read_bytes())
    assert set(recovery) == {"schema", "round", "attempt_id", "recovered_at"}
    assert recovery["schema"] == "opspilot-eval-live-recovery-1"
    assert recovery["round"] == s.ROUND
    assert recovery["attempt_id"] == h.investigator.requests[0].attempt.attempt_id
    assert recovery["recovered_at"] is not None
    assert list((h.deps.ledger_root / s.ROUND).glob("*/recovery.jsonl"))


def test_intent_is_exclusive_if_foreign_state_appears_during_setup(h, capsys):
    foreign = h.deps.ledger_root / "fault-active.json"

    def race(directory, port):
        foreign.write_bytes(b'{"owner":"CANARY-FOREIGN"}')

    h.pg.callback = race
    code, result = h.final(capsys, argv=h.argv(n=3))
    assert (code, result["status"], result["reason"]) == (
        1,
        "stopped",
        "LAB_FAULT_FOREIGN",
    )
    assert [slot["status"] for slot in result["slots"]] == [
        "setup_failed",
        "not_started",
        "not_started",
    ]
    lifecycle = json.loads((h.public() / "lifecycle.json").read_bytes())
    assert lifecycle["slots"][0]["reason"] == "LAB_FAULT_FOREIGN"
    assert len(list(h.public().glob("runs/*/attempt.json"))) == 1
    assert not h.flags.writes
    assert foreign.read_bytes() == b'{"owner":"CANARY-FOREIGN"}'
    assert not h.investigator.requests


def test_pg_port_becoming_busy_during_setup_never_switches_or_stops_foreign_pg(
    h, capsys
):
    count = 0

    def probe(port):
        nonlocal count
        assert port == 55497
        count += 1
        return count > 1

    h.ports.in_use = probe
    code, result = h.final(capsys)
    assert code == 1
    assert result["slots"][0]["status"] == "setup_failed"
    assert not h.pg.started and not h.pg.stopped
    assert "PG_PORT_IN_USE" in (h.public() / "lifecycle.json").read_text()


def test_all_plan_is_scenario_major_and_n_slots_are_never_replaced(h, capsys):
    code, result = h.final(capsys, argv=h.argv("all", 2))
    assert code == 0
    actual = [(slot["scenario_id"], slot["repetition"]) for slot in result["slots"]]
    expected = [
        (spec.scenario_id, repetition)
        for spec, _ in h.scenarios
        for repetition in (1, 2)
    ]
    assert actual == expected
    assert len(actual) == 12
    assert len(h.investigator.requests) == 12
    assert len(h.pg.started) == len(h.pg.stopped) == 12
    assert len({instance.directory for instance in h.pg.started}) == 12
    assert {instance.port for instance in h.pg.started} == {55497}


@pytest.mark.parametrize(
    ("normal_value", "fault_value", "method"),
    [
        (False, True, "Boolean"),
        (0, 100, "Float"),
        (0.0, 1.0, "Float"),
        ("CANARY-NORMAL", "CANARY-FAULT", "String"),
    ],
)
def test_flagd_method_is_derived_from_variant_value_type(
    h, capsys, normal_value, fault_value, method
):
    doc = json.loads(h.flags.current.raw)
    for flag, variant in s.VARIANTS.items():
        doc["flags"][flag]["variants"] = {"off": normal_value, variant: fault_value}
    h.flags.current = replace(h.flags.current, raw=s.encode(doc))
    code, _ = h.final(capsys)
    assert code == 0
    assert h.flagd.calls
    assert {call[1] for call in h.flagd.calls} == {method}


def test_readback_failure_after_successful_write_restores_without_investigation(
    h, capsys
):
    original_read = h.flags.read

    def after_write():
        h.flags.after_write = None

        def fail_once():
            h.flags.read = original_read
            raise lab.LabError("FLAG_READ_FAILED")

        h.flags.read = fail_once

    h.flags.after_write = after_write
    code, result = h.final(capsys)
    assert code == 1
    assert result["slots"][0]["status"] == "setup_failed"
    assert h.flags.current.raw == s.flags_raw()
    assert not h.investigator.requests
    assert not (h.deps.ledger_root / "fault-active.json").exists()


@pytest.mark.parametrize("scenario", [s.FAULT, s.NORMAL])
def test_preobservation_exact_300_second_freshness_boundary_is_allowed(
    h, capsys, scenario
):
    h.investigator.submission_delay = 300
    code, result = h.final(capsys, argv=h.argv(scenario))
    assert code == 0
    assert result["slots"][0]["status"] == "ran"


def test_environment_refusal_is_first_preflight_step_and_calls_no_other_seam(
    h, monkeypatch, capsys
):
    h.env.result = "ENV_UNCONFIRMED"

    def forbidden(*args, **kwargs):
        pytest.fail("environment refusal touched another dependency")

    for obj, method in [
        (h.clock, "now"),
        (h.clock, "monotonic"),
        (h.clock, "sleep"),
        (h.flags, "read"),
        (h.flags, "replace"),
        (h.flagd, "resolve"),
        (h.telemetry, "spans"),
        (h.telemetry, "checkout_calls"),
        (h.identity, "verify"),
        (h.ports, "in_use"),
        (h.pg, "start"),
        (h.pg, "stop"),
        (h.deps.balance, "balance"),
        (h.investigator, "investigate"),
        (h.provenance, "read"),
    ]:
        monkeypatch.setattr(obj, method, forbidden)
    code, result = h.final(capsys)
    assert (code, result["status"], result["reason"]) == (
        3,
        "refused",
        "ENV_UNCONFIRMED",
    )
    assert h.env.calls == 1
    assert not h.public().exists()
    assert_no_attempt(h)


def test_injected_dependencies_never_read_environment_file(h, monkeypatch, capsys):
    import builtins
    import io
    import os

    path = h.root / "CANARY-environment.env"
    path.write_text(s.SECRET)
    monkeypatch.setenv("M0_ENV_FILE", str(path))
    for obj, method in [(builtins, "open"), (io, "open"), (os, "open")]:
        original = getattr(obj, method)

        def guarded(file, *args, _original=original, **kwargs):
            if not isinstance(file, int) and os.fspath(file) in {
                str(path),
                bytes(path),
            }:
                pytest.fail("injected LiveDeps caused an environment-file read")
            return _original(file, *args, **kwargs)

        monkeypatch.setattr(obj, method, guarded)
    code, _ = h.final(capsys)
    assert code == 0
    assert h.env.calls == 1


@pytest.mark.parametrize("scenario", [s.FAULT, s.NORMAL])
def test_investigator_side_stale_before_run_creation_is_setup_failed(
    h, capsys, scenario
):
    h.investigator.before_creation_stale = True
    h.investigator.submission_delay = 301
    code, result = h.final(capsys, argv=h.argv(scenario))
    assert code == 1
    assert [slot["status"] for slot in result["slots"]] == ["setup_failed"]
    assert not h.investigator.created_runs
    assert not h.investigator.recordings
    summary = h.summaries()[0]
    assert summary["capture"]["attempt"]["product_run_id"] is None
    assert summary["capture"]["outcome"] is None
    lifecycle = json.loads((h.public() / "lifecycle.json").read_bytes())
    assert lifecycle["slots"][0]["reason"] == "PRE_OBSERVATION_STALE"
    assert h.flags.current.raw == s.flags_raw()
    assert h.pg.stopped == h.pg.started


@pytest.mark.parametrize(
    "invalid", ["synthetic", "missing", "hash", "binding", "outcome"]
)
def test_ran_requires_reloaded_nonsynthetic_bound_recording_with_same_outcome(
    h, capsys, invalid
):
    if invalid == "synthetic":
        h.investigator.synthetic = True
    elif invalid == "missing":
        h.investigator.omit_recording = True
    elif invalid == "binding":
        h.investigator.recording_transform = lambda doc: doc["context"].update(
            recording_id="CANARY-OTHER"
        )
    elif invalid == "outcome":
        h.investigator.result_transform = lambda result: replace(
            result,
            outcome=replace(
                result.outcome,
                final_state="completed",
                decision="report_available",
                report_available=True,
                handoff_reasons=(),
            ),
        )
    else:

        def corrupt(result):
            path, _ = h.investigator.recordings[-1]
            path.write_bytes(path.read_bytes() + b" ")
            return result

        h.investigator.result_transform = corrupt
    code, result = h.final(capsys)
    assert code == 1
    assert result["slots"][0]["status"] == "aborted"
    lifecycle = json.loads((h.public() / "lifecycle.json").read_bytes())
    reason = lifecycle["slots"][0]["reason"]
    if invalid in {"binding", "outcome"}:
        assert reason == "IDENTITY_MISMATCH"
    elif invalid == "missing":
        assert reason == "RECORDING_INCOMPLETE"
    elif invalid == "hash":
        assert reason == "HASH_MISMATCH"
    if invalid == "outcome":
        assert h.summaries()[0]["capture"]["outcome"] == json.loads(
            s.encode_outcome(h.investigator.results[0].outcome)
        )
    assert h.flags.current.raw == s.flags_raw()
    assert not (h.deps.ledger_root / "fault-active.json").exists()


def test_summary_evidence_is_derived_from_recording_and_investigator_fields_are_ignored(
    h, capsys
):
    from opspilot.evaluation import evidence_from_recording, load_recording
    from opspilot.evaluation.codec import encode

    h.investigator.with_evidence = True
    h.investigator.result_transform = lambda result: replace(
        result,
        delivered={"CANARY-FORGED": {"private": s.PRIVATE_ANSWER}},
        authorized={"CANARY-FORGED": {"private": s.SECRET}},
        delivery_records={"CANARY-FORGED": s.RAW_HTTP.encode()},
    )
    code, result = h.final(capsys)
    assert code == 0 and result["slots"][0]["status"] == "ran"
    path, ref = h.investigator.recordings[0]
    recording = load_recording(path, expected_hash=ref.sha256)
    assert recording.context.synthetic is False
    expected = evidence_from_recording(recording, outcome=recording.outcome)
    assert expected.delivered and any(
        item.delivered for item in expected.delivered.values()
    )
    assert expected.authorized
    capture = h.summaries()[0]["capture"]
    assert capture["delivered"] == encode(expected.delivered)
    assert capture["authorized"] == encode(expected.authorized)
    assert set(capture["delivery_refs"]) == set(expected.delivery_records)
    resolver = eval_live.attempt_resolver(
        s.ROUND,
        capture["attempt"]["attempt_id"],
        ledger_root=h.deps.ledger_root,
        recording_root=h.deps.recording_root,
    )
    from opspilot.evaluation import ArtifactRef

    for eid, raw in expected.delivery_records.items():
        assert resolver(ArtifactRef(**capture["delivery_refs"][eid])) == raw
    assert "CANARY-FORGED" not in json.dumps(capture)


def test_first_failure_reason_is_preserved_beside_restore_failure(h, capsys):
    h.investigator.error = "CANARY_FIRST_FAILURE"
    h.investigator.callback = lambda request: setattr(h.telemetry, "bad_wash", True)
    code, result = h.final(capsys, argv=h.argv(n=2))
    assert code == 4
    lifecycle = json.loads((h.public() / "lifecycle.json").read_bytes())
    slot = lifecycle["slots"][0]
    assert slot["reason"] == "CANARY_FIRST_FAILURE"
    assert slot["closure_reason"] == "RESTORE_FAILED:NORMAL_WINDOW_UNCONFIRMED"
    assert result["reason"] == slot["closure_reason"]
    assert lifecycle["reason"] == slot["closure_reason"]
    assert result["slots"][1]["status"] == "not_started"


def test_fault_guard_remains_held_during_investigation_restore_and_wash(h, capsys):
    checked = set()

    def check(stage):
        with (h.deps.ledger_root / ".fault.guard").open("a") as competing:
            try:
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                checked.add(stage)
            else:
                pytest.fail(f"fault guard was released during {stage}")

    h.flags.on_replace = lambda *args: check("restore" if h.flags.writes else "inject")
    h.investigator.callback = lambda request: check("investigation")
    normal_query = h.telemetry.checkout_calls

    def checkout(start, end, timeout):
        if len(h.flags.writes) >= 2:
            check("wash")
        return normal_query(start, end, timeout)

    h.telemetry.checkout_calls = checkout
    code, _ = h.final(capsys)
    assert code == 0
    assert checked == {"inject", "investigation", "restore", "wash"}
    with (h.deps.ledger_root / ".fault.guard").open("a") as released:
        fcntl.flock(released, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize(
    "state",
    ["verified", "unchanged-version", "wrong-uid", "wrong-content", "cas-conflict"],
)
def test_unknown_restore_result_is_decided_by_uid_sha_and_version_readback(
    h, capsys, state
):
    def on_replace(intent, current, new_raw, fence):
        if not h.flags.writes or new_raw != intent["original"].encode():
            return
        if state == "cas-conflict":
            raise lab.LabError("FLAG_CAS_CONFLICT")

        def unknown():
            h.flags.after_write = None
            if state == "unchanged-version":
                h.flags.current = replace(
                    h.flags.current, resource_version=current.resource_version
                )
            elif state == "wrong-uid":
                h.flags.current = replace(h.flags.current, uid="CANARY-FOREIGN-UID")
            elif state == "wrong-content":
                h.flags.current = replace(h.flags.current, raw=current.raw)
            raise lab.LabError("FLAG_WRITE_UNKNOWN")

        h.flags.after_write = unknown

    h.flags.on_replace = on_replace
    code, result = h.final(capsys, argv=h.argv(n=2))
    assert code == (0 if state == "verified" else 4)
    retained = h.deps.ledger_root / "fault-active.json"
    assert retained.exists() is (state != "verified")
    if state == "verified":
        assert all(slot["status"] == "ran" for slot in result["slots"])
        assert h.flags.current.raw == s.flags_raw()
    else:
        assert result["slots"][1]["status"] == "not_started"
        code = (
            "FLAG_CAS_CONFLICT"
            if state == "cas-conflict"
            else "FLAG_RESTORE_UNVERIFIED"
        )
        assert result["reason"] == f"RESTORE_FAILED:{code}"


def test_attempt_resolver_reads_only_its_private_recording_and_ledger(h, capsys):
    from opspilot.evaluation import ArtifactRef, load_recording

    code, _ = h.final(capsys)
    assert code == 0
    capture = h.summaries()[0]["capture"]
    resolver = eval_live.attempt_resolver(
        s.ROUND,
        capture["attempt"]["attempt_id"],
        ledger_root=h.deps.ledger_root,
        recording_root=h.deps.recording_root,
    )
    recording_ref = ArtifactRef(**capture["recording_ref"])
    assert s.sha(resolver(recording_ref)) == recording_ref.sha256
    recording = load_recording(
        h.investigator.recordings[0][0], expected_hash=recording_ref.sha256
    )
    for blob in recording.context.input_refs:
        assert s.sha(resolver(blob)) == blob.sha256
    ledger_ref = ArtifactRef(**capture["ledger_ref"])
    assert s.sha(resolver(ledger_ref)) == ledger_ref.sha256


@pytest.mark.parametrize(
    "refusal", ["env", "reservation", "fault", "identity", "pg-port"]
)
def test_preflight_refusal_starts_no_external_run_or_langsmith_project(
    h, monkeypatch, capsys, refusal
):
    # Guard the external SDK boundary as well as the socket/process guard:
    # construction of a real trace client is already outside injected deps.
    import sys
    from types import ModuleType

    langsmith = ModuleType("langsmith")

    def forbidden(*args, **kwargs):
        pytest.fail(
            "refused round attempted an external action or LangSmith project creation"
        )

    langsmith.Client = forbidden
    monkeypatch.setitem(sys.modules, "langsmith", langsmith)
    monkeypatch.setattr(h.pg, "start", forbidden)
    monkeypatch.setattr(h.deps.balance, "balance", forbidden)
    monkeypatch.setattr(h.investigator, "investigate", forbidden)
    if refusal == "env":
        h.env.result = "ENV_UNCONFIRMED"
    elif refusal == "reservation":
        h.reservation.unlink()
    elif refusal == "fault":
        (h.deps.ledger_root / "fault-active.json").write_bytes(
            b'{"owner":"CANARY-FOREIGN"}'
        )
    elif refusal == "identity":
        h.identity.result = ("CANARY-UNKNOWN-LAB",)
    else:
        h.ports.busy = True
    code, result = h.final(capsys)
    assert (code, result["status"]) == (3, "refused")
    assert_no_attempt(h)


def test_round_initialization_is_once_after_preflight_before_starts_and_journal(
    h, monkeypatch, capsys
):
    events = []
    starts = h.deps.ledger_root / "run-starts.jsonl"

    def no_started_round():
        assert not starts.exists() or starts.read_bytes() == b""
        assert not list(h.public().glob("runs/*/attempt.json"))

    for obj, method, label in [
        (h.env, "check", "env"),
        (h.identity, "verify", "identity"),
        (h.ports, "in_use", "port"),
        (h.flags, "read", "flags"),
        (h.flagd, "resolve", "flagd"),
    ]:
        original = getattr(obj, method)

        def observed(*args, _original=original, _label=label, **kwargs):
            events.append(_label)
            return _original(*args, **kwargs)

        monkeypatch.setattr(obj, method, observed)

    def provenance():
        no_started_round()
        assert {"env", "identity", "port", "flags", "flagd"} <= set(events)
        assert {call[0] for call in h.flagd.calls} == set(s.VARIANTS)
        assert not h.investigator.requests
        events.append("provenance")

    h.provenance.callback = provenance
    begun = []

    def begin(round_id):
        no_started_round()
        assert h.provenance.calls == 1
        assert events[-1] == "provenance"
        begun.append(round_id)
        events.append("begin")

    monkeypatch.setattr(h.investigator, "begin_round", begin, raising=False)

    def investigate(request):
        assert begun == [s.ROUND]
        assert h.provenance.calls == 1
        assert starts.exists()
        assert any(
            json.loads(line).get("round") == s.ROUND
            for line in starts.read_text().splitlines()
        )
        journal = load_attempt(
            h.public() / "runs" / request.attempt.attempt_id / "attempt.json"
        )
        assert journal.attempt.status == "unknown"
        events.append("investigate")

    h.investigator.callback = investigate
    code, result = h.final(capsys, argv=h.argv(n=2))
    assert code == 0
    assert [slot["status"] for slot in result["slots"]] == ["ran", "ran"]
    assert begun == [s.ROUND]
    assert h.provenance.calls == 1
    assert events.count("provenance") == events.count("begin") == 1
    assert (
        events.index("provenance") < events.index("begin") < events.index("investigate")
    )


def test_investigator_without_optional_begin_round_still_runs(h, capsys):
    assert not hasattr(h.investigator, "begin_round")
    code, result = h.final(capsys, argv=h.argv(n=2))
    assert code == 0
    assert all(slot["status"] == "ran" for slot in result["slots"])
    assert h.provenance.calls == 1
    assert len(h.investigator.requests) == 2


def test_begin_round_error_refuses_without_started_round_or_journal(
    h, monkeypatch, capsys
):
    calls = []

    def begin(round_id):
        calls.append(round_id)
        assert h.provenance.calls == 1
        raise lab.LabError("CANARY_BEGIN_FAILED")

    monkeypatch.setattr(h.investigator, "begin_round", begin, raising=False)
    code, result = h.final(capsys)
    assert (code, result["status"], result["reason"]) == (
        3,
        "refused",
        "CANARY_BEGIN_FAILED",
    )
    assert calls == [s.ROUND]
    assert h.provenance.calls == 1
    starts = h.deps.ledger_root / "run-starts.jsonl"
    assert not starts.exists() or starts.read_bytes() == b""
    assert_no_attempt(h)


def test_provenance_error_refuses_before_begin_round_or_journal(h, monkeypatch, capsys):
    def fail():
        raise lab.LabError("CANARY_PROVENANCE_FAILED")

    h.provenance.callback = fail

    def forbidden(round_id):
        pytest.fail("begin_round called after provenance failed")

    monkeypatch.setattr(h.investigator, "begin_round", forbidden, raising=False)
    code, result = h.final(capsys)
    assert (code, result["status"], result["reason"]) == (
        3,
        "refused",
        "CANARY_PROVENANCE_FAILED",
    )
    assert h.provenance.calls == 1
    starts = h.deps.ledger_root / "run-starts.jsonl"
    assert not starts.exists() or starts.read_bytes() == b""
    assert_no_attempt(h)


@pytest.mark.parametrize(
    "refusal", ["env", "reservation", "fault", "identity", "pg-port", "flags"]
)
def test_preflight_refusal_never_reads_provenance_or_begins_round(
    h, monkeypatch, capsys, refusal
):
    def forbidden(*args, **kwargs):
        pytest.fail("preflight-refused round entered provenance or begin_round")

    h.provenance.callback = forbidden
    monkeypatch.setattr(h.investigator, "begin_round", forbidden, raising=False)
    if refusal == "env":
        h.env.result = "ENV_UNCONFIRMED"
    elif refusal == "reservation":
        h.reservation.unlink()
    elif refusal == "fault":
        (h.deps.ledger_root / "fault-active.json").write_bytes(
            b'{"owner":"CANARY-FOREIGN"}'
        )
    elif refusal == "identity":
        h.identity.result = ("CANARY-UNKNOWN-LAB",)
    elif refusal == "pg-port":
        h.ports.busy = True
    else:
        doc = json.loads(h.flags.current.raw)
        doc["flags"]["cartFailure"]["defaultVariant"] = "on"
        h.flags.current = replace(h.flags.current, raw=s.encode(doc))
    code, result = h.final(capsys)
    assert (code, result["status"]) == (3, "refused")
    assert h.provenance.calls == 0
    assert_no_attempt(h)


def test_clean_provenance_freezes_exact_commit_in_round_manifest(h, capsys):
    commit = "1234567890abcdef" * 2 + "12345678"
    h.provenance.result = lab.Provenance(commit=commit, clean=True)
    code, result = h.final(capsys, argv=h.argv(n=2))
    assert code == 0
    assert all(slot["status"] == "ran" for slot in result["slots"])
    assert h.provenance.calls == 1
    for summary in h.summaries():
        assert summary["capture"]["manifest"]["dimensions"]["code"] == {
            "kind": "version",
            "value": commit,
            "reason": None,
        }
    for request in h.investigator.requests:
        dimension = request.manifest.dimensions["code"]
        assert (dimension.kind, dimension.value, dimension.reason) == (
            "version",
            commit,
            None,
        )


@pytest.mark.parametrize(
    ("commit", "clean"), [(None, True), ("b" * 40, False), (None, False)]
)
def test_unknown_or_dirty_provenance_does_not_refuse_round(
    h, monkeypatch, capsys, commit, clean
):
    h.provenance.result = lab.Provenance(commit=commit, clean=clean)
    begun = []
    monkeypatch.setattr(h.investigator, "begin_round", begun.append, raising=False)
    code, result = h.final(capsys)
    assert code == 0
    assert result["slots"][0]["status"] == "ran"
    assert h.provenance.calls == 1
    assert begun == [s.ROUND]
    dimension = h.summaries()[0]["capture"]["manifest"]["dimensions"]["code"]
    assert dimension["kind"] == "unknown"
    assert dimension["value"] is None
    assert isinstance(dimension["reason"], str) and dimension["reason"]
    aggregate = json.loads((h.public() / "aggregate.json").read_bytes())["aggregate"]
    assert aggregate["baseline_complete"] is False
    assert "MANIFEST_UNKNOWN" in aggregate["incomplete_reasons"]


def test_omitted_provenance_defaults_to_unknown_without_process_or_network(h, capsys):
    from dataclasses import fields

    assert (
        next(
            field for field in fields(lab.LiveDeps) if field.name == "provenance"
        ).default
        is None
    )
    h.deps = replace(h.deps, provenance=None)
    code, result = h.final(capsys)
    assert code == 0
    assert result["slots"][0]["status"] == "ran"
    assert h.provenance.calls == 0
    dimension = h.summaries()[0]["capture"]["manifest"]["dimensions"]["code"]
    assert dimension["kind"] == "unknown"
    assert dimension["value"] is None
    assert isinstance(dimension["reason"], str) and dimension["reason"]
