"""pytest plugin loaded into the *upstream* HolmesGPT harness (#668).

Loaded with ``-p opspilot_holmes_compare`` by ``scripts.holmes_compare.runner``;
the upstream checkout is not modified. Standard library and pytest only: it
runs in Holmes' own virtualenv. Active only when ``OHC_QID`` is set.

What it does, per pytest session (one question, one trial):

* keeps exactly the ``test_ask_holmes`` item whose test case id is
  ``OHC_QID`` (so upstream's session setup runs ``before_test`` for that
  question only, like OpsPilot's one-question-per-trial driver);
* R8-13 (``OHC_DROP_SKILL=1``): sets the case's ``memories_generated`` to
  None, so upstream neither appends the ``# Suggested Skills`` block to the
  judge text, nor applies the hard skill-count check, nor runs the
  ``rerun_with_memory`` replay. The SuggestSkills frontend tool itself stays
  injected (official configuration);
* R10-1: after upstream's setup, runs the OpsPilot shift step (same rule as
  OpsPilot's driver) and replaces the case's ``user_prompt`` with the
  rewritten question;
* wraps ``tests.llm.utils.classifiers.evaluate_correctness``: the text
  upstream would send to its judge is de-shifted (same rule as OpsPilot) and
  written to ``judge-input.txt``. ``OHC_JUDGE=opspilot``: upstream's judge is
  not called; a stub score of 1 is returned so no assertion message (which
  prints the expected output) is produced, and the real verdict comes from
  OpsPilot's judge afterwards. ``OHC_JUDGE=upstream``: upstream's judge is
  called on the de-shifted text and only its score is kept;
* writes ``harness.json`` (outcome per phase, exception type names, duration,
  tool and LLM call counts). Never written: expected output, judge rationale,
  credentials, exception messages.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

QID = os.environ.get("OHC_QID", "")
ACTIVE = bool(QID)
STATE: dict[str, Any] = {
    "schema": "holmes-compare-harness-1",
    "qid": QID,
    "selected": None,
    "setup_failed": False,
    "shift": None,
    "captured": False,
    "deshift_count": None,
    "upstream_score": None,
    "phases": {},
    "errors": {},
    "properties": {},
    "exitstatus": None,
}
_PROPERTY_KEYS = ("holmes_duration", "num_llm_calls", "tool_call_count")
STEP_TIMEOUT_S = 1800


def _trial_dir() -> Path:
    return Path(os.environ["OHC_TRIAL_DIR"])


def _write_state() -> None:
    path = _trial_dir() / "harness.json"
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(STATE, sort_keys=True) + "\n", encoding="utf-8")
    partial.replace(path)


def _step(*args: str, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    """One ``scripts.holmes_compare.steps`` call in OpsPilot's interpreter."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TZ")}
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env.update(extra_env or {})
    proc = subprocess.run(
        [os.environ["OHC_STEP_PYTHON"], "-m", "scripts.holmes_compare.steps", *args],
        cwd=os.environ["OHC_REPO"],
        env=env,
        capture_output=True,
        text=True,
        timeout=STEP_TIMEOUT_S,
        check=False,
    )
    lines = proc.stdout.strip().splitlines()
    try:
        result: dict[str, Any] = json.loads(lines[-1]) if lines else {}
    except ValueError:
        result = {}
    result.setdefault("status", "ok" if proc.returncode == 0 else "error")
    result["exit_code"] = proc.returncode
    return result


def _is_target(item: pytest.Item) -> bool:
    callspec = getattr(item, "callspec", None)
    case = callspec.params.get("test_case") if callspec is not None else None
    return (
        getattr(item, "originalname", None) == "test_ask_holmes"
        and case is not None
        and getattr(case, "id", None) == QID
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    if not ACTIVE:
        return
    keep = [item for item in items if _is_target(item)]
    drop = [item for item in items if item not in keep]
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
    STATE["selected"] = len(keep)
    if len(keep) == 1 and os.environ.get("OHC_DROP_SKILL") == "1":
        case = keep[0].callspec.params["test_case"]  # type: ignore[attr-defined]
        case.memories_generated = None
        if hasattr(case, "rerun_with_memory"):
            case.rerun_with_memory = False
    _write_state()
    if len(keep) != 1:
        pytest.exit("HARNESS_SELECT_FAILED", returncode=4)


class _StubScore:
    """Returned instead of upstream's judge in ``opspilot`` mode."""

    score = 1
    metadata: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _opspilot_holmes_compare(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Any:
    if not ACTIVE or getattr(request.node, "originalname", None) != "test_ask_holmes":
        yield
        return
    trial_dir = _trial_dir()
    infra = request.getfixturevalue("shared_test_infrastructure")
    case = request.node.callspec.params["test_case"]
    failures = infra.get("setup_failures", {}) if isinstance(infra, dict) else {}
    if case.id in (failures or {}):
        STATE["setup_failed"] = True
        _write_state()
    else:
        (trial_dir / "question-original.txt").write_text(
            case.user_prompt, encoding="utf-8"
        )
        es = {
            k: os.environ[k]
            for k in ("ELASTICSEARCH_URL", "ELASTICSEARCH_API_KEY")
            if k in os.environ
        }
        STATE["shift"] = _step(
            "shift",
            "--qid",
            QID,
            "--question-file",
            str(trial_dir / "question-original.txt"),
            "--out",
            str(trial_dir / "question-delivered.txt"),
            "--record",
            str(trial_dir / "shift.json"),
            extra_env=es,
        )
        _write_state()
        if STATE["shift"]["status"] != "ok":
            raise RuntimeError("SHIFT_FAILED")
        case.user_prompt = (trial_dir / "question-delivered.txt").read_text(
            encoding="utf-8"
        )

    import tests.llm.utils.classifiers as classifiers  # type: ignore[import-not-found]

    original = classifiers.evaluate_correctness

    def capture(
        expected_elements: Any,
        output: str | None,
        parent_span: Any,
        caplog: Any,
        evaluation_type: str = "strict",
    ) -> Any:
        raw = trial_dir / "judge-input.shifted.txt"
        raw.write_text(output or "", encoding="utf-8")
        out = _step(
            "deshift",
            "--record",
            str(trial_dir / "shift.json"),
            "--in",
            str(raw),
            "--out",
            str(trial_dir / "judge-input.txt"),
        )
        if out["status"] != "ok":
            raise RuntimeError("DESHIFT_FAILED")
        STATE["captured"] = True
        STATE["deshift_count"] = out.get("deshift_count")
        _write_state()
        if os.environ.get("OHC_JUDGE") != "upstream":
            return _StubScore()
        text = (trial_dir / "judge-input.txt").read_text(encoding="utf-8")
        result = original(
            expected_elements=expected_elements,
            output=text,
            parent_span=parent_span,
            caplog=caplog,
            evaluation_type=evaluation_type,
        )
        score = getattr(result, "score", None)
        STATE["upstream_score"] = None if score is None else int(score)
        _write_state()
        return result

    monkeypatch.setattr(classifiers, "evaluate_correctness", capture)
    try:
        yield
    finally:
        props = dict(request.node.user_properties)
        STATE["properties"] = {k: props[k] for k in _PROPERTY_KEYS if k in props}
        _write_state()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    outcome = yield
    if not ACTIVE or getattr(item, "originalname", None) != "test_ask_holmes":
        return
    report = outcome.get_result()
    STATE["phases"][report.when] = report.outcome
    if call.excinfo is not None:
        STATE["errors"][report.when] = call.excinfo.typename
    _write_state()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not ACTIVE:
        return
    STATE["exitstatus"] = int(exitstatus)
    _write_state()
