"""F1 scoring corrections approved 2026-11-05 (S1 code set, S2 Prometheus POST).

Ruling: ``~/.agent/handoffs/opspilot-rulings-2026-11-04-f1-closure.md``,
last bullet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import eval_dev_round
from scripts.holmes_baseline import dataset as ds
from tests.unit.test_432_impl import DEV_V2, NOISE_NEG, NOISE_POS, _dims, _write_round

A, B, C, X = "a" * 40, "b" * 40, "c" * 40, "d" * 40
SAME_TREE = {A: "t1", B: "t1", C: "t1", X: "t1"}


def _trees(trees: dict[str, str]):
    def lookup(commit: str) -> tuple[str, str] | None:
        full = (
            next((k for k in trees if k.startswith(commit)), None) if commit else None
        )
        return None if full is None else (full, trees[full])

    return lookup


def _code(commit: str) -> dict[str, Any]:
    return {**_dims(), "code": {"kind": "version", "value": commit, "reason": None}}


def _two_rounds(tmp_path: Path, first: str, second: str) -> list[Path]:
    return [
        _write_round(tmp_path, "dv2-aaaaaa", [(NOISE_POS, "ran")], _code(first)),
        _write_round(
            tmp_path,
            "dv2-bbbbbb",
            [(NOISE_NEG, "ran"), (NOISE_NEG, "ran")],
            _code(second),
        ),
    ]


# --- S1: MANIFEST_DRIFT:code


def test_registered_set_with_identical_product_scope_is_accepted_and_disclosed(
    tmp_path: Path,
) -> None:
    dirs = _two_rounds(tmp_path, A, B)
    doc = eval_dev_round.aggregate(
        dirs,
        DEV_V2,
        code_commits=["aaaa", "bbbb", "cccc"],
        product_tree=_trees(SAME_TREE),
    )
    assert doc["code_set"] == {
        "scope": "opspilot/",
        "product_tree": "t1",
        "registered": [A, B, C],
        "attempts_by_commit": {A: 1, B: 2},
    }


def test_commit_outside_the_registered_set_is_drift(tmp_path: Path) -> None:
    dirs = _two_rounds(tmp_path, A, X)
    with pytest.raises(eval_dev_round.DevRoundError, match="^MANIFEST_DRIFT:code$"):
        eval_dev_round.aggregate(
            dirs, DEV_V2, code_commits=[A, B], product_tree=_trees(SAME_TREE)
        )


def test_product_difference_within_the_registered_set_is_drift(tmp_path: Path) -> None:
    dirs = _two_rounds(tmp_path, A, B)
    with pytest.raises(eval_dev_round.DevRoundError, match="^MANIFEST_DRIFT:code$"):
        eval_dev_round.aggregate(
            dirs,
            DEV_V2,
            code_commits=[A, B],
            product_tree=_trees({A: "t1", B: "t2"}),
        )


def test_unresolvable_or_unknown_code_is_drift(tmp_path: Path) -> None:
    dirs = _two_rounds(tmp_path, A, B)
    with pytest.raises(eval_dev_round.DevRoundError, match="^MANIFEST_DRIFT:code$"):
        eval_dev_round.aggregate(
            dirs, DEV_V2, code_commits=[A, B, "e" * 40], product_tree=_trees(SAME_TREE)
        )
    dirty = {**_dims(), "code": {"kind": "unknown", "value": None, "reason": "dirty"}}
    r3 = _write_round(tmp_path, "dv2-cccccc", [(NOISE_POS, "ran")], dirty)
    with pytest.raises(eval_dev_round.DevRoundError, match="^MANIFEST_DRIFT:code$"):
        eval_dev_round.aggregate(
            [dirs[1], r3], DEV_V2, code_commits=[A, B], product_tree=_trees(SAME_TREE)
        )


def test_without_a_registered_set_code_drift_and_output_are_unchanged(
    tmp_path: Path,
) -> None:
    dirs = _two_rounds(tmp_path, A, B)
    with pytest.raises(eval_dev_round.DevRoundError, match="^MANIFEST_DRIFT:code$"):
        eval_dev_round.aggregate(dirs, DEV_V2, product_tree=_trees(SAME_TREE))
    same = _two_rounds(tmp_path / "same", A, A)
    doc = eval_dev_round.aggregate(same, DEV_V2)
    assert "code_set" not in doc and doc["code"] == A


def test_cli_registers_the_set_with_repeated_code_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(eval_dev_round, "git_product_tree", _trees(SAME_TREE))
    dirs = _two_rounds(tmp_path, A, B)
    out = tmp_path / "out.json"
    args = ["--dataset", str(DEV_V2), "--n", "2"]
    args += [a for d in dirs for a in ("--round", str(d))]
    registered = ["--code-commit", A, "--code-commit", B]
    assert eval_dev_round.main([*args, "--out", str(out), *registered]) == 0
    assert json.loads(out.read_text())["code_set"]["attempts_by_commit"] == {A: 1, B: 2}
    capsys.readouterr()
    assert eval_dev_round.main([*args, "--out", str(tmp_path / "o2.json")]) == 1
    assert "MANIFEST_DRIFT:code" in capsys.readouterr().out


def test_git_product_tree_resolves_this_repository() -> None:
    head = eval_dev_round.git_product_tree("HEAD")
    assert head is not None and len(head[0]) == 40 and len(head[1]) == 40
    assert eval_dev_round.git_product_tree("no-such-commit") is None


# --- S2: Q11 Holmes read_only


def _line(method: str, uri: str) -> str:
    return f"2026-11-05T00:00:00+00:00 holmes {method} {uri} 200 10 20 0.01"


@pytest.mark.parametrize(
    "uri",
    [
        "/api/v1/query",
        "/api/v1/query_range",
        "/api/v1/query?query=up",
        "/api/v1/query_range?x=1",
        "http://prometheus-proxy:8080/api/v1/query",
        "http://prometheus-proxy:8080/api/v1/query_range?x=1",
    ],
)
def test_post_to_prometheus_query_is_a_read(uri: str) -> None:
    assert ds.read_only({}, [_line("POST", uri)])


@pytest.mark.parametrize(
    "uri",
    [
        "/api/v1/admin/tsdb/delete_series",
        "/api/v1/admin/tsdb/snapshot",
        "/api/v1/query_exemplars",
        "/api/v1/query/",
        "/api/v1/queryx",
        "/api/v1/series",
        "/x/api/v1/query",
        "http://prometheus-proxy:8080/api/v1/admin/tsdb/clean_tombstones",
    ],
)
def test_post_elsewhere_is_a_write(uri: str) -> None:
    assert not ds.read_only({}, [_line("POST", uri)])


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_other_methods_are_writes_even_on_query_paths(method: str) -> None:
    assert not ds.read_only({}, [_line(method, "/api/v1/query")])


def test_get_and_other_accounts_and_jaeger_rule_unchanged() -> None:
    assert ds.read_only({}, [_line("GET", "/api/v1/admin/tsdb/snapshot")])
    other = "t opspilot DELETE /api/v1/admin 200 1 2 0.1"
    assert ds.read_only({}, [other])
    post_jaeger = {"tool_calls": [{"description": "HTTP POST http://j/api/v1/query"}]}
    assert not ds.read_only(post_jaeger, [_line("POST", "/api/v1/query")])
