"""P7 contract r3 acceptance, authored from the contract and public interfaces.

Authority: docs/tasks/2026-10-18-eval-p7-calibration.md section
「合同（r3，2026-10-22，重做 #295）」 (cited D17..D21), the PR-B3 judge interface
(docs/tasks/2026-10-14-eval-pr-b3-interface.md) and the P7 driver interface it
amends. No r3 implementation was read. Fake transports only; the network guard
refuses every socket connect and DNS lookup.

Names r3 leaves open (observation fields, the tuning CLI surface) live in one
block of eval_p7_support.py; the readings taken are in EVAL_P7_R3_AMBIGUITIES.md.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace

import pytest
from eval_b3_support import (
    MODELS,
    USAGE,
    FakeTransport,
    NetworkGuard,
    build_world,
    completion,
    content,
    env_text,
    everywhere,
    http,
    judge_input,
    load,
    make_pack,
    missed_diagnosis,
    ok_reply,
    transports,
    write_env,
)
from eval_contract_support import CAUSE, diagnosis
from eval_p7_support import (
    MODEL_VERDICT_FIELD,
    PROVIDERS,
    TRANSPORT_EXCEPTION_FIELD,
    VERDICT_MATCH_FIELD,
    DriverHarness,
    put,
    sha,
    snapshot,
    tuning_debug,
)

from opspilot.evaluation import judge
from opspilot.evaluation.calibration_flow import (
    CalibrationObservation,
    check_identifiers,
    load_verified_calibration,
    run_calibration,
)
from opspilot.evaluation.codec import canonical_bytes, decode, encode
from opspilot.evaluation.judge import JudgeObservation, judge_once, safe_input
from scripts import eval_p7


@pytest.fixture(scope="module", autouse=True)
def network_guard():
    guard = NetworkGuard()
    with pytest.MonkeyPatch.context() as mp:
        guard.install(mp)
        yield guard
    assert guard.attempts == [], f"network attempted: {guard.attempts}"


@pytest.fixture(scope="module")
def configs(network_guard, tmp_path_factory):
    path = write_env(tmp_path_factory.mktemp("p7-r3-env"), env_text())
    return {provider: load(path, provider) for provider in MODELS}


@pytest.fixture(scope="module")
def world(network_guard):
    return build_world()


@pytest.fixture(scope="module")
def pack(world):
    return make_pack(world)


@pytest.fixture
def harness(tmp_path, monkeypatch, capsys):
    return DriverHarness(tmp_path, monkeypatch, capsys, eval_p7).init()


def call(request, config, respond):
    transport = FakeTransport(respond)
    observation = judge_once(
        request, config=config, calibration=None, transport=transport
    )
    return observation, transport


def reply(diagnoses, verdict, provider):
    return http(
        completion(content(diagnoses, verdict), model=MODELS[provider], usage=USAGE)
    )


def fragment(quote: str) -> str:
    """A shorter verbatim phrase of a labelled quote (D17), never the whole field."""
    short = " ".join(quote.split()[:3])
    assert short and short != quote
    return short


def calibrate(world, pack, configs, tmp_path, **overrides):
    destination = tmp_path / "cal"
    record = run_calibration(
        pack,
        configs=configs,
        transports=transports(world, **overrides),
        destination=destination,
    )
    return destination, record


def raw_records(destination, provider, item_id):
    """The persisted per-call records (provider-NNN.json) of one item."""
    out = []
    for path in sorted(destination.glob(f"{provider}-*.json")):
        value = json.loads(path.read_bytes())
        if value["item_id"] == item_id:
            out.append(value["observation"])
    return out


# ============================================== D17: location-level matching


def test_d17_fragment_quote_at_labelled_location_is_not_a_missing_diagnosis(
    world, pack, configs, tmp_path
):
    """D17: every labelled causal location is present; the quote is not compared."""

    def fragments(label, provider):
        diagnoses = tuple(replace(d, quote=fragment(d.quote)) for d in label.diagnoses)
        return reply(diagnoses, label.verdict, provider)

    _, record = calibrate(
        world, pack, configs, tmp_path, zai=everywhere(fragments, world.authored)
    )
    zai = record.results["zai"]
    assert "KEY_DIAGNOSIS_MISSING" not in zai.reasons
    assert (zai.agreement, zai.false_passes, zai.passed) == (48, 0, True)
    assert record.status == "selected" and record.selected_provider == "zai"


def test_d17_missing_labelled_location_is_a_missing_diagnosis(
    world, pack, configs, tmp_path
):
    """D17: a labelled causal location absent from the judge's causal ones misses."""
    item = world.pick(positive=False, kind="normal", variant="hidden_normal_cause")[0]
    _, record = calibrate(
        world,
        pack,
        configs,
        tmp_path,
        zai={(item.item.item_id, 1): missed_diagnosis},  # moved to summary
    )
    zai = record.results["zai"]
    assert "KEY_DIAGNOSIS_MISSING" in zai.reasons
    assert zai.passed is False and zai.agreement == 47 and zai.false_passes == 0


