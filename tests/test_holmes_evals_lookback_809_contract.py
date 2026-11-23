"""#809 contract: the HolmesGPT eval driver uses the longest lookback and
re-derives the time-box disclosure from it (B4-U1/U2, B4-12 as amended by
R2-3; docs/tasks/2026-11-22-next-batches-b4-prereview.md rows U1, U2, B4-12).

Independent contract tests written from the rulings, #779's public interface
(the submit form's ``lookback`` field ``24h``/``7d``/``30d``; the deployment
cap ``OPSPILOT_MAX_LOOKBACK``, read by web and worker alike) and the
upstream setup scripts, without reading an implementation. No network: the
web is a loopback stand-in, every other host action goes through the
recording ``Harness`` of the trial contract.

Rules under test:

* every submit names ``lookback=30d``, the largest value the product offers;
* web and worker both run with ``OPSPILOT_MAX_LOOKBACK=30d``, for every log
  backend;
* ``TIME_BOX_LIMITED`` is derived from a per-question data span and the
  30-day cap, measured from delivery (lead ruling after the PR #824 review):
  a fixed-time item's oldest data lies ``span + LEAD`` before delivery, a
  relative-time item's ``span``; limited when that exceeds the cap, so 184
  (exactly 30 d before its anchor) is limited. Spans checked by
  hand against the setup scripts at the pinned upstream commit:
  184 2024-01-01..31 at 12:00 (30 d; days 32-36 are not dates and stay text),
  185 2024-01-01..10 at 12:00 (9 d), 189 2024-01-14T00..15T23 (47 h),
  235 relative, oldest batch ``days=45`` (45 d), 280 2026-05-14T12:00:01 to
  2026-06-03T08:02:05 (about 19 d 20 h; index C is keyword, not date);
* R2-3: standing instructions are still not imported (162 keeps
  ``GLOBAL_INSTRUCTIONS_NOT_IMPORTED``).

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``shift.MAX_LOOKBACK`` (a ``timedelta``) is the cap and
  ``shift.DATA_SPANS`` maps a question to how far before its anchor (its
  delivery, for relative-time data) its oldest data lies;
  ``TIME_BOX_LIMITED`` is computed from the two, not listed by hand;
* ``intake.submit_question`` takes ``lookback=`` and sends it as a form
  field, and sends no ``lookback`` field when not given (so other channels
  keep the product's 24 h default);
* the driver passes ``lookback="30d"`` to ``Backends.submit`` as a keyword.
"""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest

from opspilot.intake import LOOKBACK_SECONDS
from tests.holmes_evals_b_support import no_proxy_env  # noqa: F401 - fixture
from tests.holmes_evals_support import mod
from tests.test_holmes_evals_driver_contract import _completed, _missing
from tests.test_holmes_evals_intake_contract import QUESTION, _web
from tests.test_holmes_evals_trial_contract import (  # noqa: F401 - fixture
    Q275,
    START,
    _load,
    _seed_275,
    world,
)

Q184 = "184_elasticsearch_index_explosion"
Q185 = "185_elasticsearch_cross_region_search"
Q189 = "189_elasticsearch_timeseries_gap"
Q235 = "235_elasticsearch_cluster_mismatch"
Q280 = "280_es_multi_quirk"
Q162 = "162_get_skills"
CAP = timedelta(days=30)
LOOKBACK_ENV = "OPSPILOT_MAX_LOOKBACK"
#: Hand-checked spans from the setup scripts (module docstring).
SPANS = {
    Q184: timedelta(days=30),
    Q185: timedelta(days=9),
    Q189: timedelta(hours=47),
    Q235: timedelta(days=45),
    Q280: datetime(2026, 6, 3, 8, 2, 5, tzinfo=UTC)
    - datetime(2026, 5, 14, 12, 0, 1, tzinfo=UTC),
}


# -- the cap and the submitted value ---------------------------------------------


def test_cap_is_thirty_days_and_the_largest_product_lookback() -> None:
    # ASSUMPTION: shift.MAX_LOOKBACK (module docstring).
    assert mod("shift").MAX_LOOKBACK == CAP
    assert LOOKBACK_SECONDS["30d"] == CAP.total_seconds()
    assert max(LOOKBACK_SECONDS.values()) == LOOKBACK_SECONDS["30d"]


