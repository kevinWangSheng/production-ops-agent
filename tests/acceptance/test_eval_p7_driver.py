"""Acceptance of the P7 driver, independently authored before implementation.

Sole contract: docs/tasks/2026-10-19-eval-p7-driver-interface.md (02eb1e2).
Section references on each test are the traceability map. No live services.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from decimal import Decimal

import pytest
from eval_p7_support import (
    ATTEMPT,
    ROUND,
    DriverHarness,
    put,
    sha,
    snapshot,
)

from opspilot.evaluation import judge
from opspilot.evaluation.codec import canonical_bytes
from opspilot.evaluation.judge import safe_input
from scripts import eval_p7


@pytest.fixture
def harness(tmp_path, monkeypatch, capsys):
    return DriverHarness(tmp_path, monkeypatch, capsys, eval_p7).init()


def test_init_private_modes_and_existing_output(harness):
    """§§0, 1.2–1.3: positive init, private modes, no overwrite."""
    h = harness
    assert json.loads((h.ws / "workspace.json").read_bytes()) == {
        "schema": "opspilot-p7-workspace-1",
        "workspace": h.workspace,
    }
    assert h.ws.stat().st_mode & 0o777 == 0o700
    for name in ("exposure.lock", "exposure.jsonl"):
        assert (h.private / name).stat().st_mode & 0o777 == 0o600
    assert (h.ws / "workspace.json").stat().st_mode & 0o777 == 0o600
    before = snapshot(h.root)
    h.cli("init", code=3, reason="OUTPUT_EXISTS")
    assert snapshot(h.root) == before


@pytest.mark.parametrize(
    "command,args",
    [
        ("init", []),
        ("debug", ["--session", "../canary", "--judge-env", "/CANARY"]),
        (
            "formal",
            ["--run", "../canary", "--pack-hash", "1" * 64, "--judge-env", "/CANARY"],
        ),
        ("import", ["--round", "eval-p7-../canary", "--attempt", ATTEMPT]),
        (
            "import",
            [
                "--round",
                ROUND,
                "--attempt",
                ATTEMPT.upper().replace("000000000001", "AAAAAAAAAAAA"),
            ],
        ),
        (
            "balance",
            [
                "--session",
                "canary",
                "--provider",
                "../canary",
                "--phase",
                "before",
                "--unknown",
            ],
        ),
        (
            "debug",
            ["--session", "canary", "--item", "../canary", "--judge-env", "/CANARY"],
        ),
        (
            "import-annotation",
            [
                "--slot",
                "3",
                "--response",
                "/CANARY",
                "--annotator-identity",
                "CANARY",
                "--model-identity",
                "CANARY",
            ],
        ),
    ],
)
def test_ids_rejected_before_any_path_work(harness, command, args):
    """§0: invalid IDs are USAGE even when referenced files do not exist."""
    h = harness
    before = snapshot(h.root)
    h.cli(
        command,
        *args,
        workspace="../canary" if command == "init" else None,
        code=2,
        reason="USAGE",
    )
    assert snapshot(h.root) == before
    assert not h.calls and not h.balance_calls


@pytest.mark.parametrize(
    "rule",
    [
        "",
        "tmp/eval-p7/**\n!tmp/eval-p7/canary-second/\n",
        "tmp/eval-p7/**\n!tmp/eval-p7/canary-second/\ntmp/eval-p7/canary-second/**\n!tmp/eval-p7/canary-second/workspace.json\n",
    ],
)
def test_git_ignore_is_required_for_each_target(harness, rule):
    """§1.2: real temporary git repository; unignored target is refused."""
    h = harness
    (h.root / ".gitignore").write_text(rule)
    before = snapshot(h.root)
    h.cli("init", workspace="canary-second", code=3, reason="PRIVATE_PATH_NOT_IGNORED")
    assert snapshot(h.root) == before


def test_git_negation_of_one_output_is_respected(harness):
    """§1.2: a single packet file exception defeats parent ignore."""
    h = harness.seed()
    (h.root / ".gitignore").write_text(
        "tmp/eval-p7/**\n!tmp/eval-p7/canary-workspace/\n"
        "!tmp/eval-p7/canary-workspace/blind/\n"
        "tmp/eval-p7/canary-workspace/blind/**\n"
        "!tmp/eval-p7/canary-workspace/blind/packet.json\n"
    )
    convention = h.root / "convention.txt"
    convention.write_text(judge.PROMPT)
    # Prove the test's Git premise independently of the driver.
    target = h.ws / "blind/packet.json"
    result = subprocess.run(["git", "check-ignore", "-q", "--", str(target)])
    assert result.returncode == 1
    before = snapshot(h.root)
    h.cli(
        "export-blind",
        "--convention",
        convention,
        code=3,
        reason="PRIVATE_PATH_NOT_IGNORED",
    )
    assert snapshot(h.root) == before


@pytest.mark.parametrize("inside", [False, True])
def test_symlink_components_refused_even_inside_repo(harness, tmp_path, inside):
    """§1.2.1: symlinks are refused even when their destination is in repo."""
    h = harness
    target = (
        h.root / "canary-target"
        if inside
        else tmp_path.parent / (tmp_path.name + "-outside")
    )
    target.mkdir()
    (h.ws / "costs").symlink_to(target, target_is_directory=True)
    before = snapshot(h.root)
    h.cli(
        "balance",
        "--session",
        "canary",
        "--provider",
        "zai",
        "--phase",
        "before",
        "--unknown",
        code=3,
        reason="PRIVATE_PATH_OUTSIDE_REPO",
    )
    assert snapshot(h.root) == before
    assert not list(target.iterdir())


def test_packet_whitelist_prompt_and_identical_copies(harness):
    """§§2, 6.1: only declared packet fields; no provenance, intent or labels."""
    h = harness.seed().export()
    raw = (h.ws / "blind/packet.json").read_bytes()
    packet = json.loads(raw)
    assert sha(raw) == h.packet_hash
    assert set(packet) == {
        "schema",
        "items_sha256",
        "judge_prompt",
        "judge_prompt_sha256",
        "convention",
        "convention_sha256",
        "output_schema",
        "items",
    }
    assert packet["schema"] == "opspilot-p7-blind-packet-1"
    assert packet["judge_prompt"] == judge.PROMPT
    assert packet["convention"] in judge.PROMPT
    assert packet["judge_prompt_sha256"] == sha(judge.PROMPT.encode())
    assert packet["convention_sha256"] == sha(packet["convention"].encode())
    assert packet["items_sha256"] == sha((h.ws / "assembled/items.json").read_bytes())
    assert [i["item_id"] for i in packet["items"]] == sorted(i.item_id for i in h.items)
    for source, item in zip(h.items, packet["items"], strict=True):
        assert set(item) == {"item_id", "input_hash", "p6_input"}
        assert canonical_bytes(item["p6_input"]) == canonical_bytes(
            safe_input(source.request)
        )
        assert set(item["p6_input"]) == {
            "question",
            "subject_id",
            "authorized_windows",
            "report_raw",
            "cited_evidence",
            "expected_output",
            "scenario_kind",
            "injected",
            "signature_matches",
        }
    for slot in (1, 2):
        assert (h.ws / f"blind/annotator-{slot}/packet.json").read_bytes() == raw
    for record in (h.ws / "items").glob("*.json"):
        value = json.loads(record.read_bytes())
        for forbidden in (
            value["source_family"],
            value["provenance"]["round_id"],
            value["provenance"]["attempt_id"],
            value["provenance"]["product_run_id"],
            value["coverage_note"],
        ):
            assert forbidden not in raw.decode()
    assert "CANARY-P7-LABEL-PRIVATE" not in raw.decode()
    h.workspace = "canary-second"
    h.init().seed().export()
    assert (h.ws / "blind/packet.json").read_bytes() == raw
    before = snapshot(h.root)
    h.cli("export-blind", "--convention", h.convention, code=3, reason="OUTPUT_EXISTS")
    assert snapshot(h.root) == before


def test_convention_must_be_literal_in_judge_prompt(harness):
    """§6.1: absent convention rejected without outputs."""
    h = harness.seed()
    path = h.root / "convention.txt"
    path.write_text("CANARY-P7-CONVENTION-NOT-IN-PROMPT")
    before = snapshot(h.root)
    h.cli("export-blind", "--convention", path, code=3, reason="CONVENTION_MISMATCH")
    assert snapshot(h.root) == before


def test_approved_annotations_have_hash_bound_provenance(harness):
    """§6.2: both approved slots accepted; hashes bind exact private bytes."""
    h = harness.seed().export().annotations()
    for slot, model in ((1, "claude-opus-5-5"), (2, "gpt-6-astra")):
        annotation = h.ws / f"annotations/annotations-{slot}.json"
        provenance = json.loads(
            (h.ws / f"annotations/annotations-{slot}.provenance.json").read_bytes()
        )
        assert provenance["packet_sha256"] == h.packet_hash
        assert provenance["annotation_sha256"] == sha(annotation.read_bytes())
        assert provenance["response_sha256"] == sha(h.response().read_bytes())
        assert provenance["slot"] == slot
        assert provenance["model_identity"] == model


@pytest.mark.parametrize(
    "slot,model,reason",
    [
        (1, "gpt-6-astra", "ANNOTATOR_NOT_APPROVED"),
        (2, "claude-opus-5-5", "ANNOTATOR_NOT_APPROVED"),
    ],
)
def test_wrong_slot_model_is_refused(harness, slot, model, reason):
    """§6.2.3: approval is exact and slot-specific."""
    h = harness.seed().export()
    response = h.response()
    before = snapshot(h.root)
    h.cli(
        "import-annotation",
        "--slot",
        slot,
        "--response",
        response,
        "--annotator-identity",
        "CANARY-ANNOTATOR",
        "--model-identity",
        model,
        code=3,
        reason=reason,
    )
    assert snapshot(h.root) == before


def test_candidate_annotator_identity_is_refused(harness):
    """§6.2.3: candidate identity cannot supply independent annotation."""
    h = harness.seed().export()
    response = h.response()
    before = snapshot(h.root)
    result = h.cli(
        "import-annotation",
        "--slot",
        "1",
        "--response",
        response,
        "--annotator-identity",
        "CANARY-ANNOTATOR",
        "--model-identity",
        "CANARY-glm53-judge",
        code=3,
    )
    assert result["reason"] in {"CALIBRATION_UNRESOLVED", "ANNOTATOR_NOT_APPROVED"}
    assert snapshot(h.root) == before


def test_annotation_packet_stale_is_refused(harness):
    """§6.2.1: responses are bound to the exact packet bytes."""
    h = harness.seed().export()
    response = h.response(packet_hash="0" * 64)
    before = snapshot(h.root)
    h.cli(
        "import-annotation",
        "--slot",
        "1",
        "--response",
        response,
        "--annotator-identity",
        "CANARY-ANNOTATOR",
        "--model-identity",
        "claude-opus-5-5",
        code=3,
        reason="PACKET_STALE",
    )
    assert snapshot(h.root) == before


def test_debug_requests_only_debug_inputs_and_no_calibration(harness):
    """§§8, 10: all requests are debug projections; never certify debug."""
    h = harness.seed().export().debug()
    expected = {
        canonical_bytes(safe_input(i.request)) for i in h.items if i.split == "debug"
    }
    forbidden = {
        canonical_bytes(safe_input(i.request))
        for i in h.items
        if i.split == "validation"
    }
    actual = [canonical_bytes(payload) for payload in h.calls]
    assert len(actual) == 12 * 3
    assert set(actual) == expected
    assert not (set(actual) & forbidden)
    assert not list(h.ws.glob("calibration*"))
    records = list((h.ws / "debug/canary-debug").glob("*.json"))
    assert len(records) == 36
    assert all(
        json.loads(p.read_bytes())["observation"]["passed"] is False for p in records
    )
    assert len(h.balance_calls) == 2


@pytest.mark.parametrize("unknown", [False, True])
def test_debug_validation_or_unknown_item_has_zero_calls(harness, unknown):
    """§8.1: an invalid selection cannot expose even one validation input."""
    h = harness.seed().export()
    h.costs("canary-debug")
    item_id = (
        "p7-" + "f" * 24
        if unknown
        else next(i.item_id for i in h.items if i.split == "validation")
    )
    before = snapshot(h.root)
    h.cli(
        "debug",
        "--session",
        "canary-debug",
        "--judge-env",
        h.env,
        "--item",
        item_id,
        code=3,
        reason="SCHEMA_INVALID" if unknown else "VALIDATION_EXPOSURE_FORBIDDEN",
    )
    assert snapshot(h.root) == before
    assert not h.calls and not h.balance_calls


def test_freeze_one_positive_three_negative_per_scenario(harness):
    """§9.1: observed 1+3 distribution is accepted and pack reads back."""
    h = harness.seed().export().annotations().freeze()
    assert h.pack_hash
    assert len(h.pack.items) == 36
    assert not h.calls


def test_freeze_mismatch_requires_explicit_acceptance(harness):
    """§9.1: freeze reports distribution mismatch without writing pack."""
    h = harness.seed(balanced=False).export().annotations()
    before = snapshot(h.root)
    result = h.cli("freeze", code=3, reason="LABEL_DISTRIBUTION_MISMATCH")
    assert result["adjudications_sha256"] == sha(
        (h.ws / "adjudication/adjudications.json").read_bytes()
    )
    assert snapshot(h.root) == before


def test_freeze_explicit_acceptance_binds_current_distribution(harness):
    """§9.1: user's brief+adjudications+distribution bound acceptance permits freeze."""
    h = harness.seed(balanced=False).export().annotations()
    _, value = h.acceptance()
    h.freeze("--accept-distribution", value["brief_sha256"])
    summary = h.cli("summary")
    assert value["brief_sha256"] in json.dumps(summary)
    assert value["adjudications_sha256"] in json.dumps(summary)