def test_d17_labelled_location_only_as_non_causal_is_a_missing_diagnosis(
    world, pack, configs, tmp_path
):
    """D17 compares factual_causal locations: a non-causal entry there misses."""
    item = world.pick(positive=False, kind="fault", variant="extra_wrong_cause")[0]
    right, extra = item.label.diagnoses
    assert (right.location, extra.location) == ("claims[0]", "claims[1]")

    def demoted(label, provider):
        # Still False (the extra wrong cause fails the AND), so the verdict agrees.
        return reply((replace(right, factual_causal=False), extra), False, provider)

    _, record = calibrate(
        world, pack, configs, tmp_path, zai={(item.item.item_id, 1): demoted}
    )
    zai = record.results["zai"]
    assert "KEY_DIAGNOSIS_MISSING" in zai.reasons
    assert zai.passed is False and zai.false_passes == 0


def test_d17_quote_must_still_be_a_substring_of_its_field(configs):
    """D17: output validation still requires quote ⊂ field text; no retry."""
    phrase = "cart EmptyCart failed"
    assert phrase in CAUSE and phrase != CAUSE
    observation, transport = call(
        judge_input(), configs["zai"], ok_reply((diagnosis(quote=phrase),), True)
    )
    assert observation.valid is True and observation.result.verdict is True
    assert len(transport.calls) == 1
    observation, transport = call(
        judge_input(),
        configs["zai"],
        ok_reply((diagnosis(quote="cart EmptyCart crashed"),), True),
    )
    assert observation.valid is False
    assert observation.result.status == "invalid"
    assert observation.error_code == "JUDGE_OUTPUT_INVALID"
    assert len(transport.calls) == 1


def test_d17_debug_label_comparison_matches_by_location(harness):
    """D17 applies to the tuning measure too: debug key hits ignore the quote."""
    h = harness.seed().export().annotations()

    def fragments(_payload, provider, label):
        diagnoses = tuple(replace(d, quote=fragment(d.quote)) for d in label.diagnoses)
        return reply(diagnoses, label.verdict, provider)

    h.responder = fragments
    h.costs("canary-debug")
    result = h.cli("debug", "--session", "canary-debug", "--judge-env", h.env)
    assert result["label_comparison"] == {
        p: {"verdict_match": 12, "key_hit": 12} for p in PROVIDERS
    }


# =================================================== D18: recomputed verdict


def test_d18_model_verdict_disagreeing_with_recomputation_is_valid_and_recorded(
    configs,
):
    """D18: the observation verdict is the recomputed one; the model's is kept."""
    observation, transport = call(
        judge_input(), configs["zai"], ok_reply((diagnosis(),), False)
    )
    assert observation.valid is True and observation.error_code is None
    assert observation.result.status == "ok"
    assert observation.result.verdict is True
    assert observation.recomputed_verdict is True
    assert getattr(observation, MODEL_VERDICT_FIELD) is False
    assert getattr(observation, VERDICT_MATCH_FIELD) is False
    assert len(transport.calls) == 1  # never retried

    observation, _ = call(judge_input(), configs["zai"], ok_reply((diagnosis(),), True))
    assert observation.result.verdict is True
    assert getattr(observation, MODEL_VERDICT_FIELD) is True
    assert getattr(observation, VERDICT_MATCH_FIELD) is True


