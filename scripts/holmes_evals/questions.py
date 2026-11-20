"""Question list, upstream pin and the answer-free question loader (#666 H-2, H-3).

Answer isolation (R8-6): :func:`load_question` returns only the whitelisted
upstream keys. ``expected_output``, ``expected_replay_output``,
``expected_skill_updates`` and ``description`` never leave this module's
loader; the only reader of ``expected_output`` is
:func:`scripts.holmes_evals.judge.judge_trial`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

#: HolmesGPT commit the question set and classifier prompts are pinned to.
UPSTREAM_COMMIT = "a0009195468ebc55a7dff7d602b61feabe3cda1b"
#: sha256 of ``tests/llm/utils/classifiers.py`` at :data:`UPSTREAM_COMMIT`.
CLASSIFIERS_SHA256 = "66ebe528abf1d7507f1373aee055b9ba12a86de89bc79c016abc2ba8d2ced22c"
CLASSIFIERS_PATH = Path("tests/llm/utils/classifiers.py")
#: Where the ask-holmes question directories live inside the checkout.
QUESTIONS_DIR = Path("tests/llm/fixtures/test_ask_holmes")
TEST_CASE_FILE = "test_case.yaml"

#: B1: the 30 Elasticsearch questions (decision table r8/r9; prereview §1).
#: Not included: 183a, 183f, 183g, 186, 271, 274, 279.
B1_QUESTIONS: tuple[str, ...] = (
    "183b_elasticsearch_index_discovery",
    "183c_elasticsearch_log_search",
    "183d_elasticsearch_aggregation",
    "183e_elasticsearch_field_mappings",
    "184_elasticsearch_index_explosion",
    "185_elasticsearch_cross_region_search",
    "187_elasticsearch_disk_space",
    "188_elasticsearch_mapping_explosion",
    "189_elasticsearch_timeseries_gap",
    "190_elasticsearch_cross_service_correlation",
    "191_elasticsearch_query_profile",
    "193_elasticsearch_large_mapping_search",
    "235_elasticsearch_cluster_mismatch",
    "245_elasticsearch_trace_large_fields",
    "254_elasticsearch_dr_test_log_check",
    "259_wrong_cluster_logs_confusion",
    "260_global_es_remote_cluster_logs",
    "261_time_window_gap_external_data",
    "262_ambiguous_cluster_reference",
    "263_region_suffixed_service_siblings",
    "264_wrong_env_same_region",
    "265_multicluster_labeling_discipline",
    "266_toolset_disabled_vs_no_data",
    "267_cluster_name_alias",
    "269_mixed_sources_route_to_external",
    "275_es_field_name_correction",
    "276_es_numeric_severity_field",
    "277_es_timestamp_keyword_field",
    "278_es_schema_discovery",
    "280_es_multi_quirk",
)
#: The 10 multi-cluster questions (upstream ``cluster_name``; H-U1, H-18).
MULTI_CLUSTER: frozenset[str] = frozenset(
    {
        "259_wrong_cluster_logs_confusion",
        "260_global_es_remote_cluster_logs",
        "261_time_window_gap_external_data",
        "262_ambiguous_cluster_reference",
        "263_region_suffixed_service_siblings",
        "264_wrong_env_same_region",
        "265_multicluster_labeling_discipline",
        "266_toolset_disabled_vs_no_data",
        "267_cluster_name_alias",
        "269_mixed_sources_route_to_external",
    }
)
#: R8-13 / H-15: SuggestSkills count assertion dropped, no ``# Suggested
#: Skills`` block for the judge, no ``rerun_with_memory`` replay.
SKILL_ASSERTION_DROPPED: frozenset[str] = frozenset(
    {
        "275_es_field_name_correction",
        "276_es_numeric_severity_field",
        "277_es_timestamp_keyword_field",
        "278_es_schema_discovery",
        "280_es_multi_quirk",
    }
)
#: All 6 R8-13 questions across tracks: the B1 five plus C5's
#: ``282_loki_custom_stream_label`` (C5a, #569). The B1 set above stays the B1
#: subset its contract pins; tracks beyond B1 read this one.
R8_13_SKILL_ASSERTION_DROPPED: frozenset[str] = SKILL_ASSERTION_DROPPED | {
    "282_loki_custom_stream_label"
}

#: C5 (#569, PR-C): log search questions; each brings its own backend.
C5_LOKI_QUESTIONS: tuple[str, ...] = (
    "100a_loki_historical_logs",
    "101_loki_historical_logs_pod_deleted",
    "102_loki_label_discovery",
    "102a_loki_logs_transparency",
    "102b_loki_multiple_pods",
    "259_loki_historical_logs_pod_deleted_docker",
    "282_loki_custom_stream_label",
)
C5_VICTORIALOGS_QUESTIONS: tuple[str, ...] = (
    "257_victorialogs_log_search",
    "258_victorialogs_streams",
)
C5_QUESTIONS: tuple[str, ...] = C5_LOKI_QUESTIONS + C5_VICTORIALOGS_QUESTIONS
#: Every question the drivers accept (B1 first, the order rounds run in).
ALL_QUESTIONS: tuple[str, ...] = B1_QUESTIONS + C5_QUESTIONS
#: ``--questions`` group names.
QUESTION_GROUPS: dict[str, tuple[str, ...]] = {"b1": B1_QUESTIONS, "c5": C5_QUESTIONS}


class UpstreamMismatch(RuntimeError):
    """The checkout is not the pinned upstream; carries a fixed code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _git_dir(root: Path) -> Path:
    dot_git = root / ".git"
    if dot_git.is_file():
        # A worktree or submodule: ``gitdir: <path>``.
        line = dot_git.read_text(encoding="utf-8").strip()
        if not line.startswith("gitdir:"):
            raise UpstreamMismatch("UPSTREAM_HEAD_UNREADABLE")
        target = Path(line[len("gitdir:") :].strip())
        return target if target.is_absolute() else (root / target)
    return dot_git