@pytest.mark.parametrize(
    "damage",
    ["missing", "argument", "brief", "adjudications", "distribution", "empty-decision"],
)
def test_distribution_acceptance_cannot_be_reused_after_changes(harness, damage):
    """§9.1: every acceptance binding is checked independently."""
    h = harness.seed(balanced=False).export().annotations()
    path, value = h.acceptance()
    argument = value["brief_sha256"]
    if damage == "missing":
        path.unlink()
    elif damage == "argument":
        argument = "0" * 64
    elif damage == "brief":
        value["brief_sha256"] = "0" * 64
    elif damage == "adjudications":
        # Semantically equal, different bytes: acceptance binds the raw file.
        target = h.ws / "adjudication/adjudications.json"
        target.write_bytes(target.read_bytes() + b"\n")
    elif damage == "distribution":
        next(iter(value["distribution"].values()))["positive"] += 1
    else:
        value["decision_record"] = "  "
    if damage != "missing":
        put(path, value)
    before = snapshot(h.root)
    h.cli(
        "freeze",
        "--accept-distribution",
        argument,
        code=3,
        reason="ACCEPTANCE_MISMATCH",
    )
    assert snapshot(h.root) == before


def test_distribution_acceptance_is_usage_when_not_needed(harness):
    """§9.1: an already conforming pack cannot be pre-emptively exempted."""
    h = harness.seed().export().annotations()
    _, value = h.acceptance()
    before = snapshot(h.root)
    h.cli(
        "freeze", "--accept-distribution", value["brief_sha256"], code=2, reason="USAGE"
    )
    assert snapshot(h.root) == before


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("pack-hash", "HASH_MISMATCH"),
        ("debug", "DEBUG_PRECHECK_MISSING"),
        ("debug-config", "DEBUG_PRECHECK_MISSING"),
        ("prompt", "JUDGE_PROMPT_CHANGED"),
        ("assembled", "PACKET_STALE"),
        ("provenance", "PACKET_STALE"),
        ("manual-cost", "COST_BEFORE_MISSING"),
        ("deepseek-cost", "OUTPUT_EXISTS"),
        ("destination", "OUTPUT_EXISTS"),
        ("ignored-descendant", "PRIVATE_PATH_NOT_IGNORED"),
    ],
)
def test_formal_preflight_failures_leave_every_artifact_unchanged(
    harness, damage, reason
):
    """§9.2 steps 1,3–6: refusal=3, zero calls/writes, registry unchanged."""
    h = harness.ready()
    if damage == "pack-hash":
        h.pack_hash = "0" * 64
    elif damage == "debug":
        for path in (h.ws / "debug").rglob("*.json"):
            path.unlink()
    elif damage == "debug-config":
        for path in (h.ws / "debug").rglob("openai-*.json"):
            value = json.loads(path.read_bytes())
            value["config_hash"] = "0" * 64
            put(path, value)
    elif damage == "prompt":
        # A packet from an older convention must be refused with the current
        # valid configs. Changing PROMPT at runtime would invalidate configs
        # before this check and test a different boundary.
        path = h.ws / "blind/packet.json"
        packet = json.loads(path.read_bytes())
        packet["convention"] = "CANARY-P7-OLD-CONVENTION"
        packet["convention_sha256"] = sha(packet["convention"].encode())
        put(path, packet)
        for slot in (1, 2):
            provenance = h.ws / f"annotations/annotations-{slot}.provenance.json"
            value = json.loads(provenance.read_bytes())
            value["packet_sha256"] = sha(path.read_bytes())
            put(provenance, value)
    elif damage == "assembled":
        path = h.ws / "assembled/items.json"
        path.write_bytes(path.read_bytes() + b"\n")
    elif damage == "provenance":
        path = h.ws / "annotations/annotations-2.provenance.json"
        value = json.loads(path.read_bytes())
        value["packet_sha256"] = "0" * 64
        put(path, value)
    elif damage == "manual-cost":
        (h.ws / "costs/canary-formal/openai-before.json").unlink()
    elif damage == "deepseek-cost":
        put(h.ws / "costs/canary-formal/deepseek-before.json", {})
    elif damage == "destination":
        (h.ws / "calibration/canary-formal").mkdir(parents=True)
    else:
        (h.root / ".gitignore").write_text(
            "tmp/eval-p7/**\n!tmp/eval-p7/canary-workspace/\n"
            "!tmp/eval-p7/canary-workspace/calibration/\n"
            "tmp/eval-p7/canary-workspace/calibration/**\n"
            "!tmp/eval-p7/canary-workspace/calibration/canary-formal/\n"
            "tmp/eval-p7/canary-workspace/calibration/canary-formal/**\n"
            "!tmp/eval-p7/canary-workspace/calibration/canary-formal/openai-060.json\n"
        )
        target = h.ws / "calibration/canary-formal/openai-060.json"
        result = subprocess.run(
            ["git", "-C", str(h.root), "check-ignore", "-q", "--", str(target)]
        )
        assert result.returncode == 1
    before = snapshot(h.root)
    exposure = (h.private / "exposure.jsonl").read_bytes()
    h.formal(code=3, reason=reason)
    assert snapshot(h.root) == before
    assert (h.private / "exposure.jsonl").read_bytes() == exposure
    assert not h.calls and not h.balance_calls