def test_d18_no_decoded_answer_records_no_model_verdict(configs):
    observation, _ = call(
        judge_input(),
        configs["zai"],
        lambda _c: http(completion("verdict: true", usage=USAGE)),
    )
    assert observation.valid is False and observation.result.verdict is None
    assert getattr(observation, MODEL_VERDICT_FIELD) is None
    assert getattr(observation, VERDICT_MATCH_FIELD) is None


def test_d18_negative_label_model_true_recomputed_false_agrees(
    world, pack, configs, tmp_path
):
    """D18: model True, recomputed False on a negative label: agreement, no false pass."""
    item = world.pick(positive=False, kind="normal", variant="hidden_normal_cause")[0]
    item_id = item.item.item_id

    def claims_true(label, provider):
        return reply(label.diagnoses, True, provider)

    destination, record = calibrate(
        world, pack, configs, tmp_path, zai={(item_id, None): claims_true}
    )
    zai = record.results["zai"]
    assert (zai.agreement, zai.false_passes, zai.passed) == (48, 0, True)
    assert record.status == "selected" and record.selected_provider == "zai"
    stored = raw_records(destination, "zai", item_id)
    assert len(stored) == 2
    for observation in stored:
        assert observation["valid"] is True
        assert observation["result"]["verdict"] is False
        assert observation[MODEL_VERDICT_FIELD] is True
        assert observation[VERDICT_MATCH_FIELD] is False
    # Read-back rebuilds the same counts from the persisted records.
    verified = load_verified_calibration(destination, pack=pack, config=configs["zai"])
    assert verified.result == zai


def test_d18_negative_label_model_false_recomputed_true_is_a_false_pass(
    world, pack, configs, tmp_path
):
    """D18: false passes count the recomputed verdict, whatever the model said."""
    item = world.pick(positive=False, kind="normal", variant="hidden_normal_cause")[0]

    def claims_false(label, provider):
        # A normal scenario without any causal diagnosis recomputes to True.
        return reply((), False, provider)

    _, record = calibrate(
        world, pack, configs, tmp_path, zai={(item.item.item_id, 1): claims_false}
    )
    zai = record.results["zai"]
    assert zai.false_passes == 1
    assert zai.agreement == 47 and zai.passed is False
    assert "zai" not in record.eligible


def test_d18_positive_label_model_false_recomputed_true_agrees(
    world, pack, configs, tmp_path
):
    item = world.pick(positive=True, kind="fault")[0]

    def claims_false(label, provider):
        return reply(label.diagnoses, False, provider)

    _, record = calibrate(
        world, pack, configs, tmp_path, zai={(item.item.item_id, None): claims_false}
    )
    zai = record.results["zai"]
    assert (zai.agreement, zai.false_passes, zai.passed) == (48, 0, True)


# ============================================================ D19: PROMPT


