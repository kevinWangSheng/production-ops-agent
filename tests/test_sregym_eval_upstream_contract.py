"""#667 PR-A contract, group 1: upstream pin (S-1) and the Lite list.

Order and codes from the task record: HEAD first (``UPSTREAM_HEAD_UNREADABLE``,
``UPSTREAM_HEAD_MISMATCH``), then each pinned file
(``UPSTREAM_FILE_MISSING:<path>``, ``UPSTREAM_FILE_MISMATCH:<path>``), then the
Lite list (``LITE_LIST_MISMATCH``). Tests that need the pinned commit skip
when the local upstream checkout is absent (CI).
"""

from __future__ import annotations

import hashlib
import shutil

import pytest

from tests.sregym_eval_support import (
    LITE_17,
    PINNED_COMMIT,
    PINNED_FILES,
    PROBLEM_SETS,
    code_of,
    git,
    mod,
    pinned_clone,
    upstream_root,
)


def _real():
    real = upstream_root()
    if real is None:
        pytest.skip("pinned upstream checkout not on this machine")
    return real


def test_pins_match_interface():
    up = mod("upstream")
    assert up.UPSTREAM_COMMIT == PINNED_COMMIT
    assert dict(up.PINNED_FILES) == PINNED_FILES


def test_lite_problems_are_the_17_upstream_ids_in_order():
    up = mod("upstream")
    assert tuple(up.LITE_PROBLEMS) == LITE_17
    assert len(set(up.LITE_PROBLEMS)) == 17


def test_problem_seq_is_one_based_position():
    up = mod("upstream")
    assert up.problem_seq(LITE_17[0]) == 1
    assert up.problem_seq(LITE_17[9]) == 10
    assert up.problem_seq(LITE_17[16]) == 17


def test_pinned_hashes_match_local_checkout():
    """Sanity of the pin itself against the local read-only clone."""
    real = _real()
    for rel, digest in PINNED_FILES.items():
        assert hashlib.sha256((real / rel).read_bytes()).hexdigest() == digest, rel
    assert git("rev-parse", "HEAD", cwd=real) == PINNED_COMMIT


# --- read_lite_problems ------------------------------------------------------

_SYNTH = '''"""synthetic problem sets"""

OTHER_PROBLEMS = (
    "zz_other_one",
)

SREGYM_LITE_PROBLEMS = (
    "zz_alpha_fault",  # a comment
    "zz_beta_fault",
    "zz_gamma_fault",
)

SREGYM_LITE_IA_PROBLEMS = (
    "zz_alpha_fault_bigapp",
    "zz_delta_fault_bigapp",
)
'''


def test_read_lite_problems_takes_only_the_lite_tuple():
    up = mod("upstream")
    assert tuple(up.read_lite_problems(_SYNTH)) == (
        "zz_alpha_fault",
        "zz_beta_fault",
        "zz_gamma_fault",
    )


def test_read_lite_problems_missing_list_is_lite_list_mismatch():
    up = mod("upstream")
    with pytest.raises(up.UpstreamMismatch) as ei:
        up.read_lite_problems('OTHER = ("a", "b")\n')
    assert code_of(ei.value) == "LITE_LIST_MISMATCH"


def test_read_lite_problems_on_real_file_equals_pin():
    real = _real()
    up = mod("upstream")
    text = (real / PROBLEM_SETS).read_text(encoding="utf-8")
    assert tuple(up.read_lite_problems(text)) == LITE_17


# --- verify_upstream order and codes ------------------------------------------


def test_verify_no_git_is_head_unreadable(tmp_path):
    up = mod("upstream")
    root = tmp_path / "not-a-repo"
    root.mkdir()
    with pytest.raises(up.UpstreamMismatch) as ei:
        up.verify_upstream(root)
    assert code_of(ei.value) == "UPSTREAM_HEAD_UNREADABLE"


def test_verify_wrong_head_checked_before_files(tmp_path):
    """Files absent and HEAD wrong: HEAD is reported, not a file."""
    up = mod("upstream")
    repo = tmp_path / "fake-upstream"
    repo.mkdir()
    (repo / "README").write_text("synthetic\n")
    git("init", "-q", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "synthetic", cwd=repo)
    assert git("rev-parse", "HEAD", cwd=repo) != PINNED_COMMIT
    with pytest.raises(up.UpstreamMismatch) as ei:
        up.verify_upstream(repo)
    assert code_of(ei.value) == "UPSTREAM_HEAD_MISMATCH"


def test_verify_wrong_head_with_correct_files_still_rejected(tmp_path):
    real = _real()
    up = mod("upstream")
    repo = tmp_path / "copy"
    for rel in PINNED_FILES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(real / rel, repo / rel)
    git("init", "-q", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "same bytes, other commit", cwd=repo)
    with pytest.raises(up.UpstreamMismatch) as ei:
        up.verify_upstream(repo)
    assert code_of(ei.value) == "UPSTREAM_HEAD_MISMATCH"


def test_verify_accepts_pinned_checkout(tmp_path):
    real = _real()
    up = mod("upstream")
    assert up.verify_upstream(real) is None
    assert up.verify_upstream(pinned_clone(real, tmp_path / "clone")) is None


@pytest.mark.parametrize("rel", sorted(PINNED_FILES))
def test_verify_missing_pinned_file(tmp_path, rel):
    real = _real()
    up = mod("upstream")
    clone = pinned_clone(real, tmp_path / "clone")
    (clone / rel).unlink()
    with pytest.raises(up.UpstreamMismatch) as ei:
        up.verify_upstream(clone)
    assert code_of(ei.value) == f"UPSTREAM_FILE_MISSING:{rel}"


@pytest.mark.parametrize("rel", sorted(PINNED_FILES))
def test_verify_tampered_pinned_file(tmp_path, rel):
    real = _real()
    up = mod("upstream")
    clone = pinned_clone(real, tmp_path / "clone")
    target = clone / rel
    # ASSUMPTION: the hash is over the checked-out file bytes.
    target.write_bytes(target.read_bytes() + b"\n# tampered\n")
    with pytest.raises(up.UpstreamMismatch) as ei:
        up.verify_upstream(clone)
    assert code_of(ei.value) == f"UPSTREAM_FILE_MISMATCH:{rel}"


@pytest.mark.parametrize("change", ["drop_one", "add_one"])
def test_verify_lite_list_off_by_one_is_rejected(tmp_path, monkeypatch, change):
    """Pre-review group 1: one problem more or less -> refused.

    A real change to problem_sets.py also changes its hash, so the list check
    is reached only when the file hash is re-pinned. ASSUMPTION: verify_upstream
    reads the module-level ``PINNED_FILES`` at call time.
    """
    real = _real()
    up = mod("upstream")
    clone = pinned_clone(real, tmp_path / "clone")
    target = clone / PROBLEM_SETS
    text = target.read_text(encoding="utf-8")
    if change == "drop_one":
        text = text.replace(f'    "{LITE_17[-1]}",\n', "", 1)
    else:
        text = text.replace(
            f'    "{LITE_17[-1]}",\n',
            f'    "{LITE_17[-1]}",\n    "zz_extra_problem",\n',
            1,
        )
    target.write_text(text, encoding="utf-8")
    pins = dict(PINNED_FILES)
    pins[PROBLEM_SETS] = hashlib.sha256(target.read_bytes()).hexdigest()
    monkeypatch.setattr(up, "PINNED_FILES", pins)
    with pytest.raises(up.UpstreamMismatch) as ei:
        up.verify_upstream(clone)
    assert code_of(ei.value) == "LITE_LIST_MISMATCH"