def test_formal_registers_before_balance_and_sends_after_before_record(harness):
    """§§9.2.7,10: registry first, automatic DeepSeek before before any send."""
    h = harness.ready()
    registry = h.private / "exposure.jsonl"
    cost = h.ws / "costs/canary-formal"

    def at_balance():
        rows = [json.loads(line) for line in registry.read_bytes().splitlines()]
        assert len(rows) == 1
        assert rows[0]["workspace"] == h.workspace
        assert rows[0]["run_id"] == "canary-formal"
        assert rows[0]["pack_hash"] == h.pack_hash
        assert set(rows[0]["validation_families"]) == {
            i.source_family for i in h.items if i.split == "validation"
        }
        if not h.calls:
            assert not (cost / "deepseek-before.json").exists()
        else:
            assert len(h.calls) == 180
            assert (cost / "deepseek-before.json").is_file()

    def at_send(_payload):
        assert registry.read_bytes()
        assert (cost / "deepseek-before.json").is_file()
        assert not (cost / "deepseek-after.json").exists()

    h.balance_observer = at_balance
    h.observer = at_send
    h.formal()
    assert len(h.calls) == 180
    assert len(h.balance_calls) == 2
    for phase in ("before", "after"):
        value = json.loads((cost / f"deepseek-{phase}.json").read_bytes())
        assert value["source"] == "deepseek_balance_api"
        assert value["shared_account_possible"] is True
        assert Decimal(value["amount"]) == Decimal("100.00")
    assert len(registry.read_bytes().splitlines()) == 1


