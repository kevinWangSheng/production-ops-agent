"""r10 (#237) acceptance tests: artifact write bound and the slimmed calibration.json.

Authority: docs/tasks/2026-10-14-eval-pr-b3-interface.md r10 items 1-3, with r3
item 5, r4 and r6 for the error domain. Written from the public interface and the
contract only; no B3 implementation body is read. Fully offline: fake transports
and the module-wide socket guard from eval_b3_support.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path

import pytest
from eval_b3_support import (
    CANARIES,
    MODELS,
    Authored,
    NetworkGuard,
    World,
    build_world,
    env_text,
    error_code,
    everywhere,
    http_error,
    input_hash,
    leak_surface,
    load,
    make_pack,
    transports,
    write_env,
)

from opspilot.evaluation.calibration_flow import (
    freeze_label_pack,
    load_verified_calibration,
    run_calibration,
)
from opspilot.evaluation.judge import JudgeError

READ_LIMIT = 67108864  # r10 item 1: write bound equals the 64 MiB read bound
SUMMARY_KEYS = {"schema", "pack_hash", "configs", "records", "selection"}
RECORDS_PER_PROVIDER = 60  # r3 item 5: 12 debug + 48 validation calls
HEX64 = re.compile(r"[0-9a-f]{64}")

# ----------------------------------------------------------------- fixtures


@pytest.fixture(scope="module", autouse=True)
def network_guard():
    guard = NetworkGuard()
    with pytest.MonkeyPatch.context() as mp:
        guard.install(mp)
        yield guard
    assert guard.attempts == [], f"network attempted: {guard.attempts}"


@pytest.fixture(autouse=True)
def no_network(network_guard):
    before = len(network_guard.attempts)
    yield
    assert network_guard.attempts[before:] == [], "test opened a network connection"


@pytest.fixture(scope="module")
def configs(network_guard, tmp_path_factory):
    directory = tmp_path_factory.mktemp("judge-env")
    path = write_env(directory, env_text())
    return {provider: load(path, provider) for provider in MODELS}


@pytest.fixture(scope="module")
def world(network_guard):
    return build_world()


@pytest.fixture(scope="module")
def pack(world):
    return make_pack(world)


@pytest.fixture(scope="module")
def selected(world, pack, configs, tmp_path_factory):
    destination = tmp_path_factory.mktemp("calibration") / "selected"
    record = run_calibration(
        pack, configs=configs, transports=transports(world), destination=destination
    )
    assert record.status == "selected" and record.selected_provider == "zai"
    return destination


def expect_judge_error(exc: BaseException, codes) -> str:
    """r2 item 6 / r6: fixed JudgeError code, no chain, no credential in the surface."""
    assert isinstance(exc, JudgeError), type(exc).__name__
    assert exc.__cause__ is None and exc.__context__ is None
    surface = leak_surface(exc)
    for secret in CANARIES:
        assert secret not in surface
    code = error_code(exc)
    assert code is not None, "no fixed error code"
    assert code in codes, code
    return code


def raw_record(destination: Path, provider: str, n: int) -> Path:
    return destination / f"{provider}-{n:03d}.json"  # r3 item 5 naming


def copy_run(source: Path, target: Path) -> Path:
    """Copies a run directory byte for byte, keeping the 0600 modes (r6)."""
    for path in sorted(source.rglob("*")):
        out = target / path.relative_to(source)
        if path.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(path.read_bytes())
            os.chmod(out, 0o600)
    return target


def summary(destination: Path) -> dict:
    return json.loads((destination / "calibration.json").read_bytes())


def rewrite_summary(destination: Path, doc: dict) -> None:
    path = destination / "calibration.json"
    os.chmod(path, 0o600)
    path.write_text(json.dumps(doc))


def verify(destination: Path, pack, configs):
    return load_verified_calibration(destination, pack=pack, config=configs["zai"])


# ================================================== r10 item 1: write bound


def padded_world(world: World, per_item: int) -> World:
    """Each report gains one large, legitimate free-text gap (r9: gaps are content).

    The padding is spread over the 36 items so that every P6 request stays well
    under max_request_bytes (8 MiB) while the serialized items exceed 64 MiB;
    input_hash is recomputed so the pack stays internally consistent.
    """
    authored = []
    for a in world.authored:
        doc = json.loads(a.item.request.report.raw)
        doc["gaps"] = [*doc["gaps"], "Unmeasured detail " + "x" * per_item]
        report = replace(a.item.request.report, raw=json.dumps(doc))
        request = replace(a.item.request, report=report)
        digest = input_hash(request)
        item = replace(a.item, request=request, input_hash=digest)
        label = replace(a.label, input_hash=digest)
        authored.append(Authored(item, label, a.variant, a.positive))
    return World(tuple(authored))


@pytest.fixture(scope="module")
def oversized(world):
    per_item = READ_LIMIT // len(world.authored) + 65536
    assert per_item * 4 < 8 * 1024 * 1024  # each request stays far below 8 MiB
    big = padded_world(world, per_item)
    return big, make_pack(big)


def test_freeze_refuses_items_over_read_limit(oversized, tmp_path):
    _, big_pack = oversized
    target = tmp_path / "pack"
    with pytest.raises(Exception) as caught:
        freeze_label_pack(target, big_pack)
    expect_judge_error(caught.value, {"ARTIFACT_UNWRITABLE"})
    assert not (target / "items.json").exists()
    assert not (target / "pack.json").exists()


def test_run_calibration_refuses_items_over_read_limit_before_any_call(
    oversized, configs, tmp_path
):
    big_world, big_pack = oversized
    ts = transports(big_world)
    destination = tmp_path / "cal"
    with pytest.raises(Exception) as caught:
        run_calibration(
            big_pack, configs=configs, transports=ts, destination=destination
        )
    expect_judge_error(caught.value, {"ARTIFACT_UNWRITABLE"})
    assert all(t.calls == [] for t in ts.values())  # zero paid calls
    assert not (destination / "label-pack" / "items.json").exists()
    assert not (destination / "label-pack" / "pack.json").exists()
    assert not (destination / "calibration.json").exists()
    assert not any(raw_record(destination, p, 1).exists() for p in MODELS)


# ============================================== r10 item 2: summary layout


def test_summary_holds_only_cache_fields_and_raw_record_digests(selected):
    doc = summary(selected)
    assert set(doc) == SUMMARY_KEYS
    assert doc["schema"] == "opspilot-judge-calibration-1"
    assert "observations" not in doc
    records = doc["records"]
    assert set(records) == set(MODELS)
    for provider in MODELS:
        digests = records[provider]
        assert isinstance(digests, list) and len(digests) == RECORDS_PER_PROVIDER
        for n, digest in enumerate(digests, 1):
            assert isinstance(digest, str) and HEX64.fullmatch(digest), digest
            raw = raw_record(selected, provider, n).read_bytes()
            assert digest == hashlib.sha256(raw).hexdigest(), (provider, n)


def test_unchanged_rewritten_summary_still_verifies(selected, pack, configs, tmp_path):
    """Control for the tamper tests below: re-serializing alone is not a rejection."""
    run = copy_run(selected, tmp_path / "run")
    rewrite_summary(run, summary(run))
    assert verify(run, pack, configs).provider == "zai"


# ======================================== r10 item 3: loader decision order


def _tampered(selected, tmp_path, change) -> Path:
    run = copy_run(selected, tmp_path / "run")
    doc = summary(run)
    change(doc)
    rewrite_summary(run, doc)
    return run


def _wrong_digest(doc):
    digest = doc["records"]["zai"][7]
    doc["records"]["zai"][7] = hashlib.sha256(digest.encode()).hexdigest()


def _swapped_order(doc):
    digests = doc["records"]["openai"]
    digests[0], digests[1] = digests[1], digests[0]


def _upper_digest(doc):
    doc["records"]["zai"][0] = doc["records"]["zai"][0].upper()


def _short_digest(doc):
    doc["records"]["deepseek"][3] = doc["records"]["deepseek"][3][:63]


def _observations_back(doc):
    doc["observations"] = []


def _unknown_field(doc):
    doc["passed"] = True


def _missing_field(doc):
    del doc["records"]


def _one_short(doc):
    doc["records"]["zai"].pop()


def _one_extra(doc):
    doc["records"]["zai"].append(doc["records"]["zai"][0])


def _provider_missing(doc):
    del doc["records"]["deepseek"]


TAMPERS = {
    # canonical hex that no longer matches the raw bytes
    "wrong_digest": (_wrong_digest, {"HASH_MISMATCH"}),
    "swapped_order": (_swapped_order, {"HASH_MISMATCH"}),
    # non-canonical digests and extra/missing fields
    "uppercase_digest": (_upper_digest, {"SCHEMA_INVALID"}),
    "short_digest": (_short_digest, {"SCHEMA_INVALID"}),
    "observations_back": (_observations_back, {"SCHEMA_INVALID"}),
    "unknown_top_level_field": (_unknown_field, {"SCHEMA_INVALID"}),
    "records_missing": (_missing_field, {"SCHEMA_INVALID"}),
    # r10 item 3 names the record count and provider set under HASH_MISMATCH;
    # item 2 fixes 60 digests per provider, so SCHEMA_INVALID is also accepted
    # (contract ambiguity, reported).
    "one_digest_short": (_one_short, {"HASH_MISMATCH", "SCHEMA_INVALID"}),
    "one_digest_extra": (_one_extra, {"HASH_MISMATCH", "SCHEMA_INVALID"}),
    "provider_missing": (_provider_missing, {"HASH_MISMATCH", "SCHEMA_INVALID"}),
}


@pytest.mark.parametrize("name", sorted(TAMPERS))
def test_tampered_records_rejected(selected, pack, configs, tmp_path, name):
    change, codes = TAMPERS[name]
    run = _tampered(selected, tmp_path, change)
    with pytest.raises(Exception) as caught:
        verify(run, pack, configs)
    expect_judge_error(caught.value, codes)


def test_raw_record_changed_under_intact_summary_rejected(
    selected, pack, configs, tmp_path
):
    """A raw record whose bytes change without changing the derived selection."""
    run = copy_run(selected, tmp_path / "run")
    path = raw_record(run, "deepseek", 5)
    doc = json.loads(path.read_bytes())
    path.write_text(json.dumps(doc, indent=1))  # same content, other bytes
    os.chmod(path, 0o600)
    with pytest.raises(Exception) as caught:
        verify(run, pack, configs)
    expect_judge_error(caught.value, {"HASH_MISMATCH"})


# ====================================== r10 item 3 / r4: swapped summary first


def test_passing_summary_swapped_into_failing_run_is_derived_mismatch(
    world, pack, configs, selected, tmp_path
):
    failing = tmp_path / "failing"
    ts = transports(world, zai=everywhere(http_error, world.authored))
    record = run_calibration(pack, configs=configs, transports=ts, destination=failing)
    assert record.results["zai"].passed is False and record.selected_provider is None
    target = failing / "calibration.json"
    os.chmod(target, 0o600)
    target.write_bytes((selected / "calibration.json").read_bytes())
    with pytest.raises(Exception) as caught:
        verify(failing, pack, configs)
    # selection is recomputed before records digests are compared (r10 item 3).
    expect_judge_error(caught.value, {"DERIVED_RESULT_MISMATCH"})
