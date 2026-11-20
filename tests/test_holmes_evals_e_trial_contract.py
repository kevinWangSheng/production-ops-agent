"""#666 PR-E contract: trials end to end with the TodoWrite switch and the
283 prefix release check, with replaced Backends.

Independent contract tests written from the task record's "PR-E 接口"
section without reading the PR-E implementation. The fake host is the PR-D
harness (``tests/test_holmes_evals_d_trial_contract.py``: PG, web, worker,
judge recorders, the in-process fake ES for 275, namespace recorders) plus
stand-ins for the three new seams: ``namespaces_with_prefix`` (a set of
live ``app-283-*`` names that ``before_test``/``after_test`` change),
``run_tool_names`` (offers ``TodoWrite`` exactly when the worker of this
pass was started with ``OPSPILOT_TODO_WRITE=1``, as the product does, unless
a test forces a mismatch) and ``task_list_stats`` (a fixed dict). The
upstream is synthetic (answer isolation: 283's real ``before_test`` is never
read); every clock is fixed.

Covered: ``question``/``on``/``off`` for 283 and a B1 question (envs on both
processes, the summary's ``holmes_eval.todo`` block and its two disclosure
codes, the question delivered without upstream's reminder); the B1 summary
gains only ``todo``; the tool check after delivery and before the wait, and
``TODO_SWITCH_MISMATCH`` both ways (missing, not judged); the prefix release
check before and after the pass, ``namespaces_left``, kubectl failure
counted as not released (``LOG_BACKEND_NOT_RELEASED`` before,
``CLEANUP_FAILED`` after); ``TODO_MODE_CHANGED`` when a round is resumed
under another mode.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* Revision r2 (lead, 2026-11-20): ``run_tool_names`` is read only when the
  pass is enabled or forced, ``task_list_stats`` only when enabled; the
  ``todo`` block always has ``mode``/``enabled``/``source``/
  ``prompt_revision``/``tool_schema_revision`` (from ``collect``'s run
  block ``versions``, None when absent), ``offered`` only when checked and
  ``stats`` only when enabled. Revision r3 (``db616537``): the block is
  written only for ``C1_QUESTIONS + TODO_HARD_QUESTIONS`` and for enabled or
  forced passes; other questions' summaries are unchanged; an earlier
  summary without the block counts as ``question`` for
  ``TODO_MODE_CHANGED``. The harness puts a decoy at ``run.versions`` (the
  DB row's nested dict) so reading that instead fails.
* ``setup.released`` keeps PR-C's ``{"before": {...}, "after": {...}}``
  shape for 283 and each side gains ``namespaces_left``.
* ``TODO_SWITCH_MISMATCH`` is seen through ``run_question`` (deterministic
  errors are recorded there, PR-B); only trial 1 is run, so whether later
  trials of the question are also set missing is not asserted.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.holmes_evals_b_support import no_proxy_env  # noqa: F401 - fixture
from tests.holmes_evals_e_support import (
    FORCED,
    NS_283,
    PREFIX,
    PROMPT_283,
    Q275,
    Q283,
    REMINDER,
    TODO_ENV,
    write_283,
)
from tests.holmes_evals_support import mod, write_question
from tests.test_holmes_evals_d_trial_contract import C2Harness
from tests.test_holmes_evals_trial_contract import (
    DISCLOSURES,
    EXPECTED_SENTINEL,
    JUDGE,
    MODEL,
    PROMPT_275,
    RATIONALE_SENTINEL,
    SUMMARY_KEYS,
    _seed_275,
)

pytestmark = pytest.mark.usefixtures("no_proxy_env")

START = datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC)
VERSIONS = {"prompt_revision": "prompt-rev-e-1", "tool_schema_revision": "tools-e-1"}
BASE_TOOLS = ["kubernetes_pods", "kubernetes_events", "elasticsearch_search"]
STATS = {
    "calls_ok": 2,
    "calls_refused": 1,
    "queries": 7,
    "last": {"total": 4, "completed": 3, "failed": 0, "in_progress": 1, "pending": 0},
}


class TodoHarness(C2Harness):
    def __init__(self, tmp_path: Path, start: datetime) -> None:
        super().__init__(tmp_path, start)
        self.live: set[str] = set()  # app-283-* namespaces that exist
        self.nwp_calls: list[tuple[str, dict[str, str]]] = []
        self.kubectl_down = False
        self.recover_on_after_test: int | None = None  # nth after_test
        self.after_cleans: Any = lambda: True
        self.after_tests = 0
        self.before_tests = 0
        self.offer: bool | None = None  # None: follow the worker env
        self.tool_name_calls: list[tuple[str, str]] = []
        self.stats_calls: list[tuple[str, str]] = []
        self.versions: dict[str, str] | None = dict(VERSIONS)

    def run_script(self, script, *, operation, cwd, env, timeout_s, log_path):
        result = super().run_script(
            script, operation=operation, cwd=cwd, env=env,
            timeout_s=timeout_s, log_path=log_path,
        )  # fmt: skip
        if Path(cwd).name == Q283:
            if operation == "before_test":
                self.before_tests += 1
                self.live |= set(NS_283)
            elif operation == "after_test":
                self.after_tests += 1
                if self.after_cleans():
                    self.live = set()
                if self.recover_on_after_test == self.after_tests:
                    self.kubectl_down = False
        return result

    def namespaces_with_prefix(self, prefix: str, env: Any) -> list[str] | None:
        self.events.append(f"nwp:{prefix}")
        self.nwp_calls.append((prefix, dict(env)))
        if self.kubectl_down:
            return None
        return sorted(n for n in self.live if n.startswith(prefix))

    def run_tool_names(self, owner_dsn: str, run_id: str) -> list[str]:
        self.events.append("run_tool_names")
        self.tool_name_calls.append((owner_dsn, run_id))
        offered = self.offer
        if offered is None:
            offered = self.worker_envs[-1].get(TODO_ENV) == "1"
        return [*BASE_TOOLS, "TodoWrite"] if offered else list(BASE_TOOLS)

    def task_list_stats(self, owner_dsn: str, run_id: str) -> dict[str, Any]:
        self.events.append("task_list_stats")
        self.stats_calls.append((owner_dsn, run_id))
        return json.loads(json.dumps(STATS))

    def collect(self, dsn: str, run_id: str) -> tuple[dict, dict]:
        block, ledger = super().collect(dsn, run_id)
        block = dict(block)
        block.pop("versions", None)
        if self.versions is not None:
            block["versions"] = dict(self.versions)
        block["run"] = dict(
            block.get("run") or {},
            versions={"prompt_revision": "decoy", "tool_schema_revision": "decoy"},
        )
        return block, ledger

    def backends(self) -> Any:
        return dataclasses.replace(
            super().backends(),
            namespaces_with_prefix=self.namespaces_with_prefix,
            run_tool_names=self.run_tool_names,
            task_list_stats=self.task_list_stats,
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    questions, driver = mod("questions"), mod("driver")
    upstream = tmp_path / "upstream"
    write_283(upstream, expected=EXPECTED_SENTINEL)
    write_question(
        upstream,
        Q275,
        {"user_prompt": PROMPT_275, "before_test": "echo seed",
         "after_test": "echo keep", "include_tool_calls": True,
         "expected_output": [EXPECTED_SENTINEL]},
    )  # fmt: skip
    monkeypatch.setattr(questions, "verify_upstream", lambda root: None)
    if hasattr(driver, "verify_upstream"):
        monkeypatch.setattr(driver, "verify_upstream", lambda root: None)
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setenv("ZAI_API_KEY", JUDGE)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv(TODO_ENV, raising=False)
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"DEEPSEEK_API_KEY={MODEL}\n")
    env_file.chmod(0o600)
    kube = tmp_path / "engineer.kubeconfig"
    kube.write_text("apiVersion: v1\nkind: Config\n")
    made: list[Any] = []

    def make(qids: tuple[str, ...], todo_mode: str = "question", start=START):
        from tests.holmes_evals_c_support import free_port

        h = TodoHarness(tmp_path, start)
        ports = dataclasses.replace(driver.Ports(), es=free_port())
        cfg = driver.Config(
            round="t1", upstream=upstream, questions=qids, ports=ports,
            es_mode="fake", kubeconfig=kube, env_file=env_file, trace="off",
            judge=True, allow_dirty=True, tmp_root=tmp_path / "tmp",
            evidence_root=tmp_path / "evidence", poll_seconds=0.0,
            todo_mode=todo_mode,
        )  # fmt: skip
        round_ = driver.Round()
        made.append(round_)
        driver.prepare_round(cfg, h.backends(), round_)
        return h, cfg

    yield make
    for round_ in made:
        round_.close()


def _load(cfg: Any, qid: str) -> Any:
    return mod("questions").load_question(Path(cfg.upstream), qid)


def _summary(cfg: Any, qid: str, n: int = 1) -> tuple[dict, str]:
    path = Path(cfg.evidence_dir) / qid / f"trial-{n}" / "summary.json"
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw), raw


def _evidence_text(cfg: Any) -> str:
    root = Path(cfg.evidence_dir)
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    )


def _clean(h: TodoHarness, text: str) -> None:
    for needle in (EXPECTED_SENTINEL, RATIONALE_SENTINEL, *h.secrets()):
        assert needle not in text, needle[:12]


def _last(ev: list[str], name: str) -> int:
    return len(ev) - 1 - ev[::-1].index(name)


def _todo_envs(h: TodoHarness) -> tuple[str | None, str | None]:
    (web,) = h.web_envs
    (worker,) = h.worker_envs
    return web.get(TODO_ENV), worker.get(TODO_ENV)


# -- 283 under the three modes -------------------------------------------------------------


def test_283_question_mode_end_to_end(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q283,))
    record = driver.run_trial(cfg, h.backends(), Q283, _load(cfg, Q283), 1)
    assert record["status"] == "completed", record
    assert not record["history"]
    ev = h.events
    first = ev.index
    assert not [e for e in ev if e.startswith("never:")]
    assert "start_fake_es" not in ev  # no B1 question: no ES

    # Prefix release check before the setup and after the pass.
    nwp = f"nwp:{PREFIX}"
    assert first(nwp) < first("script:before_test")
    assert _last(ev, "script:after_test") < _last(ev, nwp)
    assert {p for p, _ in h.nwp_calls} == {PREFIX}
    for _, env in h.nwp_calls:
        assert env.get("KUBECONFIG") == str(cfg.kubeconfig)
    assert h.ns_checks == []  # 283 has no single namespace

    # The switch on both processes; the tool check after delivery and
    # before the wait; the stats read after the Run.
    assert _todo_envs(h) == ("1", "1")
    assert first("submit") < first("run_tool_names") < first("run_state")
    assert ev.count("run_tool_names") == 1
    (dsn, run_id), *_ = h.tool_name_calls
    assert dsn.startswith("postgresql://owner:") and run_id
    assert h.stats_calls and h.stats_calls[0][0].startswith("postgresql://owner:")
    assert first("submit") < first("task_list_stats")

    # Delivered verbatim: upstream's reminder is not appended.
    (sub,) = h.submits
    assert sub["question"] == PROMPT_283

    summary, raw = _summary(cfg, Q283)
    he = summary["holmes_eval"]
    todo = he["todo"]
    assert todo == {
        "mode": "question",
        "enabled": True,
        "source": "question",
        "offered": True,
        "prompt_revision": VERSIONS["prompt_revision"],
        "tool_schema_revision": VERSIONS["tool_schema_revision"],
        "stats": STATS,
    }
    assert REMINDER in he["disclosures"]
    assert FORCED not in he["disclosures"]
    released = he["setup"]["released"]
    for phase in ("before", "after"):
        assert released[phase]["namespaces_left"] == [], phase
    plane = he["data_plane"]
    assert plane["backend"] == "kubernetes" and plane["mode"] == "none"
    assert "knowledge" not in he  # 283 imports no runbooks
    assert h.wb.imports == []
    assert h.judge_jobs and summary["status"] == "completed"
    _clean(h, _evidence_text(cfg))


def test_283_forced_off(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q283,), todo_mode="off")
    record = driver.run_trial(cfg, h.backends(), Q283, _load(cfg, Q283), 1)
    assert record["status"] == "completed", record
    assert _todo_envs(h) == (None, None)
    he = _summary(cfg, Q283)[0]["holmes_eval"]
    t = he["todo"]
    assert (t["mode"], t["enabled"], t["source"], t["offered"]) == (
        "off", False, "forced", False,
    )  # fmt: skip
    assert "stats" not in t
    assert "run_tool_names" in h.events  # forced: checked
    assert "task_list_stats" not in h.events  # off: not read
    assert FORCED in he["disclosures"]
    assert REMINDER not in he["disclosures"]


def test_283_forced_on(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q283,), todo_mode="on")
    record = driver.run_trial(cfg, h.backends(), Q283, _load(cfg, Q283), 1)
    assert record["status"] == "completed", record
    assert _todo_envs(h) == ("1", "1")
    he = _summary(cfg, Q283)[0]["holmes_eval"]
    t = he["todo"]
    assert (t["mode"], t["enabled"], t["source"], t["offered"]) == (
        "on", True, "forced", True,
    )  # fmt: skip
    assert t["stats"] == STATS
    assert FORCED in he["disclosures"] and REMINDER in he["disclosures"]


# -- B1 under the switch -------------------------------------------------------------------


def test_b1_default_summary_unchanged(world) -> None:
    """275 is not one of the five compared questions: off by the question,
    no todo block, no new key, no PG read for the switch."""
    driver = mod("driver")
    h, cfg = world((Q275,))
    _seed_275(h)
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed", record
    assert _todo_envs(h) == (None, None)
    assert h.nwp_calls == []  # no prefix for a B1 question
    summary, _ = _summary(cfg, Q275)
    he = summary["holmes_eval"]
    assert set(he) == SUMMARY_KEYS | {"shift_timing", "data_plane"}
    assert set(he["disclosures"]) <= DISCLOSURES
    assert "released" not in he["setup"]
    # Off by the question: no PG read for the switch (existing harnesses
    # need no new seam).
    assert "run_tool_names" not in h.events
    assert "task_list_stats" not in h.events


def test_compared_question_off_by_question_gets_the_short_block(world) -> None:
    """A compared question (283 here, with ``enable_todo`` false) gets the
    five-key block, unchecked and without stats."""
    driver = mod("driver")
    h, cfg = world((Q283,))
    write_283(Path(cfg.upstream), expected=EXPECTED_SENTINEL, enable_todo=False)
    record = driver.run_trial(cfg, h.backends(), Q283, _load(cfg, Q283), 1)
    assert record["status"] == "completed", record
    assert _todo_envs(h) == (None, None)
    he = _summary(cfg, Q283)[0]["holmes_eval"]
    assert he["todo"] == {
        "mode": "question",
        "enabled": False,
        "source": "question",
        "prompt_revision": VERSIONS["prompt_revision"],
        "tool_schema_revision": VERSIONS["tool_schema_revision"],
    }
    assert "run_tool_names" not in h.events
    assert "task_list_stats" not in h.events
    assert REMINDER not in he["disclosures"] and FORCED not in he["disclosures"]


def test_versions_absent_are_none(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q275,), todo_mode="on")
    _seed_275(h)
    h.versions = None
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed", record
    t = _summary(cfg, Q275)[0]["holmes_eval"]["todo"]
    assert t["prompt_revision"] is None and t["tool_schema_revision"] is None


def test_b1_forced_on(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q275,), todo_mode="on")
    _seed_275(h)
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed", record
    assert _todo_envs(h) == ("1", "1")
    he = _summary(cfg, Q275)[0]["holmes_eval"]
    t = he["todo"]
    assert (t["mode"], t["enabled"], t["source"], t["offered"]) == (
        "on", True, "forced", True,
    )  # fmt: skip
    assert t["stats"] == STATS
    assert FORCED in he["disclosures"] and REMINDER in he["disclosures"]
    # Only the two todo codes are new.
    assert set(he["disclosures"]) - DISCLOSURES == {FORCED, REMINDER}


# -- the tool check ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("qid", "mode", "offer"),
    [(Q283, "question", False), (Q275, "on", False), (Q283, "off", True),
     (Q275, "off", True)],
)  # fmt: skip
def test_switch_mismatch_is_deterministic_missing(
    world, qid: str, mode: str, offer: bool
) -> None:
    driver = mod("driver")
    h, cfg = world((qid,), todo_mode=mode)
    if qid == Q275:
        _seed_275(h)
    h.offer = offer
    out = driver.run_question(cfg, h.backends(), qid, [1])
    state = out["trials"][1]
    assert state["status"] == "missing", state
    assert state.get("code") == "TODO_SWITCH_MISMATCH"
    summary, raw = _summary(cfg, qid)
    assert summary["status"] == "missing"
    assert "TODO_SWITCH_MISMATCH" in raw
    assert h.judge_jobs == []
    assert len(h.submits) == 1  # deterministic: no second chance
    # Checked before waiting: the Run's state was never polled.
    assert "run_state" not in h.events
    assert h.started == h.stopped


# -- prefix release check ------------------------------------------------------------------


def test_prefix_leftover_before_pass_is_retried(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q283,))
    h.live = {"app-283-alpha"}  # an earlier trial's leftover
    calls = {"n": 0}

    def cleans() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # the first after_test leaves it behind

    h.after_cleans = cleans
    record = driver.run_trial(cfg, h.backends(), Q283, _load(cfg, Q283), 1)
    assert record["status"] == "completed", record
    first, *_ = record["history"]
    assert first["code"] == "LOG_BACKEND_NOT_RELEASED" and first["kind"] == "infra"
    assert sum(h.sleeps) >= 150  # waited for the namespaces (up to 180 s)
    assert len(h.submits) == 1
    he = _summary(cfg, Q283)[0]["holmes_eval"]
    assert he["chance"] >= 2
    assert he["setup"]["released"]["after"]["namespaces_left"] == []


def test_kubectl_failure_before_pass_counts_as_not_released(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q283,))
    h.kubectl_down = True
    h.recover_on_after_test = 2  # back once the next cleanup runs
    record = driver.run_trial(cfg, h.backends(), Q283, _load(cfg, Q283), 1)
    assert record["status"] == "completed", record
    first, *_ = record["history"]
    assert first["code"] == "LOG_BACKEND_NOT_RELEASED" and first["kind"] == "infra"
    assert sum(h.sleeps) >= 150
    assert len(h.submits) == 1
    # Nothing was set up while kubectl could not answer.
    assert h.before_tests == 1


def test_kubectl_down_every_chance_is_missing(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q283,))
    h.kubectl_down = True
    try:
        record = driver.run_trial(cfg, h.backends(), Q283, _load(cfg, Q283), 1)
    except driver.RoundAbort as caught:  # fail closed either way
        assert caught.code == "CLEANUP_FAILED"
        assert h.submits == []
        return
    assert record["status"] == "missing"
    assert {x["code"] for x in record["history"]} == {"LOG_BACKEND_NOT_RELEASED"}
    assert h.submits == [] and h.before_tests == 0


def test_prefix_left_after_pass_aborts(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q283,))
    h.after_cleans = lambda: h.before_tests == 0  # cleans only before setup
    with pytest.raises(driver.RoundAbort) as caught:
        driver.run_trial(cfg, h.backends(), Q283, _load(cfg, Q283), 1)
    assert caught.value.code == "CLEANUP_FAILED"


def test_kubectl_failure_after_pass_aborts(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q283,))
    orig = h.run_script

    def run_script(script, **kw):
        result = orig(script, **kw)
        if kw["operation"] == "after_test" and h.before_tests >= 1:
            h.kubectl_down = True  # the API goes away after the pass
        return result

    h.run_script = run_script  # type: ignore[method-assign]
    with pytest.raises(driver.RoundAbort) as caught:
        driver.run_trial(cfg, h.backends(), Q283, _load(cfg, Q283), 1)
    assert caught.value.code == "CLEANUP_FAILED"
    assert len(h.submits) == 1


# -- one mode per round --------------------------------------------------------------------


def test_resuming_a_round_under_another_mode_aborts(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q283,), todo_mode="off")
    record = driver.run_trial(cfg, h.backends(), Q283, _load(cfg, Q283), 1)
    assert record["status"] == "completed", record
    assert _summary(cfg, Q283)[0]["holmes_eval"]["todo"]["mode"] == "off"
    for other in ("on", "question"):
        with pytest.raises(driver.RoundAbort) as caught:
            world((Q283,), todo_mode=other)
        assert caught.value.code == "TODO_MODE_CHANGED", other
    # The same mode resumes.
    world((Q283,), todo_mode="off")


def test_summary_without_block_counts_as_question_mode(world) -> None:
    driver = mod("driver")
    h, cfg = world((Q275,))
    _seed_275(h)
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed", record
    assert "todo" not in _summary(cfg, Q275)[0]["holmes_eval"]
    for other in ("on", "off"):
        with pytest.raises(driver.RoundAbort) as caught:
            world((Q275,), todo_mode=other)
        assert caught.value.code == "TODO_MODE_CHANGED", other
    world((Q275,), todo_mode="question")


def test_off_by_question_is_not_checked(world) -> None:
    """Revision r2: an off-by-question pass never reads the Run's tools, so
    an offered TodoWrite there is not seen (and not a mismatch)."""
    driver = mod("driver")
    h, cfg = world((Q275,))
    _seed_275(h)
    h.offer = True
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed", record
    assert "run_tool_names" not in h.events
    assert "todo" not in _summary(cfg, Q275)[0]["holmes_eval"]