def test_d19_prompt_states_the_causal_rules_of_the_final_labels():
    text = " ".join(judge.PROMPT.split())
    lower = text.lower()
    # Keep assertions inside the two causal-category bodies. This checks the
    # rule, rather than merely finding a word in an example or output schema.
    categories = list(
        re.finditer(
            r"\b(?P<kind>(?:not|non)[-\s](?:factual\s+)?causal|"
            r"(?:factual\s+)?causal)"
            r"(?:\s+(?:assertions?|statements?|diagnoses?))?\s*[:\-]",
            lower,
        )
    )
    assert categories
    bodies = []
    for index, category in enumerate(categories):
        end = (
            categories[index + 1].start() if index + 1 < len(categories) else len(lower)
        )
        bodies.append((category.group("kind"), lower[category.end() : end]))
    not_causal = [body for kind, body in bodies if kind.startswith(("not", "non"))]
    factual_causal = [
        body for kind, body in bodies if not kind.startswith(("not", "non"))
    ]
    assert not_causal
    assert factual_causal

    def clauses(body):
        return re.split(r"[.;!?]|\n+", body)

    # D23: locating THIS incident's failure or slowness is causal.
    for phrase in ("failing dependency is", "confined to", "dominated by"):
        assert any(
            phrase in clause for body in factual_causal for clause in clauses(body)
        ), phrase
    assert any(
        re.search(r"\b(?:this\s+)?incident\b", clause)
        and re.search(r"\b(?:failure|slowness|slow(?:ness)?)\b", clause)
        for body in factual_causal
        for clause in clauses(body)
    )

    # D22: a cause that is inferred and unconfirmed is not factual causal, but
    # an asserted cause can remain factual when the inference method is stated.
    assert any(
        re.search(r"\b(?:cause|causation|causal)\b", clause)
        and re.search(r"\binfer\w*\b", clause)
        and re.search(r"\b(?:unconfirm\w*|uncertain|not\s+established)\b", clause)
        for body in not_causal
        for clause in clauses(body)
    )
    assert any(
        re.search(r"\b(?:cause|causation|causal)\b", clause)
        and re.search(r"\binfer\w*\b", clause)
        and re.search(r"\b(?:method|span|duration|trace)\w*\b", clause)
        for body in factual_causal
        for clause in clauses(body)
    )

    # D23: telemetry-only error/status observations are not causal.
    def telemetry_observation(clause):
        return re.search(
            r"\b(?:error|errors|status|statuses|span|spans|series)\b", clause
        ) and re.search(
            r"\b(?:only|concentrat\w*|carry|contain|count\w*|show|have)\b",
            clause,
        )

    assert any(
        telemetry_observation(clause) for body in not_causal for clause in clauses(body)
    )
    assert not any(
        telemetry_observation(clause)
        for body in factual_causal
        for clause in clauses(body)
    )
    # Propagation records the origin service and operation.
    assert "propagat" in lower and "origin" in lower
    # Rejected / ruled-out hypotheses and off-incident "so" clauses are not.
    assert any(w in lower for w in ("rejected", "ruled out", "excluded"))
    assert re.search(r"[\"'“‘]so[\"'”’]|\bso[- ]clause", lower)
    # P7-D11 tuning wording: quote one contiguous substring without elision;
    # citations come only from that one claim; a hypothesis label is not a hedge.
    assert re.search(
        r"\bquote\b.{0,180}\b(?:one|single)\s+contiguous\b.{0,120}"
        r"\bverbatim\b.{0,120}\b(?:ellipsis|elision|three\s+dots)\b",
        lower,
    )
    assert re.search(
        r"\bcited_evidence_ids\b.{0,180}\b(?:only|solely)\b.{0,100}"
        r"\b(?:one|single)\s+claim\b",
        lower,
    )
    assert re.search(
        r"\bhypothes\w*\b.{0,120}\b(?:label|kind)\b.{0,100}"
        r"\b(?:not|does\s+not|is\s+not)\b.{0,50}\bhedg\w*\b",
        lower,
    )


# ========================================================== D20: transport


class CanaryStreamCut(Exception):
    """A transport failure whose message must never be recorded."""


def test_d20_transport_exception_class_name_is_recorded(configs):
    observation, transport = call(
        judge_input(),
        configs["zai"],
        lambda _c: CanaryStreamCut("CANARY-P7-TRANSPORT-DETAIL"),
    )
    assert observation.result.status == "error"
    assert observation.error_code == "JUDGE_TRANSPORT_ERROR"
    assert len(transport.calls) == 2  # the existing one transport retry
    name = getattr(observation, TRANSPORT_EXCEPTION_FIELD)
    assert name == "CanaryStreamCut" or name.endswith(".CanaryStreamCut")
    encoded = json.dumps(encode(observation))
    assert "CANARY-P7-TRANSPORT-DETAIL" not in encoded + repr(observation)
    check_identifiers(observation)  # stays inside the calibration identifier rule

    observation, _ = call(judge_input(), configs["zai"], ok_reply((diagnosis(),), True))
    assert getattr(observation, TRANSPORT_EXCEPTION_FIELD) is None


