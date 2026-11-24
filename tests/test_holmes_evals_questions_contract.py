"""#666 PR-A contract: question loading, upstream pin, B1 question lists,
judge mode (H-2, H-3, H-16 strict/loose selection).

Independent contract tests written from the task record's PR-A public
interface and the pre-review, without reading ``scripts/holmes_evals/``.
Red until the implementation lands; collection stays green (lazy import).

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* question directories live at ``<root>/tests/llm/fixtures/test_ask_holmes/
  <qid>/test_case.yaml`` (upstream layout), ``root`` being the checkout root;
* ``UpstreamMismatch`` is defined in ``questions``;
* ``Question`` may carry an identity field (``id``/``qid``/``folder``) besides
  the whitelist; the pre-review's longer whitelist (H-3 list:
  ``rerun_with_memory``, ``mocked_date``, ``forbidden_tools``, ``max_tokens``,
  ``frontend_tools``) is tolerated, nothing else is.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.holmes_evals_support import (
    CLASSIFIERS,
    FIXTURES,
    PINNED_CLASSIFIERS_SHA256,
    PINNED_COMMIT,
    mod,
    strings_in,
    upstream_root,
    write_question,
)

# Task record, 行为 / 答案隔离: the whitelist, upstream key names.
TASK_WHITELIST = frozenset(
    {
        "user_prompt",
        "tags",
        "toolsets",
        "before_test",
        "after_test",
        "setup_timeout",
        "port_forwards",
        "include_tool_calls",
        "memories_generated",
        "skip",
        "enable_todo",
        "evaluation",
        "cluster_name",
    }
)
# ASSUMPTION: tolerated, see module docstring.
PREREVIEW_EXTRA = frozenset(
    {
        "rerun_with_memory",
        "mocked_date",
        "forbidden_tools",
        "max_tokens",
        "frontend_tools",
    }
)
IDENTITY = frozenset({"id", "qid", "folder"})
ANSWER_KEYS = frozenset(
    {
        "expected_output",
        "expected_replay_output",
        "expected_skill_updates",
        "description",
    }
)

ANSWER_EXPECTED = [
    "SYNTH-EXPECTED-7f3a index app-x holds 42 shards",
    "SYNTH-EXPECTED-91bc the error rate peaked at 13%",
]
ANSWER_REPLAY = "SYNTH-REPLAY-55d0 replay answer"
ANSWER_SKILL_UPDATE = "SYNTH-SKILLUPD-0a1b"
ANSWER_DESCRIPTION = "SYNTH-DESCRIPTION-c42e what this question checks"
ANSWER_SENTINELS = (
    *ANSWER_EXPECTED,
    ANSWER_REPLAY,
    ANSWER_SKILL_UPDATE,
    ANSWER_DESCRIPTION,
)

QID = "183c_elasticsearch_log_search"


def _full_doc() -> dict:
    return {
        "user_prompt": "Synthetic: which service logged errors in app-183c-logs?",
        "description": ANSWER_DESCRIPTION,
        "tags": ["easy", "logs"],
        "before_test": "echo synthetic-setup",
        "after_test": "echo synthetic-cleanup",
        "setup_timeout": 420,
        "include_tool_calls": True,
        "memories_generated": False,
        "skip": False,
        "enable_todo": True,
        "cluster_name": "synthetic-cluster-a",
        "port_forwards": [{"namespace": "ns", "service": "svc", "port": 9200}],
        "expected_output": list(ANSWER_EXPECTED),
        "expected_replay_output": ANSWER_REPLAY,
        "expected_skill_updates": [ANSWER_SKILL_UPDATE],
    }


# --------------------------------------------------------------------------
# 1. Loader isolation (H-3)


def test_question_is_frozen_dataclass_with_whitelisted_fields_only(tmp_path):
    q_mod = mod("questions")
    write_question(tmp_path, QID, _full_doc())
    q = q_mod.load_question(tmp_path, QID)

    assert isinstance(q, q_mod.Question)
    assert dataclasses.is_dataclass(q)
    names = {f.name for f in dataclasses.fields(q)}
    assert not names & ANSWER_KEYS, names & ANSWER_KEYS
    assert names <= TASK_WHITELIST | PREREVIEW_EXTRA | IDENTITY, (
        names - TASK_WHITELIST - PREREVIEW_EXTRA - IDENTITY
    )
    assert "user_prompt" in names
    with pytest.raises(dataclasses.FrozenInstanceError):
        q.user_prompt = "changed"  # type: ignore[misc]


def test_question_class_declares_no_answer_field():
    q_mod = mod("questions")
    names = {f.name for f in dataclasses.fields(q_mod.Question)}
    assert not names & ANSWER_KEYS
    for key in ANSWER_KEYS:
        assert not hasattr(q_mod.Question, key), key


def test_answer_values_reach_no_attribute_dict_entry_or_repr(tmp_path):
    q_mod = mod("questions")
    write_question(tmp_path, QID, _full_doc())
    q = q_mod.load_question(tmp_path, QID)

    for key in ANSWER_KEYS:
        assert not hasattr(q, key), key
    reachable = strings_in(q)
    for key in ANSWER_KEYS:
        assert key not in reachable, f"{key} appears as a key inside Question"
    blob = "\n".join(reachable) + "\n" + repr(q) + "\n" + str(q)
    for secret in ANSWER_SENTINELS:
        assert secret not in blob, secret
        # Also not split across a joined rendering.
        assert secret.split(" ", 1)[0] not in blob, secret


def test_whitelisted_values_use_upstream_key_names_and_values(tmp_path):
    q_mod = mod("questions")
    doc = _full_doc()
    write_question(tmp_path, QID, doc)
    q = q_mod.load_question(tmp_path, QID)

    assert q.user_prompt == doc["user_prompt"]
    assert list(q.tags) == doc["tags"]
    assert q.before_test == doc["before_test"]
    assert q.after_test == doc["after_test"]
    assert q.setup_timeout == 420
    assert q.include_tool_calls is True
    assert q.memories_generated is False
    assert q.skip is False
    assert q.enable_todo is True
    assert q.cluster_name == "synthetic-cluster-a"
    forwards = [dict(x) for x in q.port_forwards]
    assert forwards == doc["port_forwards"]


def test_loader_keeps_answers_out_for_string_expected_output(tmp_path):
    q_mod = mod("questions")
    doc = _full_doc()
    doc["expected_output"] = ANSWER_EXPECTED[0]
    write_question(tmp_path, QID, doc)
    q = q_mod.load_question(tmp_path, QID)
    blob = "\n".join(strings_in(q)) + repr(q)
    assert ANSWER_EXPECTED[0] not in blob


# --------------------------------------------------------------------------
# 1b. Upstream pin (H-2)


def test_pinned_constants():
    q_mod = mod("questions")
    assert q_mod.UPSTREAM_COMMIT == PINNED_COMMIT
    assert q_mod.CLASSIFIERS_SHA256 == PINNED_CLASSIFIERS_SHA256


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=contract-test",
            "-c",
            "user.email=ct@example.invalid",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_verify_upstream_rejects_wrong_head(tmp_path):
    q_mod = mod("questions")
    repo = tmp_path / "fake-upstream"
    (repo / CLASSIFIERS).parent.mkdir(parents=True)
    real = upstream_root()
    if real is not None:
        # Same bytes as the pinned file, so only HEAD is wrong.
        shutil.copyfile(real / CLASSIFIERS, repo / CLASSIFIERS)
    else:
        (repo / CLASSIFIERS).write_text("# synthetic classifiers\n")
    _git("init", "-q", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "synthetic", cwd=repo)
    assert _git("rev-parse", "HEAD", cwd=repo) != PINNED_COMMIT

    with pytest.raises(q_mod.UpstreamMismatch):
        q_mod.verify_upstream(repo)


def test_verify_upstream_rejects_wrong_classifiers_hash(tmp_path):
    q_mod = mod("questions")
    real = upstream_root()
    if real is None:
        pytest.skip("pinned upstream checkout not on this machine")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--shared", "--no-checkout", str(real), str(clone)],
        check=True,
        capture_output=True,
    )
    # HEAD is the pinned commit; only classifiers.py is materialized.
    _git("update-ref", "--no-deref", "HEAD", PINNED_COMMIT, cwd=clone)
    _git("checkout", PINNED_COMMIT, "--", str(CLASSIFIERS), cwd=clone)
    assert _git("rev-parse", "HEAD", cwd=clone) == PINNED_COMMIT
    target = clone / CLASSIFIERS
    # ASSUMPTION: the hash is taken over the checked-out file.
    target.write_bytes(target.read_bytes() + b"\n# tampered\n")

    with pytest.raises(q_mod.UpstreamMismatch):
        q_mod.verify_upstream(clone)


def test_verify_upstream_accepts_pinned_checkout():
    q_mod = mod("questions")
    real = upstream_root()
    if real is None:
        pytest.skip("pinned upstream checkout not on this machine")
    assert q_mod.verify_upstream(real) is None


# --------------------------------------------------------------------------
# 2. Question lists (H-2, H-U1 list, R8-13)

EXPECTED_B1 = frozenset(
    {
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
    }
)
EXPECTED_MULTI_CLUSTER = frozenset(
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
EXPECTED_SKILL_DROPPED = frozenset(
    {
        "275_es_field_name_correction",
        "276_es_numeric_severity_field",
        "277_es_timestamp_keyword_field",
        "278_es_schema_discovery",
        "280_es_multi_quirk",
    }
)
#: #809 (depends on #808, B4-U7): the four questions B1-1 left out for lack
#: of upstream's elasticsearch/cluster tools are back in B1.
REINCLUDED_808 = frozenset(
    {
        "183a_elasticsearch_cluster_health",
        "183f_elasticsearch_shard_filtering",
        "183g_elasticsearch_index_stats",
        "186_elasticsearch_shard_explosion",
    }
)
EXCLUDED_PREFIXES = ("271", "274", "279")


def test_b1_questions_are_the_upstream_directories():
    q_mod = mod("questions")
    b1 = q_mod.B1_QUESTIONS
    assert isinstance(b1, tuple)
    assert len(b1) == 34
    assert len(set(b1)) == 34
    assert set(b1) == EXPECTED_B1 | REINCLUDED_808


def test_excluded_questions_absent():
    q_mod = mod("questions")
    for qid in q_mod.B1_QUESTIONS:
        number = qid.split("_", 1)[0]
        assert number not in EXCLUDED_PREFIXES, qid
    # 254 has two upstream directories; only the Elasticsearch one counts.
    assert "254_elasticsearch_dr_test_log_check" in q_mod.B1_QUESTIONS
    assert "254_mcp_oauth" not in q_mod.B1_QUESTIONS


def test_multi_cluster_and_skill_dropped_sets():
    q_mod = mod("questions")
    assert isinstance(q_mod.MULTI_CLUSTER, frozenset)
    assert isinstance(q_mod.SKILL_ASSERTION_DROPPED, frozenset)
    assert q_mod.MULTI_CLUSTER == EXPECTED_MULTI_CLUSTER
    assert len(q_mod.MULTI_CLUSTER) == 10
    assert q_mod.SKILL_ASSERTION_DROPPED == EXPECTED_SKILL_DROPPED
    assert q_mod.MULTI_CLUSTER <= set(q_mod.B1_QUESTIONS)
    assert q_mod.SKILL_ASSERTION_DROPPED <= set(q_mod.B1_QUESTIONS)


def test_b1_questions_exist_in_pinned_checkout():
    """Directory names only; no test_case.yaml is opened."""
    q_mod = mod("questions")
    real = upstream_root()
    if real is None:
        pytest.skip("pinned upstream checkout not on this machine")
    names = {p.name for p in (real / FIXTURES).iterdir() if p.is_dir()}
    missing = set(q_mod.B1_QUESTIONS) - names
    assert not missing, sorted(missing)


# --------------------------------------------------------------------------
# 3. judge_mode (H-16; upstream property_manager default strict)


@pytest.mark.parametrize(
    ("evaluation", "mode"),
    [
        (None, "strict"),
        ({"correctness": 1}, "strict"),
        ({"correctness": {"type": "strict", "expected_score": 1}}, "strict"),
        ({"correctness": {"type": "loose"}}, "loose"),
        ({"correctness": {"type": "loose", "expected_score": 1}}, "loose"),
    ],
)
def test_judge_mode(tmp_path, evaluation, mode):
    q_mod = mod("questions")
    doc = _full_doc()
    if evaluation is not None:
        doc["evaluation"] = evaluation
    write_question(tmp_path, QID, doc)
    q = q_mod.load_question(tmp_path, QID)
    assert q_mod.judge_mode(q) == mode