@pytest.mark.parametrize("phase", ["before", "after"])
def test_deepseek_balance_cli_has_no_manual_writer(harness, phase):
    """§10: only debug/formal may create DeepSeek balance records."""
    h = harness
    before = snapshot(h.root)
    h.cli(
        "balance",
        "--session",
        "canary",
        "--provider",
        "deepseek",
        "--phase",
        phase,
        "--amount",
        "1.00",
        "--currency",
        "CNY",
        code=2,
        reason="USAGE",
    )
    assert snapshot(h.root) == before
    assert not h.balance_calls


@pytest.mark.parametrize("artifact", ["registry", "partial-pack"])
def test_exposure_in_another_workspace_blocks_formal(harness, artifact):
    """§9.2.7: shared root catches other workspace registration or partial runs."""
    h = harness.ready()
    if artifact == "registry":
        # Let the first public run write its own registry schema. The next
        # workspace shares sources, but has fresh debug/cost/run artifacts.
        h.formal()
        from shutil import rmtree

        rmtree(h.ws / "calibration")  # discard only this test's fake outputs
        h.workspace = "canary-other"
        h.init().ready()
    else:
        other = h.private / "canary-other"
        put(
            other / "workspace.json",
            dict(schema="opspilot-p7-workspace-1", workspace="canary-other"),
        )
        put(
            other / "calibration/canary-interrupted/label-pack/items.json",
            {"items": h.pack.items},
        )
    before = snapshot(h.root)
    h.formal(code=3, reason="VALIDATION_EXPOSED")
    assert snapshot(h.root) == before
    assert not h.calls and not h.balance_calls