@pytest.mark.usefixtures("no_proxy_env")
def test_submit_question_sends_the_lookback_field() -> None:
    intake = mod("intake")
    account = intake.new_account()
    with _web() as (url, capture):
        result = intake.submit_question(
            url, account, question=QUESTION, key="k-809", lookback="30d"
        )
    assert result.status == 201
    (req,) = capture.requests
    form = urllib.parse.parse_qs(req["body"].decode("ascii"), keep_blank_values=True)
    assert form["lookback"] == ["30d"]
    assert form["question"] == [QUESTION] and form["idempotency_key"] == ["k-809"]


def test_every_submit_names_30d_and_both_processes_share_the_cap(world) -> None:  # noqa: ANN001, F811
    driver = mod("driver")
    h, cfg, _ = world(START)
    _seed_275(h)
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed"
    (sub,) = h.submits
    assert sub.get("lookback") == "30d"
    (web,) = h.web_envs
    (worker,) = h.worker_envs
    assert web.get(LOOKBACK_ENV) == "30d"
    assert worker.get(LOOKBACK_ENV) == "30d"


@pytest.mark.parametrize(
    "backend", ["elasticsearch", "loki", "victorialogs", "kubernetes", "sql", "mongodb"]
)
def test_tool_env_sets_the_cap_for_every_backend(world, backend: str) -> None:  # noqa: ANN001, F811
    # tool_env is what web and worker share; every item gets the same cap.
    _, cfg, _ = world(START)
    db_env = {
        "OPSPILOT_SQL_URL": "postgresql://ro@127.0.0.1:1/x",
        "OPSPILOT_MONGODB_URL": "mongodb://127.0.0.1:1/x",
    }
    env = mod("driver").tool_env(cfg, backend, db_env=db_env)
    assert env.get(LOOKBACK_ENV) == "30d"


# -- the time-box disclosure, re-derived -----------------------------------------


def test_data_spans_match_the_setup_scripts() -> None:
    # ASSUMPTION: shift.DATA_SPANS (module docstring). A day's slack for how
    # the anchor is read; 184's oldest data lies past the cap from delivery.
    spans = mod("shift").DATA_SPANS
    assert set(SPANS) <= set(spans)
    for qid, want in SPANS.items():
        got = spans[qid]
        assert isinstance(got, timedelta), qid
        assert abs(got - want) <= timedelta(days=1), (qid, got)
    assert spans[Q184] + mod("shift").LEAD > CAP


def test_time_box_limited_is_computed_from_span_and_cap() -> None:
    shift = mod("shift")
    fixed = frozenset(shift.FIXED_TIME)

    def reach(qid: str, span: timedelta) -> timedelta:
        # From delivery: a shifted item's anchor lands LEAD before it.
        return span + shift.LEAD if qid in fixed else span

    derived = frozenset(
        q for q, span in shift.DATA_SPANS.items() if reach(q, span) > CAP
    )
    assert frozenset(shift.TIME_BOX_LIMITED) == derived
    # B4-12 / U2 and the PR #824 ruling: 185, 189 and 280 move out; 184
    # (30 d + LEAD) and 235 (45 days) stay.
    assert frozenset(shift.TIME_BOX_LIMITED) == {Q184, Q235}


@pytest.mark.parametrize(
    ("qid", "limited"),
    [(Q184, True), (Q185, False), (Q189, False), (Q280, False), (Q235, True)],
)
def test_trial_records_carry_the_derived_flag(qid: str, limited: bool) -> None:
    driver = mod("driver")
    assert driver.trial_record(_missing(qid, 1)).time_box_limited is limited
    assert driver.trial_record(_completed(qid, 1, "pass")).time_box_limited is limited


# -- R2-3: standing instructions stay out ----------------------------------------


def test_162_still_discloses_global_instructions_not_imported() -> None:
    driver, questions = mod("driver"), mod("questions")
    q = questions.Question(user_prompt="x")
    shift = driver.ShiftRecord(
        applied=False, reason="RELATIVE_TIME", backend="kubernetes"
    )
    codes = driver.disclosures(Q162, q, shift, k8s_namespace="otel-demo")
    assert "GLOBAL_INSTRUCTIONS_NOT_IMPORTED" in codes
    assert "GLOBAL_INSTRUCTIONS_IMPORTED_REVIEWED" not in codes
