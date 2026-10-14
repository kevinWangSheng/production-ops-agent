"""P7 blind-label freezing, strict calibration read-back and D16 selection.

No human annotation or model availability is claimed by this module. Artifacts
must stay in a private ignored directory; callers own the independent authors.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, cast

from opspilot.evaluation.codec import (
    StorageError,
    canonical_bytes,
    decode,
    exact,
    parse,
    validated,
)
from opspilot.evaluation.judge import (
    OBSERVATION_ERROR_CODES,
    JudgeConfig,
    JudgeError,
    JudgeInput,
    JudgeObservation,
    JudgeTransport,
    _check_config,
    check_diagnoses,
    judge_config_hash,
    judge_once,
    response_identity_matches,
    safe_input,
)
from opspilot.evaluation.model import JudgedDiagnosis
from opspilot.evaluation.rounds import CalibrationResult, calibrate

SCENARIOS = frozenset(
    {
        "eval-normal-1",
        "eval-normal-2",
        "eval-payment-failure-1",
        "eval-payment-failure-2",
        "eval-product-catalog-failure",
        "eval-cart-failure",
    }
)
COVERAGE = frozenset(
    {
        "hidden_normal_cause",
        "summary_claim_mismatch",
        "extra_wrong_cause",
        "borrowed_citation",
        "hypothesis",
        "no_positive_diagnosis",
        "insufficient_evidence",
    }
)
_PROVIDERS = frozenset({"zai", "openai", "deepseek"})
_FILES = (
    "items.json",
    "annotations-1.json",
    "annotations-2.json",
    "adjudications.json",
)
_MAX_FILE = 64 * 1024 * 1024


@dataclass(frozen=True)
class CalibrationItem:
    item_id: str
    scenario_id: str
    split: Literal["debug", "validation"]
    source_family: str
    input_hash: str
    request: JudgeInput
    coverage: tuple[str, ...]


@dataclass(frozen=True)
class BlindLabel:
    item_id: str
    input_hash: str
    diagnoses: tuple[JudgedDiagnosis, ...]
    verdict: bool


@dataclass(frozen=True)
class AnnotationSet:
    annotator_identity: str
    model_identity: str
    labels: tuple[BlindLabel, ...]


@dataclass(frozen=True)
class Adjudication:
    item_id: str
    annotation_hashes: tuple[str, str]
    decision_record: str
    label: BlindLabel


@dataclass(frozen=True)
class LabelPack:
    items: tuple[CalibrationItem, ...]
    annotations: tuple[AnnotationSet, ...]
    adjudications: tuple[Adjudication, ...]


@dataclass(frozen=True)
class SelectionRecord:
    status: Literal["selected", "pending_user_decision", "no_baseline"]
    selected_provider: str | None
    eligible: tuple[str, ...]
    pack_hash: str
    results: Mapping[str, CalibrationResult]


@dataclass(frozen=True)
class CalibrationObservation:
    item_id: str
    input_hash: str
    repetition: int
    observation: JudgeObservation


@dataclass(frozen=True)
class VerifiedCalibration:
    pack_hash: str
    config_hash: str
    provider: str
    result: CalibrationResult
    # Proof is rechecked at use, so replacing a dataclass field cannot forge trust.
    _proof: bytes = field(repr=False)
    _pack: LabelPack = field(repr=False)
    _directory: Path = field(repr=False)


def _validated_pack(pack: LabelPack) -> LabelPack:
    try:
        return cast(LabelPack, validated(LabelPack, pack))
    except StorageError:
        pass
    raise JudgeError("SCHEMA_INVALID")


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _parts(pack: LabelPack) -> dict[str, bytes]:
    return dict(
        zip(
            _FILES,
            (
                canonical_bytes({"items": pack.items}),
                canonical_bytes(pack.annotations[0]),
                canonical_bytes(pack.annotations[1]),
                canonical_bytes({"adjudications": pack.adjudications}),
            ),
            strict=True,
        )
    )


def label_pack_hash(pack: LabelPack) -> str:
    final_labels(pack)
    return _hash(
        {
            "schema": "opspilot-label-pack-1",
            "files": {
                k: hashlib.sha256(v).hexdigest() for k, v in _parts(pack).items()
            },
        }
    )


def final_labels(pack: LabelPack) -> Mapping[str, BlindLabel]:
    """Reconstruct labels; never trust cached verdicts or adjudication membership."""
    try:
        pack = validated(LabelPack, pack)
        if len(pack.items) != 36 or len(pack.annotations) != 2:
            raise JudgeError("SCHEMA_INVALID")
        ids = {item.item_id for item in pack.items}
        if len(ids) != 36 or "" in ids:
            raise JudgeError("SCHEMA_INVALID")
        distribution = Counter((item.scenario_id, item.split) for item in pack.items)
        wanted = Counter(
            {
                (sid, split): count
                for sid in SCENARIOS
                for split, count in (("debug", 2), ("validation", 4))
            }
        )
        if distribution != wanted:
            raise JudgeError("SCHEMA_INVALID")
        sources: dict[str, str] = {}
        input_hashes: set[str] = set()
        coverage: set[str] = set()
        items = {item.item_id: item for item in pack.items}
        for item in pack.items:
            if (
                item.scenario_id != item.request.spec.scenario_id
                or not item.source_family.strip()
                or item.input_hash != _hash(safe_input(item.request))
                or item.input_hash in input_hashes
                or not set(item.coverage) <= COVERAGE
            ):
                raise JudgeError("HASH_MISMATCH")
            input_hashes.add(item.input_hash)
            if sources.setdefault(item.source_family, item.split) != item.split:
                raise JudgeError("CALIBRATION_SET_OVERLAP")
            if item.split == "validation":
                coverage.update(item.coverage)
        if coverage != COVERAGE:
            raise JudgeError("CALIBRATION_COVERAGE_INCOMPLETE")
        first, second = pack.annotations
        if (
            not first.annotator_identity.strip()
            or not second.annotator_identity.strip()
            or first.annotator_identity == second.annotator_identity
            or not first.model_identity.strip()
            or not second.model_identity.strip()
            or any(
                any(
                    response_identity_matches(p, a.model_identity.strip().casefold())
                    for p in ("zai", "openai", "deepseek")
                )
                for a in pack.annotations
            )
        ):
            raise JudgeError("CALIBRATION_UNRESOLVED")
        annotations: list[dict[str, BlindLabel]] = []
        for annotation in pack.annotations:
            labels = {label.item_id: label for label in annotation.labels}
            if len(labels) != 36 or len(annotation.labels) != 36 or set(labels) != ids:
                raise JudgeError("SCHEMA_INVALID")
            for label in annotation.labels:
                _check_label(items[label.item_id], label)
            annotations.append(labels)
        adjudications = {a.item_id: a for a in pack.adjudications}
        if (
            len(adjudications) != len(pack.adjudications)
            or not set(adjudications) <= ids
        ):
            raise JudgeError("CALIBRATION_UNRESOLVED")
        annotation_hashes = (_hash(first), _hash(second))
        final: dict[str, BlindLabel] = {}
        for item_id in ids:
            left, right = (a[item_id] for a in annotations)
            if left == right:
                if item_id in adjudications:
                    raise JudgeError("CALIBRATION_UNRESOLVED")
                final[item_id] = left
            else:
                decision = adjudications.get(item_id)
                if (
                    decision is None
                    or decision.annotation_hashes != annotation_hashes
                    or not decision.decision_record.strip()
                    or decision.label.item_id != item_id
                ):
                    raise JudgeError("CALIBRATION_UNRESOLVED")
                _check_label(items[item_id], decision.label)
                final[item_id] = decision.label
        if (
            sum(
                not final[i.item_id].verdict
                for i in pack.items
                if i.split == "validation"
            )
            < 12
        ):
            raise JudgeError("CALIBRATION_TOO_FEW_NEGATIVES")
        return final
    except StorageError:
        pass
    raise JudgeError("SCHEMA_INVALID")


def _check_label(item: CalibrationItem, label: BlindLabel) -> None:
    if label.input_hash != item.input_hash:
        raise JudgeError("HASH_MISMATCH")
    valid = False
    try:
        valid = (
            check_diagnoses(item.request, label.diagnoses, label.verdict)
            is label.verdict
        )
    except (JudgeError, StorageError):
        pass
    if not valid:
        # Labels are not judge observations; never expose judge-output codes here.
        raise JudgeError("CALIBRATION_LABEL_INVALID")


def _create_directory(path: Path) -> None:
    code: str | None = None
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        code = "OUTPUT_EXISTS"
    except OSError:
        code = "ARTIFACT_UNWRITABLE"
    if code is not None:
        raise JudgeError(code)


def _write(path: Path, raw: bytes) -> None:
    code: str | None = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        code = "OUTPUT_EXISTS"
    except OSError:
        code = "ARTIFACT_UNWRITABLE"
    if code is not None:
        raise JudgeError(code)


def _read(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE:
                raise JudgeError("SCHEMA_INVALID")
            raw = stream.read(_MAX_FILE + 1)
            if len(raw) > _MAX_FILE:
                raise JudgeError("SCHEMA_INVALID")
            return raw
    except OSError:
        pass
    raise JudgeError("EVIDENCE_UNAVAILABLE")


def _check_directory(path: Path) -> None:
    try:
        if stat.S_ISDIR(path.lstat().st_mode):
            return
    except OSError:
        pass
    raise JudgeError("EVIDENCE_UNAVAILABLE")


def _calibration_source(path: Path) -> tuple[bytes, Path]:
    # Directory is the primary public API; retain the original explicit-file API.
    try:
        mode = path.lstat().st_mode
    except OSError:
        pass
    else:
        if stat.S_ISDIR(mode):
            return _read(path / "calibration.json"), path
        if stat.S_ISREG(mode) and path.name == "calibration.json":
            return _read(path), path.parent
    raise JudgeError("EVIDENCE_UNAVAILABLE")


def freeze_label_pack(path: Path, pack: LabelPack) -> str:
    pack = _validated_pack(pack)
    pack_hash = label_pack_hash(pack)
    parts = _parts(pack)
    index = canonical_bytes(
        {
            "schema": "opspilot-label-pack-1",
            "files": {k: hashlib.sha256(v).hexdigest() for k, v in parts.items()},
        }
    )
    _create_directory(path)
    for name, raw in parts.items():
        _write(path / name, raw)
    _write(
        path / "pack.json", index
    )  # Index published last; partial packs cannot load.
    return pack_hash


def load_label_pack(path: Path, *, expected_pack_hash: str) -> LabelPack:
    _check_directory(path)
    try:
        raw = _read(path / "pack.json")
        obj = exact(parse(raw), {"schema", "files"})
        if _hash(obj) != expected_pack_hash:
            raise JudgeError("HASH_MISMATCH")
        if obj["schema"] != "opspilot-label-pack-1":
            raise JudgeError("SCHEMA_INVALID")
        hashes = exact(obj["files"], set(_FILES))
        parts: dict[str, bytes] = {}
        for name in _FILES:
            parts[name] = _read(path / name)
            if hashlib.sha256(parts[name]).hexdigest() != hashes[name]:
                raise JudgeError("HASH_MISMATCH")
        pack = LabelPack(
            decode(
                tuple[CalibrationItem, ...],
                exact(parse(parts["items.json"]), {"items"})["items"],
            ),
            tuple(decode(AnnotationSet, parse(parts[name])) for name in _FILES[1:3]),
            decode(
                tuple[Adjudication, ...],
                exact(parse(parts["adjudications.json"]), {"adjudications"})[
                    "adjudications"
                ],
            ),
        )
        if label_pack_hash(pack) != expected_pack_hash:
            raise JudgeError("HASH_MISMATCH")
        return pack
    except StorageError:
        pass
    raise JudgeError("SCHEMA_INVALID")


def _missing_diagnosis(label: BlindLabel, observation: JudgeObservation) -> bool:
    expected = {(d.location, d.quote) for d in label.diagnoses if d.factual_causal}
    actual = {
        (d.location, d.quote) for d in observation.result.diagnoses if d.factual_causal
    }
    return not expected <= actual


def _counts(
    pack: LabelPack,
    labels: Mapping[str, BlindLabel],
    config: JudgeConfig,
    observations: tuple[CalibrationObservation, ...],
    pack_hash: str,
) -> CalibrationResult:
    config_hash = judge_config_hash(config)
    wanted = Counter(
        (i.item_id, repetition)
        for i in pack.items
        for repetition in range(1, (2 if i.split == "validation" else 1) + 1)
    )
    if Counter((o.item_id, o.repetition) for o in observations) != wanted:
        raise JudgeError("SCHEMA_INVALID")
    items = {i.item_id: i for i in pack.items}
    verdicts: list[tuple[str, bool]] = []
    problems: set[str] = set()
    invalid_agreements = 0
    for entry in observations:
        item = items[entry.item_id]
        observation = entry.observation
        result = observation.result
        if (
            entry.input_hash != item.input_hash
            or result.config_hash != config_hash
            or result.calibrated
            or result.calibration_hash != ""
            or observation.passed
            or observation.provider != config.provider
            or observation.phase != "calibration"
            or observation.balance_delta is not None
            or observation.elapsed_seconds < 0
            or observation.elapsed_seconds > config.timeout_seconds
            and observation.valid
        ):
            raise JudgeError("DERIVED_RESULT_MISMATCH")
        if result.status == "ok":
            if result.verdict is None:
                raise JudgeError("SCHEMA_INVALID")
            recomputed = check_diagnoses(item.request, result.diagnoses, result.verdict)
            if (
                not observation.valid
                or observation.error_code is not None
                or not response_identity_matches(
                    config.provider, observation.response_model
                )
                or observation.recomputed_verdict is not recomputed
            ):
                raise JudgeError("DERIVED_RESULT_MISMATCH")
            if _missing_diagnosis(labels[item.item_id], observation):
                problems.add("KEY_DIAGNOSIS_MISSING")
        else:
            if (
                observation.valid
                or observation.recomputed_verdict is not None
                or result.verdict is not None
                or result.diagnoses
                or observation.error_code not in OBSERVATION_ERROR_CODES
            ):
                raise JudgeError("DERIVED_RESULT_MISMATCH")
            problems.add("JUDGE_OBSERVATION_FAILED")
        # Keep actual false passes separate from errors. Errors and omissions
        # receive no agreement credit, including a failed negative observation.
        if item.split == "validation":
            effective = observation.recomputed_verdict if observation.valid else False
            compromised = not observation.valid or _missing_diagnosis(
                labels[item.item_id], observation
            )
            invalid_agreements += (
                compromised and effective == labels[item.item_id].verdict
            )
            verdicts.append((item.item_id, bool(effective)))
    counted = calibrate(
        {
            i.item_id: labels[i.item_id].verdict
            for i in pack.items
            if i.split == "validation"
        },
        verdicts,
        judge_config_hash=config_hash,
        calibration_pack_hash=pack_hash,
    )
    agreement = counted.agreement - invalid_agreements
    if agreement < 46:
        problems.add("AGREEMENT_BELOW_THRESHOLD")
    return replace(
        counted,
        agreement=agreement,
        passed=counted.passed and not problems,
        reasons=tuple(dict.fromkeys((*counted.reasons, *sorted(problems)))),
    )


def _selection(
    pack_hash: str, results: Mapping[str, CalibrationResult]
) -> SelectionRecord:
    eligible = tuple(p for p in ("zai", "openai") if results[p].passed)
    if (
        results["zai"].passed
        and results["zai"].agreement >= results["openai"].agreement
    ):
        status: Literal["selected", "pending_user_decision", "no_baseline"] = "selected"
        selected: str | None = "zai"
    else:
        status = "pending_user_decision" if eligible else "no_baseline"
        selected = None
    return SelectionRecord(status, selected, eligible, pack_hash, results)


def _check_candidates(pack: LabelPack, configs: Mapping[str, JudgeConfig]) -> None:
    if set(configs) != _PROVIDERS:
        raise JudgeError("CONFIG_INVALID")
    for provider, config in configs.items():
        _check_config(config)
        if config.provider != provider:
            raise JudgeError("JUDGE_IDENTITY_MISMATCH")
        if any(
            a.model_identity.strip().casefold()
            in (config.request_model.casefold(), config.response_model.casefold())
            for a in pack.annotations
        ):
            raise JudgeError("CALIBRATION_UNRESOLVED")


def run_calibration(
    pack: LabelPack,
    *,
    configs: Mapping[str, JudgeConfig],
    transports: Mapping[str, JudgeTransport],
    destination: Path,
) -> SelectionRecord:
    pack = _validated_pack(pack)
    labels = final_labels(pack)
    pack_hash = label_pack_hash(pack)
    _check_candidates(pack, configs)
    if set(transports) != _PROVIDERS:
        raise JudgeError("CONFIG_INVALID")
    # Claim destination before any paid call; restarting cannot overwrite history.
    _create_directory(destination)
    # Durable freeze precedes the first judge output, even for an in-memory pack.
    frozen_hash = freeze_label_pack(destination / "label-pack", pack)
    if frozen_hash != pack_hash:
        raise JudgeError("HASH_MISMATCH")
    configs = dict(configs)
    transports = dict(transports)
    observations: dict[str, tuple[CalibrationObservation, ...]] = {}
    results: dict[str, CalibrationResult] = {}
    for provider in ("zai", "openai", "deepseek"):
        entries: list[CalibrationObservation] = []
        for item in pack.items:
            for repetition in range(1, (2 if item.split == "validation" else 1) + 1):
                observation = judge_once(
                    item.request,
                    config=configs[provider],
                    calibration=None,
                    transport=transports[provider],
                )
                observation = replace(observation, phase="calibration")
                entry = CalibrationObservation(
                    item.item_id, item.input_hash, repetition, observation
                )
                # Persist each observation before proceeding, including failures.
                _write(
                    destination / f"{provider}-{len(entries) + 1:03d}.json",
                    canonical_bytes(entry),
                )
                entries.append(entry)
        observations[provider] = tuple(entries)
        results[provider] = _counts(
            pack, labels, configs[provider], observations[provider], pack_hash
        )
    selection = _selection(pack_hash, results)
    _write(
        destination / "calibration.json",
        canonical_bytes(
            {
                "schema": "opspilot-judge-calibration-1",
                "pack_hash": pack_hash,
                "configs": {
                    p: replace(c, api_key="withheld") for p, c in configs.items()
                },
                "observations": observations,
                "selection": selection,
            }
        ),
    )
    return selection


def _verify(
    raw: bytes,
    pack: LabelPack,
    config: JudgeConfig,
    directory: Path,
) -> tuple[SelectionRecord, CalibrationResult]:
    try:
        labels = final_labels(pack)
        pack_hash = label_pack_hash(pack)
        obj = exact(
            parse(raw), {"schema", "pack_hash", "configs", "observations", "selection"}
        )
        if obj["schema"] != "opspilot-judge-calibration-1":
            raise JudgeError("SCHEMA_INVALID")
        if obj["pack_hash"] != pack_hash:
            raise JudgeError("HASH_MISMATCH")
        configs = decode(Mapping[str, JudgeConfig], obj["configs"])
        _check_candidates(pack, configs)
        if any(c.api_key != "withheld" for c in configs.values()):
            raise JudgeError("SCHEMA_INVALID")
        if judge_config_hash(configs[config.provider]) != judge_config_hash(config):
            raise JudgeError("HASH_MISMATCH")
        cached_observations = decode(
            Mapping[str, tuple[CalibrationObservation, ...]], obj["observations"]
        )
        if set(cached_observations) != _PROVIDERS:
            raise JudgeError("SCHEMA_INVALID")
        frozen_pack = load_label_pack(
            directory / "label-pack", expected_pack_hash=pack_hash
        )
        if frozen_pack != pack:
            raise JudgeError("HASH_MISMATCH")
        observations: dict[str, tuple[CalibrationObservation, ...]] = {}
        for provider in ("zai", "openai", "deepseek"):
            entries: list[CalibrationObservation] = []
            for item in frozen_pack.items:
                for repetition in range(
                    1, (2 if item.split == "validation" else 1) + 1
                ):
                    entry = decode(
                        CalibrationObservation,
                        parse(
                            _read(directory / f"{provider}-{len(entries) + 1:03d}.json")
                        ),
                    )
                    if (entry.item_id, entry.input_hash, entry.repetition) != (
                        item.item_id,
                        item.input_hash,
                        repetition,
                    ):
                        raise JudgeError("IDENTITY_MISMATCH")
                    entries.append(entry)
            observations[provider] = tuple(entries)
        if cached_observations != observations:
            raise JudgeError("DERIVED_RESULT_MISMATCH")
        results = {
            p: _counts(pack, labels, configs[p], observations[p], pack_hash)
            for p in _PROVIDERS
        }
        selection = _selection(pack_hash, results)
        if decode(SelectionRecord, obj["selection"]) != selection:
            raise JudgeError("DERIVED_RESULT_MISMATCH")
        if (
            selection.status != "selected"
            or selection.selected_provider != config.provider
        ):
            raise JudgeError("CALIBRATION_UNRESOLVED")
        return selection, results[config.provider]
    except StorageError:
        pass
    raise JudgeError("SCHEMA_INVALID")


def load_verified_calibration(
    path: Path,
    *,
    pack: LabelPack,
    config: JudgeConfig,
) -> VerifiedCalibration:
    _check_config(config)
    raw, directory = _calibration_source(path)
    _check_directory(directory)
    selection, result = _verify(raw, pack, config, directory)
    return VerifiedCalibration(
        selection.pack_hash,
        judge_config_hash(config),
        config.provider,
        result,
        raw,
        _validated_pack(pack),
        directory,
    )


def calibration_matches(calibration: VerifiedCalibration, config: JudgeConfig) -> bool:
    try:
        if type(calibration) is not VerifiedCalibration:
            return False
        _check_config(config)
        current = _read(calibration._directory / "calibration.json")
        if parse(current) != parse(calibration._proof):
            return False
        selection, result = _verify(
            current, calibration._pack, config, calibration._directory
        )
        return (
            calibration.provider == config.provider
            and calibration.pack_hash == selection.pack_hash
            and calibration.config_hash == judge_config_hash(config)
            and calibration.result == result
            and result.passed
        )
    except (JudgeError, StorageError, ValueError, TypeError):
        return False