def test_import_verified_live_capture_and_duplicate_source(harness):
    """§3: valid live source imports; an already imported attempt is refused."""
    from eval_p7_support import live_source

    h = harness
    _, _, _, capture, args = live_source(h)
    result = h.cli("import", *args)
    item_id = result["item_id"]
    value = json.loads((h.ws / "items" / f"{item_id}.json").read_bytes())
    assert value["kind"] == "original"
    assert value["rewrite"] is None
    assert value["provenance"]["attempt_id"] == capture.attempt.attempt_id
    assert value["source_family"] == f"live/{ROUND}/{capture.attempt.attempt_id}"
    assert item_id == "p7-" + result["input_hash"][:24]
    before = snapshot(h.root)
    h.cli("import", *args, code=3, reason="SOURCE_ALREADY_IMPORTED")
    assert snapshot(h.root) == before


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("mode", "SOURCE_NOT_LIVE"),
        ("status", "SOURCE_NOT_RAN"),
        ("report", "SOURCE_INCOMPLETE"),
        ("outcome", "SOURCE_INCOMPLETE"),
        ("recording", "SOURCE_INCOMPLETE"),
        ("judged", "SOURCE_ALREADY_JUDGED"),
    ],
)
def test_import_rejects_ineligible_source_without_writes(harness, damage, reason):
    """§3 steps 2–3: public storage-valid captures still must satisfy P7 eligibility."""
    from eval_p7_support import live_source, replace_source

    from opspilot.evaluation import (
        DimensionValue,
        JudgeResult,
        load_summary,
        manifest_hash,
    )
    from scripts import eval_live

    h = harness
    source, path, aggregate, capture, args = live_source(h)
    if damage == "mode":
        capture = replace(capture, attempt=replace(capture.attempt, mode="replay"))
    elif damage == "status":
        capture = replace(capture, attempt=replace(capture.attempt, status="aborted"))
    elif damage == "recording":
        capture = replace(capture, recording_ref=None)
    elif damage in {"report", "outcome"}:
        capture = replace(capture, **{damage: None})
    else:
        digest = "a" * 64
        manifest = replace(
            capture.manifest,
            dimensions={
                **capture.manifest.dimensions,
                "judge_config": DimensionValue("hash", digest, None),
                "calibration_pack": DimensionValue("hash", digest, None),
            },
        )
        capture = replace(
            capture,
            manifest=manifest,
            attempt=replace(capture.attempt, manifest_hash=manifest_hash(manifest)),
            judge=JudgeResult("ok", True, (), True, digest, digest),
        )
    replace_source(path, aggregate, capture)
    # Prove lower-level storage accepts the fixture, rather than allowing an
    # incidental schema/cache failure to masquerade as the driver's refusal.
    load_summary(
        path,
        manifest=capture.manifest,
        resolve=eval_live.attempt_resolver(
            ROUND,
            capture.attempt.attempt_id,
            ledger_root=source.deps.ledger_root,
            recording_root=source.deps.recording_root,
        ),
    )
    before = snapshot(h.root)
    h.cli("import", *args, code=3, reason=reason)
    assert snapshot(h.root) == before
    assert not h.calls


