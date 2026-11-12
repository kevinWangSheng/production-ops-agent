"""#295 B1 calibration tooling: blind pack, label merge, adjudication, compare.

Synthetic data only; no real reports or labels.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import eval_judge_recal as r

FAULT_Q = "A fault was injected in this scenario. Choose A only if ..."
NORMAL_Q = "No fault was injected ... Choose A only if ..."


def _item(aid, sid, system, glm, openai, text="report", **extra):
    return {
        "attempt_id": aid,
        "scenario_id": sid,
        "system": system,
        "slot": 1,
        "expected_elements": [f"element {len(sid)}"],
        "verdicts": {"glm": glm, "openai": openai},
        "report_text": text,
        **extra,
    }


def _sources():
    dev = {
        "schema": r.ITEMS_SCHEMA,
        "voided": ["v-1"],
        "attempts": [
            _item(
                "a-1",
                "dv2-aaa111",
                "opspilot",
                True,
                True,
                "OpsPilot found checkout on opspilot-m1",
            ),
            _item(
                "holmes-a2",
                "dv2-aaa111",
                "holmes",
                True,
                False,
                "see https://holmesgpt.dev/x",
            ),
            _item("a-3", "dv2-bbb222", "opspilot", False, False),
            _item("a-4", "dv2-bbb222", "opspilot", None, None),  # not judged
            _item("v-1", "dv2-bbb222", "opspilot", True, True),  # voided
        ],
    }
    holdout = {
        "schema": r.ITEMS_SCHEMA,
        "voided": [],
        "attempts": [_item("holmes-h1", "ho-ccc333", "holmes", True, True)],
    }
    return {"dev": dev, "holdout": holdout}


QUESTIONS = {
    ("dev", "dv2-aaa111"): {"question": FAULT_Q, "canary": None},
    ("dev", "dv2-bbb222"): {"question": NORMAL_Q, "canary": "CANARY-XYZ"},
    ("holdout", "ho-ccc333"): {"question": FAULT_Q, "canary": None},
}


def _pack(seed=b"\x01" * 32):
    return r.build_pack(
        _sources(),
        QUESTIONS,
        {"holmes-a2": "fail"},
        {"a-1": True, "holmes-a2": False, "a-3": False, "holmes-h1": True},
        dataset_names=["dev-v2", "holdout-7ec15a", "holdout"],
        seed=seed,
    )


def test_pack_holds_only_judged_live_items_without_identity_or_verdicts():
    pack, key, hits = _pack()
    assert hits == []
    assert pack["count"] == 4 == key["count"]
    assert {e["attempt_id"] for e in key["items"].values()} == {
        "a-1",
        "holmes-a2",
        "a-3",
        "holmes-h1",
    }
    for item in pack["items"]:
        assert set(item) <= {
            "id",
            "question",
            "expected_elements",
            "output",
            "injected_canary",
        }
    text = json.dumps(pack).lower()
    for word in (
        "opspilot",
        "holmes",
        "dv2-",
        "ho-ccc333",
        "glm",
        "openai",
        "verdict",
        "dev",
        "holdout",
    ):
        assert word not in text
    assert "[name removed]" in json.dumps(pack)


def test_key_keeps_identity_pipeline_and_prior_ruling():
    _pack_, key, _ = _pack()
    by_aid = {e["attempt_id"]: e for e in key["items"].values()}
    assert by_aid["a-1"]["pipeline"] is True
    assert by_aid["a-3"]["pipeline"] is False
    assert by_aid["holmes-a2"]["pipeline"] is None
    assert by_aid["holmes-a2"]["user_ruling"] == "fail"
    assert by_aid["a-1"]["redactions"] == 2
    assert (
        by_aid["a-3"]["dataset"] == "dev"
        and by_aid["holmes-h1"]["dataset"] == "holdout"
    )


def test_canary_only_on_items_whose_scenario_has_one():
    pack, key, _ = _pack()
    for item in pack["items"]:
        sid = key["items"][item["id"]]["scenario_id"]
        assert ("injected_canary" in item) == (sid == "dv2-bbb222")


def test_shuffle_depends_on_seed_and_ids_are_sequential():
    p1, k1, _ = _pack(b"\x01" * 32)
    p2, k2, _ = _pack(b"\x02" * 32)
    assert [i["id"] for i in p1["items"]] == ["R001", "R002", "R003", "R004"]
    order1 = [k1["items"][i]["attempt_id"] for i in sorted(k1["items"])]
    order2 = [k2["items"][i]["attempt_id"] for i in sorted(k2["items"])]
    assert sorted(order1) == sorted(order2)
    again = _pack(b"\x01" * 32)[1]
    assert [again["items"][i]["attempt_id"] for i in sorted(again["items"])] == order1


def test_leak_is_reported_when_a_term_survives_redaction():
    sources = _sources()
    sources["dev"]["attempts"][2]["report_text"] = "mentions dv2-aaa111 here"
    _p, _k, hits = r.build_pack(
        sources, QUESTIONS, {}, {}, dataset_names=["dev-v2"], seed=b"\0" * 32
    )
    assert "dv2-aaa111" in hits


def test_unknown_scenario_is_refused():
    with pytest.raises(r.RecalError, match="SCENARIO_UNKNOWN"):
        r.build_pack(_sources(), {}, {}, {}, dataset_names=[], seed=b"\0" * 32)


def _labels(name, values):
    return {"schema": r.LABELS_SCHEMA, "labeller": name, "labels": values}


def _ids(key, aid):
    return next(i for i, e in key["items"].items() if e["attempt_id"] == aid)


def test_merge_excludes_prior_rulings_from_adjudication():
    _p, key, _ = _pack()
    a = {i: "pass" for i in key["items"]}
    b = dict(a)
    ruled, other = _ids(key, "holmes-a2"), _ids(key, "a-3")
    b[ruled] = "fail"
    b[other] = "fail"
    merged = r.merge_labels(key, [_labels("A", a), _labels("B", b)])
    assert merged["complete"] is True
    assert sorted(merged["disagreed"]) == sorted([ruled, other])
    assert merged["to_adjudicate"] == [other]
    assert merged["labeller_agreement"] == {"agreed": 2, "n": 4}


def test_merge_stops_on_missing_or_invalid_labels():
    _p, key, _ = _pack()
    a = {i: "pass" for i in key["items"]}
    b = dict(a)
    first = sorted(b)[0]
    b[first] = "maybe"
    merged = r.merge_labels(key, [_labels("A", a), _labels("B", b)])
    assert merged == {
        "schema": r.MERGED_SCHEMA,
        "complete": False,
        "invalid": {"B": [first]},
    }
    with pytest.raises(r.RecalError, match="LABEL_ID_UNKNOWN"):
        r.merge_labels(key, [_labels("A", a | {"R999": "pass"}), _labels("B", a)])
    with pytest.raises(r.RecalError, match="LABELLERS_NOT_DISTINCT"):
        r.merge_labels(key, [_labels("A", a), _labels("A", a)])


def test_brief_has_no_judge_or_labeller_opinion():
    pack, key, _ = _pack()
    ids = [_ids(key, "a-3")]
    brief = r.adjudication_brief(pack, ids, "INSTRUCTIONS")
    assert ids[0] in brief and "INSTRUCTIONS" in brief and "CANARY-XYZ" in brief
    for word in ("glm", "openai", "verdict", "labeller A", "a-3", "dv2-"):
        assert word not in brief
    others = [i for i in key["items"] if i not in ids]
    assert all(f"## {i}" not in brief for i in others)


def _key(rows):
    """rows: (blind id, pipeline, user_ruling, verdicts)"""
    return {
        "items": {
            bid: {
                "attempt_id": f"att-{bid}",
                "dataset": "dev",
                "system": "s",
                "scenario_id": "x",
                "slot": 1,
                "pipeline": pipe,
                "user_ruling": ruling,
                "verdicts": verdicts,
                "published_pass": (ruling == "pass") if ruling else pipe,
                "invalid": False,
            }
            for bid, pipe, ruling, verdicts in rows
        }
    }


def _flow_key(n_pass, n_fail, n_ruled=1):
    rows = []
    for k in range(n_pass):
        rows.append((f"P{k:03d}", True, None, {"glm": True, "openai": True}))
    for k in range(n_fail):
        rows.append((f"F{k:03d}", False, None, {"glm": False, "openai": False}))
    for k in range(n_ruled):
        rows.append((f"U{k:03d}", None, "fail", {"glm": True, "openai": False}))
    return _key(rows)


THRESHOLD = {"denominator": 102, "min_agree": 98, "max_false_pass": 0, "max_invalid": 0}


def _gold(key, flips=()):
    gold = {}
    for i, e in key["items"].items():
        if e["user_ruling"] is not None:
            gold[i] = {"label": e["user_ruling"], "source": "prior_ruling"}
            continue
        label = "pass" if e["pipeline"] else "fail"
        if i in flips:
            label = "fail" if label == "pass" else "pass"
        gold[i] = {"label": label, "source": "blind"}
    return gold


def test_threshold_boundary_on_false_fails():
    key = _flow_key(85, 17, 9)
    four = [f"F{k:03d}" for k in range(4)]
    res = r.compare(key, _gold(key, four), THRESHOLD)
    assert res["flow"]["agree"] == 98 and res["flow"]["false_fail"] == 4
    assert res["flow"]["achieved"] is True
    res = r.compare(key, _gold(key, four + ["F004"]), THRESHOLD)
    assert res["flow"]["agree"] == 97 and res["flow"]["achieved"] is False


def test_one_false_pass_fails_the_threshold():
    key = _flow_key(85, 17, 9)
    res = r.compare(key, _gold(key, ["P000"]), THRESHOLD)
    assert res["flow"]["agree"] == 101
    assert res["flow"]["false_pass"] == 1 and res["flow"]["achieved"] is False
    assert res["flow"]["false_pass_items"][0]["attempt_id"] == "att-P000"
    assert res["published_overturned"][0]["attempt_id"] == "att-P000"


def test_invalid_output_fails_and_ruled_items_stay_out_of_denominator():
    key = _flow_key(85, 17, 9)
    key["items"]["P001"]["invalid"] = True
    res = r.compare(key, _gold(key), THRESHOLD)
    assert res["flow"]["n"] == 102 and res["flow"]["invalid"] == 1
    assert res["flow"]["achieved"] is False
    assert len(res["reference"]["prior_rulings_non_blind"]) == 9
    with pytest.raises(r.RecalError, match="DENOMINATOR_DIFFERS"):
        r.compare(_flow_key(85, 16, 9), _gold(_flow_key(85, 16, 9)), THRESHOLD)


def test_per_judge_reference_counts_single_judge_false_passes():
    key = _flow_key(85, 17, 9)
    res = r.compare(key, _gold(key), THRESHOLD)
    assert res["reference"]["per_judge"]["glm"]["false_pass"] == 9
    assert res["reference"]["per_judge"]["openai"]["false_pass"] == 0
    assert res["reference"]["per_judge"]["glm"]["n"] == 111


def test_gold_labels_sources_and_adjudication_checks():
    key = _key(
        [
            ("R001", True, None, {}),
            ("R002", False, None, {}),
            ("R003", None, "pass", {}),
        ]
    )
    merged = {"complete": True, "agreed": {"R001": "fail"}, "to_adjudicate": ["R002"]}
    adj = {"schema": r.ADJUDICATION_SCHEMA, "rulings": {"R002": "pass"}}
    gold = r.gold_labels(key, merged, adj)
    assert gold == {
        "R001": {"label": "fail", "source": "blind"},
        "R002": {"label": "pass", "source": "user"},
        "R003": {"label": "pass", "source": "prior_ruling"},
    }
    with pytest.raises(r.RecalError, match="ADJUDICATION_INCOMPLETE"):
        r.gold_labels(
            key, merged, {"schema": r.ADJUDICATION_SCHEMA, "rulings": {"R002": None}}
        )
    with pytest.raises(r.RecalError, match="ADJUDICATION_IDS_DIFFER"):
        r.gold_labels(key, merged, {"schema": r.ADJUDICATION_SCHEMA, "rulings": {}})
    with pytest.raises(r.RecalError, match="LABELS_INCOMPLETE"):
        r.gold_labels(key, {"complete": False}, adj)


def test_recompute_replaces_only_judged_attempts():
    key = _key([("R001", True, None, {})])
    published = {
        "per_scenario": [
            {
                "dataset": "dev",
                "system": "s",
                "attempts": [
                    {"attempt_id": "att-R001", "pass": True},
                    {"attempt_id": "q", "pass": False},
                ],
            }
        ]
    }
    gold = {"R001": {"label": "fail", "source": "blind"}}
    assert r.recompute(published, key, gold) == {
        "dev": {"s": {"published": 1, "recomputed": 0, "n": 2}}
    }


def test_freeze_rejects_a_changed_pack(tmp_path: Path):
    pack, key, _ = _pack()
    manifest = r.write_pack(tmp_path / "pack", pack, key, b"instructions\n")
    freeze = {
        "schema": r.FREEZE_SCHEMA,
        "pack_files": manifest["files"],
        "threshold": THRESHOLD,
    }
    path = tmp_path / "freeze.json"
    path.write_text(json.dumps(freeze))
    assert r.load_freeze(path, tmp_path / "pack")["threshold"] == THRESHOLD
    (tmp_path / "pack/pack/instructions.md").write_text("changed\n")
    with pytest.raises(r.RecalError, match="PACK_NOT_FROZEN:pack/instructions.md"):
        r.load_freeze(path, tmp_path / "pack")


def test_write_pack_refuses_to_overwrite(tmp_path: Path):
    pack, key, _ = _pack()
    r.write_pack(tmp_path, pack, key, b"x")
    with pytest.raises(FileExistsError):
        r.write_pack(tmp_path, pack, key, b"x")


def test_labels_and_compare_cli_end_to_end(tmp_path: Path):
    pack, key, _ = _pack()
    manifest = r.write_pack(tmp_path / "p", pack, key, b"INSTR\n")
    freeze = tmp_path / "freeze.json"
    freeze.write_text(
        json.dumps(
            {
                "schema": r.FREEZE_SCHEMA,
                "pack_files": manifest["files"],
                "threshold": {
                    "denominator": 3,
                    "min_agree": 3,
                    "max_false_pass": 0,
                    "max_invalid": 0,
                },
            }
        )
    )
    a = {i: ("pass" if e["pipeline"] else "fail") for i, e in key["items"].items()}
    b = dict(a)
    flip = _ids(key, "a-3")
    b[flip] = "pass"
    (tmp_path / "a.json").write_text(json.dumps(_labels("A", a)))
    (tmp_path / "b.json").write_text(json.dumps(_labels("B", b)))
    out = tmp_path / "labels"
    assert (
        r.main(
            [
                "labels",
                "--pack",
                str(tmp_path / "p"),
                "--freeze",
                str(freeze),
                "--label",
                str(tmp_path / "a.json"),
                "--label",
                str(tmp_path / "b.json"),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    adj = json.loads((out / "adjudication.json").read_text())
    assert list(adj["rulings"]) == [flip]
    adj["rulings"][flip] = "fail"
    (out / "adjudication.json").write_text(json.dumps(adj))
    result = tmp_path / "result.json"
    assert (
        r.main(
            [
                "compare",
                "--pack",
                str(tmp_path / "p"),
                "--freeze",
                str(freeze),
                "--merged",
                str(out / "merged.json"),
                "--adjudication",
                str(out / "adjudication.json"),
                "--out",
                str(result),
            ]
        )
        == 0
    )
    doc = json.loads(result.read_text())
    assert doc["flow"] == doc["flow"] | {
        "n": 3,
        "agree": 3,
        "false_pass": 0,
        "achieved": True,
    }
    assert "report" not in result.read_text()