def _head_commit(root: Path) -> str:
    """HEAD's commit id, read from the git files (no subprocess)."""
    git_dir = _git_dir(root)
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        raise UpstreamMismatch("UPSTREAM_HEAD_UNREADABLE") from None
    if not head.startswith("ref:"):
        return head
    ref = head[len("ref:") :].strip()
    # A linked worktree keeps refs in the common dir.
    common = git_dir
    commondir = git_dir / "commondir"
    if commondir.is_file():
        common = (git_dir / commondir.read_text(encoding="utf-8").strip()).resolve()
    for base in (git_dir, common):
        loose = base / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    raise UpstreamMismatch("UPSTREAM_HEAD_UNREADABLE")


def verify_upstream(root: Path) -> None:
    """Refuse a checkout whose HEAD or classifiers.py differs from the pin."""
    if _head_commit(root) != UPSTREAM_COMMIT:
        raise UpstreamMismatch("UPSTREAM_HEAD_MISMATCH")
    try:
        digest = hashlib.sha256((root / CLASSIFIERS_PATH).read_bytes()).hexdigest()
    except OSError:
        raise UpstreamMismatch("UPSTREAM_CLASSIFIERS_MISSING") from None
    if digest != CLASSIFIERS_SHA256:
        raise UpstreamMismatch("UPSTREAM_CLASSIFIERS_MISMATCH")


@dataclass(frozen=True)
class Question:
    """Whitelisted upstream keys only; attribute names are the upstream keys.

    There is no answer field. Absent keys take upstream's defaults
    (``include_tool_calls`` and ``enable_todo`` False, the rest None).
    """

    user_prompt: str | list[str]
    tags: list[str] | None = None
    toolsets: dict[str, Any] | None = None
    before_test: str | None = None
    after_test: str | None = None
    setup_timeout: int | None = None
    port_forwards: list[dict[str, Any]] | None = None
    include_tool_calls: bool = False
    memories_generated: bool | None = None
    skip: bool | None = None
    enable_todo: bool = False
    evaluation: dict[str, Any] | None = None
    cluster_name: str | None = None


WHITELIST: tuple[str, ...] = tuple(Question.__dataclass_fields__)
_FALSE_DEFAULTS = frozenset({"include_tool_calls", "enable_todo"})


def question_dir(root: Path, qid: str) -> Path:
    """The question's directory; ``qid`` must be one plain directory name."""
    if not qid or qid in {".", ".."} or "/" in qid or "\\" in qid or "\0" in qid:
        raise ValueError("QUESTION_ID_INVALID")
    return root / QUESTIONS_DIR / qid


def read_test_case(root: Path, qid: str) -> Mapping[str, Any]:
    """The whole parsed ``test_case.yaml``. Answer-bearing: callers keep only
    whitelisted keys (:func:`load_question`) or ``expected_output`` inside the
    judging function."""
    import yaml  # type: ignore[import-untyped]

    path = question_dir(root, qid) / TEST_CASE_FILE
    with path.open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    if not isinstance(doc, Mapping):
        raise ValueError("TEST_CASE_NOT_A_MAPPING")
    return doc


def load_question(root: Path, qid: str) -> Question:
    doc = read_test_case(root, qid)
    if "user_prompt" not in doc:
        raise ValueError("USER_PROMPT_MISSING")
    kept: dict[str, Any] = {}
    for key in WHITELIST:
        value = doc.get(key)
        if value is None and key in _FALSE_DEFAULTS:
            value = False
        kept[key] = value
    del doc
    return Question(**kept)


def judge_mode(q: Question) -> Literal["strict", "loose"]:
    """Upstream ``property_manager.py``: ``evaluation.correctness.type`` when
    ``correctness`` is an Evaluation object, otherwise strict."""
    evaluation = q.evaluation
    if not isinstance(evaluation, Mapping):
        return "strict"
    correctness = evaluation.get("correctness")
    if not isinstance(correctness, Mapping):
        return "strict"
    mode = correctness.get("type")
    if mode == "loose":
        return "loose"
    if mode == "strict":
        return "strict"
    raise ValueError("EVALUATION_TYPE_INVALID")