@pytest.mark.parametrize("damage", ["prefix", "unignored", "tracked", "outside"])
def test_import_source_gate_precedes_reading_files(harness, damage, tmp_path):
    """§3 step 0: round/ignore checks happen before missing source file reads."""
    h = harness
    source = h.root / "private-source"
    source.mkdir()
    round_id = ROUND
    reason = "PRIVATE_PATH_NOT_IGNORED"
    if damage == "prefix":
        round_id = "canary-old-public-round"
        reason = "USAGE"
    elif damage == "unignored":
        (h.root / ".gitignore").write_text("tmp/\n")
    elif damage == "tracked":
        path = source / ROUND / "runs" / ATTEMPT / "summary.json"
        put(path, {"CANARY": "not a valid summary; must never be parsed"})
        subprocess.run(["git", "add", "-f", str(path)], check=True)
    else:
        source = tmp_path.parent / (tmp_path.name + "-external-source")
        source.mkdir()
        reason = "PRIVATE_PATH_UNVERIFIED"
    before = snapshot(h.root)
    h.cli(
        "import",
        "--round",
        round_id,
        "--attempt",
        ATTEMPT,
        "--evidence-root",
        source,
        code=2 if damage == "prefix" else 3,
        reason=reason,
    )
    assert snapshot(h.root) == before


def assert_p7_error(code, call):
    """§0: lower errors keep only their fixed code and no exception chain."""
    with pytest.raises(eval_p7.P7Error) as caught:
        call()
    assert isinstance(caught.value, ValueError)
    assert caught.value.code == code
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "decision,reason",
    [(False, "PRIVATE_PATH_NOT_IGNORED"), (None, "PRIVATE_PATH_UNVERIFIED")],
)
def test_library_ignore_check_injection_and_error_boundary(harness, decision, reason):
    """§§0,1.2: documented tri-state ignore callback maps to fixed P7Error."""
    from copy import copy

    h = harness
    ws = copy(h.library_workspace)
    # Preserve every opaque Workspace field while injecting the one documented
    # extension point. No constructor or path-field names are invented.
    object.__setattr__(ws, "ignore_check", lambda _path: decision)
    before = snapshot(h.root)
    assert_p7_error(
        reason,
        lambda: eval_p7.record_balance(
            ws,
            session_id="canary-library",
            provider="zai",
            phase="before",
            amount=None,
            currency=None,
            source="unknown",
        ),
    )
    assert snapshot(h.root) == before


def test_library_public_round_guard_precedes_source_reads(harness):
    """§3.0: library guard uses SOURCE_ROUND_INVALID, not CLI's USAGE."""
    h = harness
    roots = eval_p7.SourceRoots(
        h.root / "missing-evidence",
        h.root / "missing-ledger",
        h.root / "missing-recording",
    )
    before = snapshot(h.root)
    assert_p7_error(
        "SOURCE_ROUND_INVALID",
        lambda: eval_p7.import_capture(
            h.library_workspace,
            round_id="canary-public",
            attempt_id=ATTEMPT,
            roots=roots,
        ),
    )
    assert snapshot(h.root) == before


@pytest.mark.parametrize(
    "mapping,damage",
    [
        ("configs", "missing"),
        ("configs", "extra"),
        ("transports", "missing"),
        ("transports", "extra"),
    ],
)
def test_formal_library_key_sets_rejected_before_exposure(harness, mapping, damage):
    """§9.2.2: exact configs/transports key sets checked before registration."""
    from opspilot.evaluation.judge import HTTPJudgeTransport

    h = harness.ready()
    configs = dict(h.configs)
    transports = {p: HTTPJudgeTransport() for p in h.configs}
    target = configs if mapping == "configs" else transports
    if damage == "missing":
        target.pop("deepseek")
    else:
        target["CANARY-UNKNOWN-PROVIDER"] = next(iter(target.values()))
    before = snapshot(h.root)
    assert_p7_error(
        "CONFIG_INVALID",
        lambda: eval_p7.formal(
            h.library_workspace,
            run_id="canary-formal",
            pack_hash=h.pack_hash,
            configs=configs,
            transports=transports,
            balance=lambda _provider: Decimal("100.00"),
        ),
    )
    assert snapshot(h.root) == before
    assert not h.calls and not h.balance_calls


def test_formal_bad_cli_config_refuses_without_exposure(harness):
    """§9.2.2: CLI config failure exits 3 without occupying validation families."""
    h = harness.ready()
    raw = h.env.read_text().replace(
        "https://api.z.ai/api/paas/v4", "https://CANARY.invalid"
    )
    h.env.write_text(raw)
    before = snapshot(h.root)
    h.formal(code=3, reason="CONFIG_INVALID")
    assert snapshot(h.root) == before
    assert not h.calls and not h.balance_calls


def test_formal_in_linked_worktree_uses_main_private_root(harness):
    """§§1.1,9.2.7: changing Git worktree cannot bypass previous exposure."""
    h = harness.ready()
    h.formal()
    h.workspace = "canary-linked"
    h.init().ready()
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CANARY",
            "-c",
            "user.email=canary@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "CANARY fixture",
        ],
        check=True,
    )
    linked = h.root / "canary-linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked), "HEAD"],
        check=True,
        capture_output=True,
    )
    h.mp.chdir(linked)
    before = snapshot(h.root)
    h.formal(code=3, reason="VALIDATION_EXPOSED")
    assert snapshot(h.root) == before
    assert not (linked / "tmp/eval-p7").exists()
    assert not h.calls and not h.balance_calls


