"""Independent contract tests for the C6c content check (#570): the second
read-only layer of ``mongodb_find`` and ``mongodb_aggregate``.

MongoDB has no read-only session or transaction, so a write can only be
stopped by the account (first layer, checked by C6-U1) and by the tool:
a fixed command face plus this content check on the only model-written
parts -- ``filter``, ``projection``, ``sort`` and ``pipeline``.

Written from the C6 task record (docs/tasks/2026-11-20-tool-expansion-c6.md,
「C6c 公开接口」→「只读：两道加工具命令面」), PRODUCT-CONSTRAINTS line 18 and
the C6 pre-review (C6-5), without reading the implementation. Red until C6c
lands; collection stays green.

Every refusal must happen before any command: the recording connector
(``tests/c6c_mongo_support.py``) receives nothing, not even the account
check. Every accepted query must reach ``find``/``aggregate`` with exactly
the parsed (relaxed extended JSON) value, plus the tool's own ``$limit``.

Codes: ``QUERY_CONSTRUCT_NOT_ALLOWED`` (forbidden key at any depth, stage
outside the whitelist, cross-database lookup), ``INVALID_QUERY`` (empty,
over 20000 characters, not an object/array, nested over 30 levels, a stage
object with more than one key), ``INVALID_JSON`` (does not parse). Where the
record leaves two codes possible for one input, the test accepts both.
Assumptions beyond the record are marked ``# ASSUMPTION:``.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.c6c_mongo_support import (
    AGGREGATE,
    FIND,
    MONGO_DB,
    make_tools,
    refused_before_request,
    view,
)

QC = "QUERY_CONSTRUCT_NOT_ALLOWED"
IQ = "INVALID_QUERY"
IJ = "INVALID_JSON"

FUNCTION = {"body": "function() { return true; }", "args": [], "lang": "js"}
ACCUMULATOR = {
    "init": "function() { return 0; }",
    "accumulate": "function(s) { return s + 1; }",
    "accumulateArgs": [],
    "merge": "function(a, b) { return a + b; }",
    "lang": "js",
}


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("OPSPILOT_MONGODB") or re.fullmatch(
            r"OPSPILOT_[A-Z0-9_]*DSN", name
        ):
            monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C6C_TEST_K8S_CA_FILE", str(ca_file))


def _j(value: object) -> str:
    return json.dumps(value)


def _nest(depth: int, leaf: object) -> dict:
    """``depth`` nested ``{"a": ...}`` objects around ``leaf``. The record
    caps nesting at 30 levels without fixing how levels are counted, so the
    tests stay clear of the boundary: 20 must pass, 40 must not."""
    value: object = leaf
    for _ in range(depth):
        value = {"a": value}
    assert isinstance(value, dict)
    return value


def _deep_where(depth: int) -> str:
    return _j(_nest(depth, {"$where": "1"}))


# == find: forbidden keys at any depth ============================================

FILTER_REFUSED = {
    "top-where": '{"$where": "this.total > 1"}',
    "where-sleep": '{"$where": "sleep(10000) || true"}',
    "where-code-value": '{"$where": {"$code": "function() { return true; }"}}',
    "code-ext-json": '{"total": {"$code": "function() { return 1; }"}}',
    "code-with-scope": '{"total": {"$code": "x", "$scope": {"y": 1}}}',
    "code-in-array": '{"total": {"$in": [{"$code": "1"}]}}',
    "expr-function": _j({"$expr": {"$function": FUNCTION}}),
    "expr-eq-function": _j({"$expr": {"$eq": [{"$function": FUNCTION}, True]}}),
    "expr-accumulator": _j({"$expr": {"$accumulator": ACCUMULATOR}}),
    "or-where": '{"$or": [{"a": 1}, {"$where": "1"}]}',
    "nor-elemmatch-where": '{"$and": [{"$nor": [{"b": {"$elemMatch": {"$where": "1"}}}]}]}',
    "not-where": '{"a": {"$not": {"$where": "1"}}}',
    "in-where": '{"a": {"$in": [{"$where": "1"}]}}',
    "unicode-escaped-dollar": '{"\\u0024where": "1"}',
    "unicode-escaped-letter": '{"$wh\\u0065re": "1"}',
    "unicode-escaped-function": _j({"$expr": {"x": 1}}).replace(
        '"x"', '"\\u0024function"'
    ),
    "deep-where-20": _deep_where(20),
    "out-key-in-filter": '{"$out": "stolen"}',
    "merge-key-in-filter": '{"a": {"$merge": "stolen"}}',
    "accumulator-in-filter": _j({"a": {"$accumulator": ACCUMULATOR}}),
}


@pytest.mark.parametrize("case", sorted(FILTER_REFUSED))
def test_find_filter_with_a_forbidden_key_is_refused(monkeypatch, case):
    tools = make_tools(monkeypatch)
    outcome = tools.find(FILTER_REFUSED[case])
    refused_before_request(outcome, tools.driver, QC)


PROJECTION_REFUSED = {
    "function": _j({"x": {"$function": FUNCTION}}),
    "accumulator": _j({"x": {"$accumulator": ACCUMULATOR}}),
    "where": '{"x": {"$where": "1"}}',
    "code": '{"x": {"$code": "function() { return 1; }"}}',
    "nested-function": _j({"x": {"$cond": [True, {"$function": FUNCTION}, 0]}}),
}


@pytest.mark.parametrize("case", sorted(PROJECTION_REFUSED))
def test_find_projection_with_a_forbidden_key_is_refused(monkeypatch, case):
    tools = make_tools(monkeypatch)
    outcome = tools.find("{}", projection=PROJECTION_REFUSED[case])
    refused_before_request(outcome, tools.driver, QC)


SORT_REFUSED = {
    # The record: sort values are only 1, -1 or {"$meta": ...}; the forbidden
    # keys are refused at any depth as well. Which code wins is not fixed.
    "function": (_j({"x": {"$function": FUNCTION}}), (QC, IQ)),
    "where": ('{"x": {"$where": "1"}}', (QC, IQ)),
    "two": ('{"x": 2}', (IQ, QC, "INVALID_PARAMS")),
    "string": ('{"x": "asc"}', (IQ, QC, "INVALID_PARAMS")),
    "zero": ('{"x": 0}', (IQ, QC, "INVALID_PARAMS")),
    "object": ('{"x": {"a": 1}}', (IQ, QC, "INVALID_PARAMS")),
}


@pytest.mark.parametrize("case", sorted(SORT_REFUSED))
def test_find_sort_outside_one_minus_one_and_meta_is_refused(monkeypatch, case):
    text, codes = SORT_REFUSED[case]
    tools = make_tools(monkeypatch)
    outcome = tools.find("{}", sort=text)
    refused_before_request(outcome, tools.driver, *codes)


# == aggregate: forbidden keys, nested pipelines, the stage whitelist ==============

PIPELINE_REFUSED = {
    # $out / $merge at the top and in every nested pipeline.
    "out": '[{"$out": "stolen"}]',
    "out-after-match": '[{"$match": {}}, {"$out": {"db": "admin", "coll": "x"}}]',
    "merge-string": '[{"$merge": "stolen"}]',
    "merge-object": '[{"$merge": {"into": "stolen", "whenMatched": "replace"}}]',
    "facet-out": '[{"$facet": {"a": [{"$out": "stolen"}]}}]',
    "facet-second-branch-merge": (
        '[{"$facet": {"a": [{"$match": {}}], "b": [{"$merge": "stolen"}]}}]'
    ),
    "lookup-pipeline-out": (
        '[{"$lookup": {"from": "customers", "pipeline": [{"$out": "stolen"}], '
        '"as": "j"}}]'
    ),
    "lookup-let-pipeline-merge": (
        '[{"$lookup": {"from": "customers", "let": {"c": "$c"}, '
        '"pipeline": [{"$merge": "stolen"}], "as": "j"}}]'
    ),
    "unionwith-pipeline-out": (
        '[{"$unionWith": {"coll": "archive", "pipeline": [{"$out": "stolen"}]}}]'
    ),
    "unionwith-facet-lookup-merge": _j(
        [
            {
                "$unionWith": {
                    "coll": "archive",
                    "pipeline": [
                        {
                            "$facet": {
                                "a": [
                                    {
                                        "$lookup": {
                                            "from": "customers",
                                            "pipeline": [{"$merge": "stolen"}],
                                            "as": "j",
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                }
            }
        ]
    ),
    "facet-lookup-unionwith-out": _j(
        [
            {
                "$facet": {
                    "a": [
                        {
                            "$lookup": {
                                "from": "customers",
                                "pipeline": [
                                    {
                                        "$unionWith": {
                                            "coll": "archive",
                                            "pipeline": [{"$out": "stolen"}],
                                        }
                                    }
                                ],
                                "as": "j",
                            }
                        }
                    ]
                }
            }
        ]
    ),
    # $out / $merge as a key below the stage level.
    "out-inside-expr": '[{"$match": {"$expr": {"$out": 1}}}]',
    "merge-inside-replacewith": '[{"$replaceWith": {"$merge": 1}}]',
    # Server-side JavaScript in any stage.
    "match-where": '[{"$match": {"$where": "1"}}]',
    "match-expr-function": _j([{"$match": {"$expr": {"$function": FUNCTION}}}]),
    "addfields-function": _j([{"$addFields": {"x": {"$function": FUNCTION}}}]),
    "group-accumulator": _j(
        [{"$group": {"_id": None, "x": {"$accumulator": ACCUMULATOR}}}]
    ),
    "project-literal-code": '[{"$project": {"x": {"$literal": {"$code": "1"}}}}]',
    "set-code": '[{"$set": {"x": {"$code": "function() {}"}}}]',
    "window-function": _j(
        [{"$setWindowFields": {"output": {"x": {"$function": FUNCTION}}}}]
    ),
    "lookup-pipeline-where": (
        '[{"$lookup": {"from": "customers", "pipeline": '
        '[{"$match": {"$where": "1"}}], "as": "j"}}]'
    ),
    "unionwith-pipeline-function": _j(
        [
            {
                "$unionWith": {
                    "coll": "archive",
                    "pipeline": [{"$addFields": {"x": {"$function": FUNCTION}}}],
                }
            }
        ]
    ),
    "facet-accumulator": _j(
        [
            {
                "$facet": {
                    "a": [{"$group": {"_id": 1, "x": {"$accumulator": ACCUMULATOR}}}]
                }
            }
        ]
    ),
    "graphlookup-restrict-where": _j(
        [
            {
                "$graphLookup": {
                    "from": "employees",
                    "startWith": "$boss",
                    "connectFromField": "boss",
                    "connectToField": "name",
                    "as": "chain",
                    "restrictSearchWithMatch": {"$where": "1"},
                }
            }
        ]
    ),
    "bucket-output-function": _j(
        [
            {
                "$bucket": {
                    "groupBy": "$total",
                    "boundaries": [0, 10],
                    "output": {"x": {"$function": FUNCTION}},
                }
            }
        ]
    ),
    "deep-where-20-in-match": _j([{"$match": _nest(20, {"$where": "1"})}]),
    "unicode-escaped-out": '[{"\\u0024out": "stolen"}]',
    "unicode-escaped-merge-letter": '[{"$m\\u0065rge": "stolen"}]',
    # Cross-database forms of $lookup, $graphLookup and $unionWith.
    "lookup-from-object": _j(
        [
            {
                "$lookup": {
                    "from": {"db": "admin", "coll": "system.users"},
                    "localField": "a",
                    "foreignField": "b",
                    "as": "j",
                }
            }
        ]
    ),
    "lookup-db-key": _j(
        [
            {
                "$lookup": {
                    "from": "users",
                    "db": "admin",
                    "localField": "a",
                    "foreignField": "b",
                    "as": "j",
                }
            }
        ]
    ),
    "lookup-from-number": _j(
        [{"$lookup": {"from": 1, "localField": "a", "foreignField": "b", "as": "j"}}]
    ),
    "graphlookup-from-object": _j(
        [
            {
                "$graphLookup": {
                    "from": {"db": "admin", "coll": "system.users"},
                    "startWith": "$a",
                    "connectFromField": "a",
                    "connectToField": "b",
                    "as": "j",
                }
            }
        ]
    ),
    "graphlookup-db-key": _j(
        [
            {
                "$graphLookup": {
                    "from": "users",
                    "db": "admin",
                    "startWith": "$a",
                    "connectFromField": "a",
                    "connectToField": "b",
                    "as": "j",
                }
            }
        ]
    ),
    "unionwith-coll-object": _j(
        [{"$unionWith": {"coll": {"db": "admin", "coll": "system.users"}}}]
    ),
    "unionwith-db-key": '[{"$unionWith": {"coll": "users", "db": "admin"}}]',
    "unionwith-db-key-with-pipeline": (
        '[{"$unionWith": {"db": "local", "coll": "oplog.rs", "pipeline": []}}]'
    ),
    "facet-lookup-cross-db": _j(
        [
            {
                "$facet": {
                    "a": [
                        {
                            "$lookup": {
                                "from": {"db": "local", "coll": "oplog.rs"},
                                "localField": "a",
                                "foreignField": "b",
                                "as": "j",
                            }
                        }
                    ]
                }
            }
        ]
    ),
    "lookup-pipeline-unionwith-cross-db": _j(
        [
            {
                "$lookup": {
                    "from": "customers",
                    "pipeline": [{"$unionWith": {"coll": "x", "db": "admin"}}],
                    "as": "j",
                }
            }
        ]
    ),
}


@pytest.mark.parametrize("case", sorted(PIPELINE_REFUSED))
def test_pipeline_with_a_forbidden_construct_is_refused(monkeypatch, case):
    tools = make_tools(monkeypatch)
    outcome = tools.aggregate(PIPELINE_REFUSED[case])
    refused_before_request(outcome, tools.driver, QC)


NOT_WHITELISTED = (
    "$documents",
    "$currentOp",
    "$collStats",
    "$indexStats",
    "$planCacheStats",
    "$listSessions",
    "$listLocalSessions",
    "$changeStream",
    "$changeStreamSplitLargeEvent",
    "$search",
    "$searchMeta",
    "$vectorSearch",
    "$listSearchIndexes",
    "$shardedDataDistribution",
    "$querySettings",
    "$queryStats",
    "$listSampledQueries",
    "$listClusterCatalog",
    "$listCatalog",
    # Operators are case-sensitive; only the exact whitelist spellings pass.
    "$MATCH",
    "$Match",
    "$Out",
    "$OUT",
    "$Merge",
    "match",
    "$match ",
    "$",
    "",
)

_STAGE_ARGUMENT = {"$documents": [{"x": 1}], "$changeStream": {}}


def _stage(name: str) -> dict:
    return {name: _STAGE_ARGUMENT.get(name, {})}


@pytest.mark.parametrize("stage", NOT_WHITELISTED)
def test_a_stage_outside_the_whitelist_is_refused_at_the_top(monkeypatch, stage):
    tools = make_tools(monkeypatch)
    outcome = tools.aggregate(_j([{"$match": {}}, _stage(stage)]))
    # A stage name that is not even an operator may also read as bad shape.
    codes = (QC, IQ) if stage in ("", "$", "$match ", "match") else (QC,)
    refused_before_request(outcome, tools.driver, *codes)


NESTINGS = {
    "facet": lambda s: [{"$facet": {"a": [{"$match": {}}], "b": [s]}}],
    "lookup": lambda s: [
        {"$lookup": {"from": "customers", "pipeline": [s], "as": "j"}}
    ],
    "unionwith": lambda s: [{"$unionWith": {"coll": "archive", "pipeline": [s]}}],
    "facet-in-lookup": lambda s: [
        {
            "$lookup": {
                "from": "customers",
                "pipeline": [{"$facet": {"a": [s]}}],
                "as": "j",
            }
        }
    ],
}


@pytest.mark.parametrize("where", sorted(NESTINGS))
@pytest.mark.parametrize(
    "stage", ("$documents", "$currentOp", "$collStats", "$indexStats", "$search")
)
def test_a_stage_outside_the_whitelist_is_refused_in_every_nested_pipeline(
    monkeypatch, where, stage
):
    tools = make_tools(monkeypatch)
    outcome = tools.aggregate(_j(NESTINGS[where](_stage(stage))))
    refused_before_request(outcome, tools.driver, QC)


# == shape: empty, length, depth, JSON, stage objects ===============================

FILTER_SHAPE = {
    "empty": ("", (IQ, IJ)),
    "blank": ("   ", (IQ, IJ)),
    "array": ("[]", (IQ,)),
    "string": ('"orders"', (IQ,)),
    "null": ("null", (IQ,)),
    "number": ("1", (IQ,)),
    "unquoted-key": ("{status: 1}", (IJ,)),
    "single-quotes": ("{'status': 1}", (IJ,)),
    "truncated": ('{"status": 1', (IJ,)),
    "trailing-document": ('{"a": 1} {"b": 2}', (IJ,)),
    "trailing-comma": ('{"a": 1,}', (IJ,)),
    "undefined": ('{"a": undefined}', (IJ,)),
    "comment": ('{/* x */ "a": 1}', (IJ,)),
    "shell-objectid": ('{"_id": ObjectId("65f0c0ffee00000000000001")}', (IJ,)),
    "bad-oid": ('{"_id": {"$oid": "not-hex"}}', (IJ,)),
    "bad-date": ('{"at": {"$date": "yesterday"}}', (IJ,)),
    "bad-number-long": ('{"n": {"$numberLong": "five"}}', (IJ,)),
    "too-long": ('{"note": "' + "x" * 20_000 + '"}', (IQ,)),
    "too-deep": (_j(_nest(40, 1)), (IQ,)),
    # ASSUMPTION (not in the record; the lead asked for it): a JSON object
    # with a repeated key is refused rather than silently keeping one value.
    "duplicate-key": ('{"a": 1, "a": 2}', (IJ, IQ)),
    "duplicate-key-hiding-where": ('{"a": {"$where": "1"}, "a": 1}', (IJ, IQ, QC)),
}


@pytest.mark.parametrize("case", sorted(FILTER_SHAPE))
def test_find_filter_shape_is_checked_before_any_command(monkeypatch, case):
    text, codes = FILTER_SHAPE[case]
    tools = make_tools(monkeypatch)
    outcome = tools.find(text)
    refused_before_request(outcome, tools.driver, *codes)


@pytest.mark.parametrize(
    ("param", "text", "codes"),
    [
        ("projection", "[]", (IQ,)),
        ("projection", "{bad}", (IJ,)),
        ("projection", '{"a": "' + "y" * 20_000 + '"}', (IQ,)),
        ("sort", "[]", (IQ,)),
        ("sort", "{bad}", (IJ,)),
        ("sort", _j(_nest(40, 1)), (IQ,)),
    ],
)
def test_projection_and_sort_shape_is_checked(monkeypatch, param, text, codes):
    tools = make_tools(monkeypatch)
    outcome = tools.find("{}", **{param: text})
    refused_before_request(outcome, tools.driver, *codes)


PIPELINE_SHAPE = {
    "empty": ("", (IQ, IJ)),
    "object": ('{"$match": {}}', (IQ,)),
    "empty-object": ("{}", (IQ,)),
    "string": ('"$match"', (IQ,)),
    "stage-is-string": ('["$match"]', (IQ, QC)),
    "stage-is-number": ("[1]", (IQ, QC)),
    "stage-is-array": ('[[{"$match": {}}]]', (IQ, QC)),
    "stage-is-empty-object": ("[{}]", (IQ, QC)),
    "stage-with-two-keys": ('[{"$match": {}, "$project": {"a": 1}}]', (IQ,)),
    "stage-with-two-keys-hiding-out": ('[{"$match": {}, "$out": "stolen"}]', (IQ, QC)),
    "facet-stage-with-two-keys": (
        '[{"$facet": {"a": [{"$match": {}, "$limit": 1}]}}]',
        (IQ,),
    ),
    "lookup-stage-with-two-keys": (
        '[{"$lookup": {"from": "c", "pipeline": [{"$match": {}, "$skip": 1}], '
        '"as": "j"}}]',
        (IQ,),
    ),
    "invalid-json": ("[{$match: {}}]", (IJ,)),
    "truncated": ('[{"$match": {}}', (IJ,)),
    "too-long": (_j([{"$match": {"note": "z" * 20_000}}]), (IQ,)),
    "too-deep": (_j([{"$match": _nest(40, 1)}]), (IQ,)),
}


@pytest.mark.parametrize("case", sorted(PIPELINE_SHAPE))
def test_pipeline_shape_is_checked_before_any_command(monkeypatch, case):
    text, codes = PIPELINE_SHAPE[case]
    tools = make_tools(monkeypatch)
    outcome = tools.aggregate(text)
    refused_before_request(outcome, tools.driver, *codes)


def test_the_length_limit_is_20000_characters(monkeypatch):
    pytest.importorskip("bson")
    from bson import json_util

    prefix, suffix = '{"note": "', '"}'
    text = prefix + "x" * (20_000 - len(prefix) - len(suffix)) + suffix
    assert len(text) == 20_000
    tools = make_tools(monkeypatch)
    outcome = tools.find(text)
    assert outcome.status in ("ok", "no_data"), view(outcome)
    [find] = [s for s in tools.driver.sent if s.name == "find"]
    assert find.doc["filter"] == json_util.loads(text)
    tools.driver.reset()
    refused_before_request(tools.find(text[:-2] + 'x"}'), tools.driver, IQ)


# == what passes: legal queries reach the server unchanged ===========================

FILTER_ACCEPTED = {
    "empty": "{}",
    "comparison": '{"status": "paid", "total": {"$gte": 10}}',
    "oid": '{"_id": {"$oid": "65f0c0ffee00000000000001"}}',
    "date": '{"at": {"$gte": {"$date": "2026-11-20T00:00:00Z"}}}',
    "number-long": '{"n": {"$numberLong": "5"}}',
    "number-decimal": '{"price": {"$numberDecimal": "9.99"}}',
    "regex-operator": '{"name": {"$regex": "^cust", "$options": "i"}}',
    "regular-expression": (
        '{"name": {"$regularExpression": {"pattern": "^c", "options": ""}}}'
    ),
    "expr": '{"$expr": {"$gt": ["$total", 10]}}',
    "elem-match": '{"tags": {"$elemMatch": {"$eq": "x"}}}',
    "exists-in": '{"a": {"$exists": true}, "b": {"$in": [1, 2, 3]}}',
    # $search is a stage name; inside $text it is the query operator.
    "text-search": '{"$text": {"$search": "checkout"}}',
    # The forbidden words as values, as keys without "$", or not at the start.
    "where-as-value": '{"note": "$where"}',
    "words-in-value": '{"note": "uses $out, $merge, $function and $accumulator"}',
    "code-as-value": '{"note": "$code"}',
    "plain-keys": '{"where": 1, "out": 2, "merge": 3, "code": 4, "function": 5}',
    "dollar-inside-key": '{"price$where": 1}',
    "deep-20": _j(_nest(20, 1)),
}


@pytest.mark.parametrize("case", sorted(FILTER_ACCEPTED))
def test_legal_filters_are_sent_as_parsed(monkeypatch, case):
    pytest.importorskip("bson")
    from bson import json_util

    text = FILTER_ACCEPTED[case]
    tools = make_tools(monkeypatch)
    outcome = tools.find(text)
    assert outcome.status in ("ok", "no_data"), view(outcome)
    [find] = [s for s in tools.driver.sent if s.name == "find"]
    assert find.database == MONGO_DB
    assert find.doc["filter"] == json_util.loads(text)


@pytest.mark.parametrize(
    ("projection", "sort"),
    [
        ('{"name": 1, "_id": 0}', '{"at": -1, "name": 1}'),
        ('{"score": {"$meta": "textScore"}}', '{"score": {"$meta": "textScore"}}'),
        ('{"items": {"$slice": 3}}', '{"_id": 1}'),
    ],
)
def test_legal_projection_and_sort_are_sent_as_parsed(monkeypatch, projection, sort):
    pytest.importorskip("bson")
    from bson import json_util

    tools = make_tools(monkeypatch)
    outcome = tools.find(
        '{"$text": {"$search": "x"}}', projection=projection, sort=sort
    )
    assert outcome.status in ("ok", "no_data"), view(outcome)
    [find] = [s for s in tools.driver.sent if s.name == "find"]
    assert find.doc["projection"] == json_util.loads(projection)
    assert find.doc["sort"] == json_util.loads(sort)


PIPELINE_ACCEPTED = {
    "match-group-sort": _j(
        [
            {"$match": {"status": "paid"}},
            {"$group": {"_id": "$customer_id", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ]
    ),
    "lookup-localfield": _j(
        [
            {
                "$lookup": {
                    "from": "customers",
                    "localField": "customer_id",
                    "foreignField": "_id",
                    "as": "c",
                }
            },
            {"$unwind": "$c"},
        ]
    ),
    "lookup-pipeline": _j(
        [
            {
                "$lookup": {
                    "from": "customers",
                    "let": {"cid": "$customer_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$_id", "$$cid"]}}},
                        {"$project": {"name": 1}},
                    ],
                    "as": "c",
                }
            }
        ]
    ),
    "graphlookup": _j(
        [
            {
                "$graphLookup": {
                    "from": "employees",
                    "startWith": "$boss",
                    "connectFromField": "boss",
                    "connectToField": "name",
                    "as": "chain",
                }
            }
        ]
    ),
    "unionwith-pipeline": _j(
        [{"$unionWith": {"coll": "archive", "pipeline": [{"$match": {}}]}}]
    ),
    "facet": _j(
        [{"$facet": {"a": [{"$count": "n"}], "b": [{"$sortByCount": "$status"}]}}]
    ),
    "bucket": _j(
        [{"$bucket": {"groupBy": "$total", "boundaries": [0, 10, 100], "default": "x"}}]
    ),
    "bucket-auto": _j([{"$bucketAuto": {"groupBy": "$total", "buckets": 3}}]),
    "window": _j(
        [{"$setWindowFields": {"sortBy": {"at": 1}, "output": {"r": {"$rank": {}}}}}]
    ),
    "densify-fill": _j(
        [
            {
                "$densify": {
                    "field": "at",
                    "range": {"step": 1, "unit": "hour", "bounds": "full"},
                }
            },
            {"$fill": {"output": {"n": {"value": 0}}}},
        ]
    ),
    "geo-near": _j(
        [
            {
                "$geoNear": {
                    "near": {"type": "Point", "coordinates": [0, 0]},
                    "distanceField": "d",
                }
            }
        ]
    ),
    "every-other-stage": _j(
        [
            {"$sample": {"size": 5}},
            {"$skip": 1},
            {"$limit": 1000},
            {"$unset": "x"},
            {"$set": {"y": 1}},
            {"$addFields": {"z": 1}},
            {"$replaceRoot": {"newRoot": {"c": "$c"}}},
            {"$replaceWith": {"c": "$c"}},
            {"$redact": "$$KEEP"},
            {"$project": {"a": 1}},
            {"$count": "n"},
        ]
    ),
    "words-as-values": _j([{"$match": {"note": "$out $merge $where $function"}}]),
    "ext-json-values": (
        '[{"$match": {"at": {"$gte": {"$date": "2026-11-20T00:00:00Z"}}, '
        '"_id": {"$oid": "65f0c0ffee00000000000001"}}}, '
        '{"$group": {"_id": null, "n": {"$sum": {"$numberLong": "1"}}}}]'
    ),
    "deep-20": _j([{"$match": _nest(20, 1)}]),
}


@pytest.mark.parametrize("case", sorted(PIPELINE_ACCEPTED))
def test_legal_pipelines_are_sent_as_parsed_with_the_tools_limit_last(
    monkeypatch, case
):
    pytest.importorskip("bson")
    from bson import json_util

    text = PIPELINE_ACCEPTED[case]
    tools = make_tools(monkeypatch)
    outcome = tools.aggregate(text, limit=20)
    assert outcome.status in ("ok", "no_data"), view(outcome)
    [aggregate] = [s for s in tools.driver.sent if s.name == "aggregate"]
    assert aggregate.database == MONGO_DB
    pipeline = list(aggregate.doc["pipeline"])
    assert pipeline[:-1] == json_util.loads(text)
    assert dict(pipeline[-1]) == {"$limit": 21}


def test_refusal_names_the_construct_it_hit(monkeypatch):
    tools = make_tools(monkeypatch)
    outcome = tools.aggregate('[{"$facet": {"a": [{"$merge": "stolen"}]}}]')
    refused_before_request(outcome, tools.driver, QC)
    assert "$merge" in " ".join(str(n) for n in view(outcome).get("notes") or [])
    tools.driver.reset()
    outcome = tools.aggregate('[{"$collStats": {}}]')
    refused_before_request(outcome, tools.driver, QC)
    assert "$collStats" in " ".join(str(n) for n in view(outcome).get("notes") or [])


def test_find_and_aggregate_take_their_query_only_as_json_strings(monkeypatch):
    # ASSUMPTION: the schema types filter/projection/sort/pipeline as JSON
    # strings (record: 「JSON 对象字符串」「JSON 数组字符串」); a structured value
    # never bypasses the check -- it is refused or checked the same way.
    tools = make_tools(monkeypatch)
    outcome = tools.run(FIND, {"collection": "orders", "filter": {"$where": "1"}})
    assert outcome.status in ("error", "denied"), view(outcome)
    outcome = tools.run(
        AGGREGATE, {"collection": "orders", "pipeline": [{"$out": "stolen"}]}
    )
    assert outcome.status in ("error", "denied"), view(outcome)
    assert tools.driver.sent == []