def test_d20_new_observation_fields_are_absent_ok_for_old_artifacts(configs):
    observation, _ = call(
        judge_input(), configs["zai"], ok_reply((diagnosis(),), False)
    )
    current = encode(observation)
    new_fields = {MODEL_VERDICT_FIELD, VERDICT_MATCH_FIELD, TRANSPORT_EXCEPTION_FIELD}
    assert new_fields <= set(current)
    again = decode(JudgeObservation, current)
    for name in new_fields:
        assert getattr(again, name) == getattr(observation, name)
    legacy = {k: v for k, v in current.items() if k not in new_fields}
    old = decode(JudgeObservation, legacy)
    for name in new_fields:
        assert getattr(old, name) is None
    record = {"item_id": "item-1", "input_hash": "0" * 64, "repetition": 1}
    stored = decode(CalibrationObservation, {**record, "observation": legacy})
    assert getattr(stored.observation, TRANSPORT_EXCEPTION_FIELD) is None


@pytest.mark.parametrize(
    "answer",
    [
        "CANARY not json",
        content((diagnosis(quote="a phrase the report never says"),), True),
        content((diagnosis(),), True, calibrated=True),
    ],
)
def test_d20_invalid_answers_are_not_retried(configs, answer):
    observation, transport = call(
        judge_input(),
        configs["zai"],
        lambda _c: http(completion(answer, usage=USAGE)),
    )
    assert observation.result.status == "invalid"
    assert observation.error_code in {"JUDGE_OUTPUT_INVALID", "SCHEMA_INVALID"}
    assert len(transport.calls) == 1 and observation.attempts == 1


RAW_MARKER = "CANARY-P7-RAW-MODEL-OUTPUT"


def _assert_raw_output_kept_privately(h, result):
    assert RAW_MARKER not in json.dumps(result)
    holders = [
        path
        for path, (_mode, value) in snapshot(h.root).items()
        if value[0] == "file" and RAW_MARKER.encode() in value[1]
    ]
    assert holders, "the rejected raw output was not persisted"
    for relative in holders:
        path = h.root / relative
        assert path.is_relative_to(h.private), relative
        assert path.stat().st_mode & 0o077 == 0, relative
        ignored = subprocess.run(
            ["git", "-C", str(h.root), "check-ignore", "-q", "--", str(path)]
        )
        assert ignored.returncode == 0, relative


def _one_rejected_answer(h, *, split):
    raws = {i.request.report.raw for i in h.items if i.split == split}
    sent = []

    def respond(payload, provider, _label):
        if provider == "zai" and payload["report_raw"] in raws and not sent:
            sent.append(provider)
            return http(
                completion(
                    f"{RAW_MARKER} this is not JSON",
                    model=MODELS[provider],
                    usage=USAGE,
                )
            )
        return None

    h.responder = respond
    return sent


def test_d20_debug_keeps_rejected_raw_output_in_private_workspace(harness):
    h = harness.seed().export().annotations()
    sent = _one_rejected_answer(h, split="debug")
    h.costs("canary-debug")
    result = h.cli("debug", "--session", "canary-debug", "--judge-env", h.env)
    assert sent == ["zai"]
    assert result["results"]["zai"].get("invalid") == 1
    _assert_raw_output_kept_privately(h, result)


def test_d20_formal_keeps_rejected_raw_output_in_private_workspace(harness):
    h = harness.ready()
    sent = _one_rejected_answer(h, split="validation")
    result = h.formal(code=6)  # zai fails, openai passes: pending (D16)
    assert sent == ["zai"]
    _assert_raw_output_kept_privately(h, result)


# ============================================== D21: formal-exposed tuning