def test_library_ignored_balance_write_is_exclusive(harness):
    """§§0,1.2,10: approved ignored write succeeds; repeated write keeps bytes."""
    from copy import copy

    h = harness
    ws = copy(h.library_workspace)
    object.__setattr__(ws, "ignore_check", lambda _path: True)
    kwargs = dict(
        session_id="canary-library",
        provider="zai",
        phase="before",
        amount="12.00",
        currency="CNY",
        source="manual",
    )
    eval_p7.record_balance(ws, **kwargs)
    path = h.ws / "costs/canary-library/zai-before.json"
    value = json.loads(path.read_bytes())
    assert Decimal(value["amount"]) == Decimal("12.00")
    assert value["source"] == "manual"
    assert path.stat().st_mode & 0o777 == 0o600
    before = snapshot(h.root)
    assert_p7_error("OUTPUT_EXISTS", lambda: eval_p7.record_balance(ws, **kwargs))
    assert snapshot(h.root) == before


def test_debug_existing_deepseek_before_is_refused_without_calls(harness):
    """§§8.3,10: automatic DeepSeek balance is single-use for a session."""
    h = harness.seed().export().debug()
    h.calls.clear()
    h.balance_calls.clear()
    before = snapshot(h.root)
    h.cli(
        "debug",
        "--session",
        "canary-debug",
        "--judge-env",
        h.env,
        code=3,
        reason="OUTPUT_EXISTS",
    )
    assert snapshot(h.root) == before
    assert not h.calls and not h.balance_calls


def test_summary_stdout_keeps_report_labels_decisions_and_credentials_private(harness):
    """§§0,7,11: exercise real private adjudication text, then safe public summary."""
    from eval_p7_support import CANARY_DECISION, CANARY_LABEL

    h = harness.seed().export().annotations(disagree=True).freeze()
    assert CANARY_DECISION in (h.ws / "adjudication/adjudications.json").read_text()
    assert CANARY_LABEL in (h.ws / "annotations/annotations-1.json").read_text()
    h.cli("summary")  # shared CLI assertion checks every private CANARY


@pytest.mark.parametrize("missing", ["exposure.lock", "exposure.jsonl"])
def test_formal_missing_registry_is_refused_without_recreating_it(harness, missing):
    """§9.2.7: missing shared registry refuses without calls or side effects."""
    h = harness.ready()
    (h.private / missing).unlink()
    before = snapshot(h.root)
    h.formal(code=3, reason="EXPOSURE_REGISTRY_MISSING")
    assert snapshot(h.root) == before
    assert not h.calls and not h.balance_calls


def test_library_preserves_lower_storage_hash_error_without_chain(harness):
    """§§0,3.2: a lower StorageError's code crosses the public P7 boundary."""
    from eval_p7_support import live_source

    h = harness
    source, path, _, capture, _ = live_source(h)
    value = json.loads(path.read_bytes())
    value["report_sha256"] = "0" * 64
    put(path, value)
    roots = eval_p7.SourceRoots(
        source.deps.evidence_root, source.deps.ledger_root, source.deps.recording_root
    )
    before = snapshot(h.root)
    assert_p7_error(
        "HASH_MISMATCH",
        lambda: eval_p7.import_capture(
            h.library_workspace,
            round_id=ROUND,
            attempt_id=capture.attempt.attempt_id,
            roots=roots,
        ),
    )
    assert snapshot(h.root) == before


def test_snapshot_reads_canary_without_disabling_runtime_env_guard(harness):
    """Fixture regression: snapshots include .env while runtime reads stay blocked."""
    import builtins

    h = harness
    state = snapshot(h.root)
    assert state[".env"][1] == ("file", b"CANARY-P7-DOTENV-MUST-NOT-BE-READ=invalid\n")
    with pytest.raises(AssertionError):
        (h.root / ".env").read_bytes()
    with pytest.raises(AssertionError):
        builtins.open(h.root / ".env", "rb")
    assert snapshot(h.root) == state


@pytest.mark.parametrize(
    "debug_distribution", ["all-positive", "all-negative", "compensating"]
)
@pytest.mark.parametrize("accept_deviation", [False, True])
def test_freeze_debug_distribution_cannot_be_waived(
    harness, debug_distribution, accept_deviation
):
    """§9.1 (02eb1e2): each scenario needs debug 1+1; acceptance never waives it."""
    from eval_p7_support import REPO

    from opspilot.evaluation import load_scenarios

    h = harness
    scenarios = [
        spec.scenario_id for spec, _ in load_scenarios(REPO / "evals/scenarios")
    ]
    counts = {scenarios[0]: 2 if debug_distribution != "all-negative" else 0}
    if debug_distribution == "compensating":
        counts[scenarios[1]] = 0
    # Without acceptance, validation is exactly 1+3, isolating the debug gate.
    # With acceptance, validation is 0+4 and its deviation is legitimately
    # accepted, so only the non-waivable debug gate can prevent freezing.
    h.seed(
        balanced=not accept_deviation, debug_positive_counts=counts
    ).export().annotations()
    by_id = {label.item_id: label for label in h.labels}
    actual = {
        scenario: sum(
            by_id[item.item_id].verdict
            for item in h.items
            if item.split == "debug" and item.scenario_id == scenario
        )
        for scenario in scenarios
    }
    assert actual == {scenario: counts.get(scenario, 1) for scenario in scenarios}
    if debug_distribution == "compensating":
        assert sum(actual.values()) == len(scenarios)
    args = []
    if accept_deviation:
        _, acceptance = h.acceptance()
        assert all(
            value == {"positive": 0, "negative": 4}
            for value in acceptance["distribution"].values()
        )
        args = ["--accept-distribution", acceptance["brief_sha256"]]
    assert not (h.ws / "label-pack").exists()
    before = snapshot(h.root)
    h.cli("freeze", *args, code=3, reason="LABEL_DISTRIBUTION_MISMATCH")
    assert snapshot(h.root) == before
    assert not (h.ws / "label-pack").exists()
    assert not h.calls and not h.balance_calls


