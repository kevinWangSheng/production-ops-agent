"""Independent #701 contract tests: retry feedback names every failing check.

Approved default B4-16 (docs/tasks/2026-11-22-next-batches-b4-prereview.md):
the forced-final retry feedback lists, for each claim, every citation check
that claim fails, not just the first. Which reports are accepted or rejected
does not change; only the feedback content does. The feedback stays the fixed
REPORT_RETRY_TEMPLATE with C2's closed reason vocabulary and never carries
view or claim content.

Interpretation choices (not prescribed by B4-16):

- A check that fails only because an earlier one did -- there is no
  delivered view to compare against (every cited id unknown), or the
  time_scope_ref is not a policy at all -- is not a second failure. A claim
  whose only problem is a truncated id therefore keeps main's exact bytes.
- Reasons are listed in the gate's existing check order.
- Wording of how several reasons for one claim are joined is not fixed;
  tests check names, order and bytes only where main already defines them.

All tests go through ``validated_report``, the single function the live
round and the forced-final rebuild (#304) both call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from opspilot.investigation import reports
from opspilot.investigation.reports import DeliveredView, validated_report
from tests.m1_tool_support import historical_window_context

REPO = Path(__file__).resolve().parents[1]
TARGETS = frozenset({"checkout-prod"})
POLICY = "policy-window-1"
OK_ID = "metrics.query-t0:d1"
NO_DATA_ID = "logs.search-t1:d2"
UNBOUND_ID = "traces.find-t2:d3"
VIEWS = (
    DeliveredView(OK_ID, TARGETS, "ok", frozenset({POLICY}), True),
    DeliveredView(NO_DATA_ID, TARGETS, "no_data", frozenset({POLICY}), False),
    # ok but not citable, and delivered under no time policy.
    DeliveredView(UNBOUND_ID, TARGETS, "ok", frozenset(), False),
)

PREFIX = "The previous reply was not accepted as the final report (REPORT_INVALID)."
SUFFIX = (
    " This is the forced final retry: return exactly one corrected json object "
    "in the report format already given, citing only evidence_id values "
    "already delivered in this transcript."
)
# Main's REPORT_RETRY_TEMPLATE (067d2c18). It is what ``prompt_revision``
# hashes; #701 changes only the per-claim detail, not the template.
MAIN_TEMPLATE = (
    "The previous reply was not accepted as the final report ({reason})."
    "{detail} This is the forced final retry: return exactly one corrected "
    "json object in the report format already given, citing only "
    "evidence_id values already delivered in this transcript."
)


def _claim(ids, refs=("checkout-prod",), scope=POLICY, text="claim text"):
    return {
        "kind": "fact",
        "text": text,
        "evidence_ids": list(ids),
        "target_refs": list(refs),
        "time_scope_ref": scope,
    }


def _report(*claims):
    return {
        "schema_version": "m0-report-v3",
        "assessment_status": "completed",
        "conclusion": "partial",
        "summary": "summary",
        "claims": list(claims),
        "gaps": ["gap"],
        "next_steps": [],
        "root_cause": None,
    }


def _validate(payload, *, context=None):
    return validated_report(
        json.dumps(payload),
        finish_reason="stop",
        delivered=VIEWS,
        evidence_context=context or historical_window_context("run-701"),
        authorized_targets=TARGETS,
    )


def _in_order(text, names):
    positions = [text.find(name) for name in names]
    assert all(p >= 0 for p in positions), (names, text)
    assert positions == sorted(positions), (names, text)


# The r3 S105 trial 2 shape: a truncated id (`<op>-t0`, no `:<dispatch>`)
# plus an unauthorized extra target_ref on the same claim.
S701 = _report(
    _claim(["metrics.query-t0"], refs=("checkout-prod", "otel-demo/frontend"))
)
# Three independent failures on delivered views: a no_data view, and an ok
# view that is not citable and not bound to the claim's time policy.
THREE = _report(_claim([NO_DATA_ID, UNBOUND_ID]))


def test_701_truncated_id_and_extra_target_ref_are_both_reported():
    report, reason, feedback = _validate(S701)
    assert report is None and reason == "REPORT_INVALID"
    assert feedback.startswith(PREFIX) and feedback.endswith(SUFFIX)
    _in_order(feedback, ["claim 0", "unknown_evidence_id", "missing_target_refs"])
    # The #686 D6 hints for both checks are still given.
    assert "metrics.query-t0 -> metrics.query-t0:d1" in feedback
    assert "otel-demo/frontend" in feedback
    # No delivered view was cited, so "not observed by view" is not a
    # separate failure; and only one claim exists.
    assert "target_not_observed_by_view" not in feedback
    assert "claim 1" not in feedback


def test_three_failures_on_one_claim_are_all_reported_in_check_order():
    report, reason, feedback = _validate(THREE)
    assert report is None and reason == "REPORT_INVALID"
    _in_order(
        feedback,
        [
            "claim 0",
            "cites_non_ok_view",
            "not_citable_as_fact",
            "time_scope_not_bound_to_view",
        ],
    )
    assert NO_DATA_ID in feedback and UNBOUND_ID in feedback
    assert "claim 1" not in feedback


# Bytes computed on main (067d2c18) for claims with a single failure.
SINGLE_FAILURE = {
    "truncated_id_only": (
        _report(_claim(["metrics.query-t0"])),
        " claim 0: unknown_evidence_id (evidence_id: metrics.query-t0). "
        "Nearest delivered evidence_id: metrics.query-t0 -> metrics.query-t0:d1.",
    ),
    "extra_target_ref_only": (
        _report(_claim([OK_ID], refs=("checkout-prod", "otel-demo/frontend"))),
        " claim 0: missing_target_refs. Failing target_refs: otel-demo/frontend; "
        "allowed target_refs: checkout-prod.",
    ),
    "non_ok_view_only": (
        _report(_claim([NO_DATA_ID])),
        f" claim 0: cites_non_ok_view (evidence_id: {NO_DATA_ID}).",
    ),
    "unknown_time_scope_only": (
        _report(_claim([OK_ID], scope="policy-window-9")),
        " claim 0: missing_time_scope_ref.",
    ),
    "two_claims_one_failure_each": (
        _report(
            _claim(["metrics.query-t0"]),
            _claim([OK_ID], refs=("otel-demo/frontend",)),
        ),
        " claim 0: unknown_evidence_id (evidence_id: metrics.query-t0). "
        "Nearest delivered evidence_id: metrics.query-t0 -> metrics.query-t0:d1."
        " claim 1: missing_target_refs. Failing target_refs: otel-demo/frontend; "
        "allowed target_refs: checkout-prod.",
    ),
}


@pytest.mark.parametrize("case", sorted(SINGLE_FAILURE))
def test_single_failure_claims_keep_mains_exact_bytes(case):
    payload, detail = SINGLE_FAILURE[case]
    report, reason, feedback = _validate(payload)
    assert report is None and reason == "REPORT_INVALID"
    assert feedback == PREFIX + detail + SUFFIX


def test_retry_template_and_reason_vocabulary_are_unchanged():
    assert reports.REPORT_RETRY_TEMPLATE == MAIN_TEMPLATE
    assert {
        reports.UNKNOWN_EVIDENCE_ID,
        reports.MISSING_TARGET_REFS,
        reports.MISSING_TIME_SCOPE_REF,
        reports.CITES_NON_OK_VIEW,
        reports.NOT_CITABLE_AS_FACT,
        reports.TIME_SCOPE_NOT_BOUND_TO_VIEW,
        reports.TARGET_NOT_OBSERVED_BY_VIEW,
    } == {
        "unknown_evidence_id",
        "missing_target_refs",
        "missing_time_scope_ref",
        "cites_non_ok_view",
        "not_citable_as_fact",
        "time_scope_not_bound_to_view",
        "target_not_observed_by_view",
    }


# (payload, accepted). Accepted reports return reason "" and no feedback;
# every rejected one is REPORT_INVALID, as on main.
VERDICTS = {
    "valid": (_report(_claim([OK_ID])), True),
    "valid_hypothesis_unbound_view": (
        _report({**_claim([UNBOUND_ID]), "kind": "hypothesis"}),
        True,
    ),
    **{name: (payload, False) for name, (payload, _) in SINGLE_FAILURE.items()},
    "s701": (S701, False),
    "three": (THREE, False),
    "valid_claim_then_combined_failures": (
        _report(_claim([OK_ID]), _claim(["metrics.query-t0"], refs=("x/y",))),
        False,
    ),
}


@pytest.mark.parametrize("case", sorted(VERDICTS))
def test_accept_reject_verdict_is_unchanged(case):
    payload, accepted = VERDICTS[case]
    report, reason, feedback = _validate(payload)
    if accepted:
        assert report is not None and reason == "" and feedback == ""
    else:
        assert report is None and reason == "REPORT_INVALID"
        assert feedback.startswith(PREFIX) and feedback.endswith(SUFFIX)


def test_feedback_carries_no_content_and_keeps_the_fifty_claim_cap():
    secret = "PRIVATE-701-VIEW-CONTENT"
    context = historical_window_context("run-701")
    context["private_notes"] = [secret]
    claims = [
        _claim(
            ["metrics.query-t0"],
            refs=("checkout-prod", "otel-demo/frontend"),
            text=f"{secret} claim {i}",
        )
        for i in range(60)
    ]
    report, reason, feedback = _validate(_report(*claims), context=context)
    assert report is None and reason == "REPORT_INVALID"
    assert secret not in feedback
    assert "claim 0:" in feedback
    assert "claim 50:" not in feedback and "claim 59:" not in feedback
    assert "Truncated:" in feedback
    assert feedback.startswith(PREFIX) and feedback.endswith(SUFFIX)


_PARITY_SCRIPT = """
import json, sys
sys.path.insert(0, sys.argv[1])
from tests import test_701_retry_feedback_all_failures as t
out = [t._validate(p)[2] for p in (t.S701, t.THREE, t.MULTI)]
print(json.dumps(out))
"""

MULTI = _report(
    _claim(["metrics.query-t0", NO_DATA_ID], refs=("b/z", "a/y", "checkout-prod")),
    _claim([UNBOUND_ID, "nope"], refs=("c/x",), scope="policy-window-9"),
)


def test_feedback_bytes_are_deterministic_across_calls_and_hash_seeds():
    for payload in (S701, THREE, MULTI):
        first = _validate(payload)[2]
        assert first and first == _validate(json.loads(json.dumps(payload)))[2]
    outputs = []
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        done = subprocess.run(
            [sys.executable, "-c", _PARITY_SCRIPT, str(REPO)],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        outputs.append(done.stdout)
    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0]) == [_validate(p)[2] for p in (S701, THREE, MULTI)]