def test_d21_formal_exposed_validation_items_are_tuning_items_never_validation(
    harness,
):
    h = harness.ready()
    validation_families = {i.source_family for i in h.items if i.split == "validation"}
    h.formal()
    h.calls.clear()
    h.balance_calls.clear()
    registry = (h.private / "exposure.jsonl").read_bytes()
    every = sorted(i.item_id for i in h.items)  # 12 debug + 24 exposed validation
    h.costs("canary-tuning")
    tuning_debug(h, "canary-tuning", items=every)
    sent = [canonical_bytes(payload) for payload in h.calls]
    assert len(sent) == 36 * 3
    assert set(sent) == {canonical_bytes(safe_input(i.request)) for i in h.items}
    records = [
        p
        for p in (h.ws / "debug/canary-tuning").glob("*.json")
        if re.fullmatch(r"(zai|openai|deepseek)-\d{3}\.json", p.name)
    ]
    assert len(records) == 36 * 3
    assert all(
        json.loads(p.read_bytes())["observation"]["passed"] is False for p in records
    )
    after = (h.private / "exposure.jsonl").read_bytes()
    assert after.startswith(registry)
    (row,) = [json.loads(line) for line in after[len(registry) :].splitlines()]
    assert row["kind"] == "debug"
    assert row["tuning"] is True
    assert validation_families <= set(row["debug_families"])
    # The exposure registry still keeps them out of any later formal run.
    h.costs("canary-formal-2")
    h.calls.clear()
    h.balance_calls.clear()
    before = snapshot(h.root)
    h.cli(
        "formal",
        "--run",
        "canary-formal-2",
        "--pack-hash",
        h.pack_hash,
        "--judge-env",
        h.env,
        code=3,
        reason="VALIDATION_EXPOSED",
    )
    assert snapshot(h.root) == before
    assert (h.private / "exposure.jsonl").read_bytes() == after
    assert not h.calls and not h.balance_calls


def test_d21_unexposed_validation_items_still_cannot_be_tuned(harness):
    """D21 reuses only formal-exposed items; a live validation item stays sealed."""
    h = harness.seed().export().annotations()
    item_id = next(i.item_id for i in h.items if i.split == "validation")
    h.costs("canary-tuning")
    before = snapshot(h.root)
    registry = (h.private / "exposure.jsonl").read_bytes()
    tuning_debug(
        h,
        "canary-tuning",
        items=[item_id],
        code=3,
        reason="VALIDATION_EXPOSURE_FORBIDDEN",
    )
    assert snapshot(h.root) == before
    assert (h.private / "exposure.jsonl").read_bytes() == registry
    assert not h.calls and not h.balance_calls


def _age_convention(h):
    """The workspace was exported under an older judge convention (pre-r3)."""
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
    assert packet["convention"] not in judge.PROMPT


def test_d21_old_convention_workspace_tunes_but_never_runs_formal(harness):
    h = harness.seed().export().annotations().freeze()
    _age_convention(h)
    h.calls.clear()
    h.costs("canary-debug")
    tuning_debug(h, "canary-debug")
    assert len(h.calls) == 12 * 3
    # Formal keeps the criterion binding (D21 changes tuning only).
    h.costs("canary-formal")
    h.calls.clear()
    h.balance_calls.clear()
    before = snapshot(h.root)
    registry = (h.private / "exposure.jsonl").read_bytes()
    h.formal(code=3, reason="JUDGE_PROMPT_CHANGED")
    assert snapshot(h.root) == before
    assert (h.private / "exposure.jsonl").read_bytes() == registry
    assert not h.calls and not h.balance_calls


def test_d21_old_convention_does_not_excuse_tampered_packet_bindings(harness):
    """Blind-packet integrity still holds for tuning debug."""
    h = harness.seed().export().annotations()
    _age_convention(h)
    path = h.ws / "assembled/items.json"
    path.write_bytes(path.read_bytes() + b"\n")
    h.costs("canary-debug")
    before = snapshot(h.root)
    tuning_debug(h, "canary-debug", code=3, reason="PACKET_STALE")
    assert snapshot(h.root) == before
    assert not h.calls and not h.balance_calls