@pytest.mark.parametrize(
    "first,second", [("zai", "openai"), ("zai", "deepseek"), ("openai", "deepseek")]
)
def test_debug_provider_values_must_match_mapping_keys(harness, first, second):
    """§8.2a (02eb1e2): correct keys with swapped configs refuse before any write/call."""
    from opspilot.evaluation.judge import HTTPJudgeTransport

    h = harness.seed().export().annotations()
    h.costs("canary-debug")
    configs = dict(h.configs)
    configs[first], configs[second] = configs[second], configs[first]
    assert set(configs) == {"zai", "openai", "deepseek"}
    assert configs[first].provider != first
    transports = {p: HTTPJudgeTransport() for p in configs}
    balance_calls = []
    before = snapshot(h.root)
    assert_p7_error(
        "CONFIG_INVALID",
        lambda: eval_p7.debug_judge(
            h.library_workspace,
            session_id="canary-debug",
            providers=tuple(configs),
            item_ids=None,
            configs=configs,
            transports=transports,
            balance=lambda provider: (
                balance_calls.append(provider) or Decimal("100.00")
            ),
        ),
    )
    assert snapshot(h.root) == before
    assert not (h.ws / "costs/canary-debug/deepseek-before.json").exists()
    assert not h.calls and not h.balance_calls and not balance_calls


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("prompt", "JUDGE_PROMPT_CHANGED"),
        ("items", "PACKET_STALE"),
        ("packet", "PACKET_STALE"),
    ],
)
def test_debug_exported_packet_bindings_refuse_without_side_effects(
    harness, damage, reason
):
    """§8.2a / §9.2.4 (02eb1e2): stale criterion or raw hashes forbid debug exposure."""
    from opspilot.evaluation.judge import HTTPJudgeTransport

    h = harness.seed().export().annotations()
    h.costs("canary-debug")
    configs = dict(h.configs)
    if damage == "prompt":
        new_prompt = "CANARY-P7-NEW-PROMPT-WITHOUT-EXPORTED-CONVENTION"
        assert (
            json.loads((h.ws / "blind/packet.json").read_bytes())["convention"]
            not in new_prompt
        )
        h.mp.setattr(judge, "PROMPT", new_prompt)
        # Keep each config valid against the current criterion. This isolates
        # packet binding from the lower judge's separate config validation.
        configs = {
            p: replace(config, prompt=new_prompt) for p, config in configs.items()
        }
    else:
        path = h.ws / (
            "assembled/items.json" if damage == "items" else "blind/packet.json"
        )
        raw = path.read_bytes()
        path.write_bytes(raw + b"\n")
        assert sha(path.read_bytes()) != sha(raw)
    transports = {p: HTTPJudgeTransport() for p in configs}
    balance_calls = []
    before = snapshot(h.root)
    assert_p7_error(
        reason,
        lambda: eval_p7.debug_judge(
            h.library_workspace,
            session_id="canary-debug",
            providers=tuple(configs),
            item_ids=None,
            configs=configs,
            transports=transports,
            balance=lambda provider: (
                balance_calls.append(provider) or Decimal("100.00")
            ),
        ),
    )
    assert snapshot(h.root) == before
    assert not (h.ws / "costs/canary-debug/deepseek-before.json").exists()
    assert not h.calls and not h.balance_calls and not balance_calls


@pytest.mark.parametrize(
    "first,second", [("zai", "openai"), ("zai", "deepseek"), ("openai", "deepseek")]
)
def test_formal_provider_values_must_match_mapping_keys(harness, first, second):
    """§9.2.2 (02eb1e2): swapped config values cannot occupy validation source families."""
    from opspilot.evaluation.judge import HTTPJudgeTransport

    h = harness.ready()
    configs = dict(h.configs)
    configs[first], configs[second] = configs[second], configs[first]
    assert set(configs) == {"zai", "openai", "deepseek"}
    assert configs[first].provider != first
    transports = {p: HTTPJudgeTransport() for p in configs}
    balance_calls = []
    registry = (h.private / "exposure.jsonl").read_bytes()
    before = snapshot(h.root)
    assert_p7_error(
        "CONFIG_INVALID",
        lambda: eval_p7.formal(
            h.library_workspace,
            run_id="canary-formal",
            pack_hash=h.pack_hash,
            configs=configs,
            transports=transports,
            balance=lambda provider: (
                balance_calls.append(provider) or Decimal("100.00")
            ),
        ),
    )
    assert snapshot(h.root) == before
    assert (h.private / "exposure.jsonl").read_bytes() == registry
    assert not (h.ws / "costs/canary-formal/deepseek-before.json").exists()
    assert not h.calls and not h.balance_calls and not balance_calls
