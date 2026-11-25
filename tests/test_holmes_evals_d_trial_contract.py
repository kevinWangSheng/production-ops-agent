"""#666 PR-D contract: one C2 trial end to end with replaced Backends.

Independent contract tests written from the task record's "PR-D 接口"
section (pass flow, ``knowledge.py``, the new ``Backends`` fields, new codes,
summary fields) without reading the PR-D implementation. PG, web, worker,
judge are the PR-B harness's recorders
(``tests/test_holmes_evals_trial_contract.py``); the workbench's review
interface is a stand-in ``Workbench`` behind ``import_runbooks``/
``revoke_runbooks``/``active_runbooks`` that reads the question's fixtures
with the real ``import_skills.read_fixtures`` (as the default
``import_question`` does) and keeps one active set per PG instance (every
pass starts a new PG). The upstream is synthetic (answer isolation); every
clock is fixed.

Covered: the order of one pass (release check, ``before_test``, PG, web,
import, worker, delivery, collect, revoke and residue check, web stopped,
``after_test``); environments (knowledge switch on both, the import account
appended to the web's users, no log backend); the import's arguments; the
summary's ``knowledge``, ``data_plane``, disclosures and shift; what the
evidence must not hold (runbook bodies, the 162 extra files, answers,
passwords, keys); every fail-closed point (``RUNBOOK_RESIDUE``,
``RUNBOOK_IMPORT_FAILED``, ``RUNBOOK_FIXTURES_MISSING``,
``RUNBOOK_IMPORT_MISMATCH``, ``RUNBOOK_CATALOG_MISMATCH``, ``CLEANUP_FAILED``
for a refused revoke and for residue) with its retry or abort; the
``finally``-path revoke; a B1 trial untouched by knowledge.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* Interface (implementer addendum ``2f19645b``): the Run's
  ``runbook_catalog`` fact comes from ``Backends.run_catalog(owner_dsn,
  run_id) -> dict | None``; the harness also puts the same fact into the
  collected ledger's ``run.input`` (harmless if unused). ``import_runbooks``
  gets ``Config.import_account`` (a ``UiAccount``, ``.name`` starts with
  ``holmes-eval-import-``), created by ``prepare_round`` only when a C2
  question is selected.
* ``content_bytes`` is the size of the fixture's file or of its content.
* In the ``finally`` path a failed revoke is recorded, not raised: the
  trial's own failure code stays the one in ``history``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opspilot.knowledge.catalog import build_catalog
from tests.holmes_evals_b_support import no_proxy_env  # noqa: F401 - fixture
from tests.holmes_evals_d_support import (
    BODY_SENTINEL,
    C2_NO_K8S,
    GLOBAL_SENTINEL,
    IMPORT_PREFIX,
    ISSUE_SENTINEL,
    NAMESPACE,
    PROMPTS,
    Q89,
    Q90,
    Q94,
    Q162,
    RESOURCE_SENTINEL,
    fixture_names,
    write_c2_question,
)
from tests.holmes_evals_support import FIXTURES, mod, write_question
from tests.test_holmes_evals_trial_contract import (
    EXPECTED_SENTINEL,
    JUDGE,
    MODEL,
    PROMPT_275,
    Q275,
    RATIONALE_SENTINEL,
    Harness,
    _seed_275,
)

pytestmark = pytest.mark.usefixtures("no_proxy_env")

START = datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC)
TARGET = "m0-otel-20260909"
SOURCE_PREFIX = "holmesgpt@a0009195:tests/llm/fixtures/test_ask_holmes/"
SENTINELS = (
    BODY_SENTINEL, GLOBAL_SENTINEL, ISSUE_SENTINEL, RESOURCE_SENTINEL,
    EXPECTED_SENTINEL, RATIONALE_SENTINEL,
)  # fmt: skip


class Workbench:
    """The workbench review interface and the PG runbook view, per PG."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.by_pg: dict[int, set[str]] = {}
        self.pg = 0
        self.preseed: set[str] = set()  # leftovers in the next PG
        self.carry_over = False  # environment reused: the set survives
        self.imports: list[dict[str, Any]] = []
        self.revokes: list[dict[str, Any]] = []
        self.active_calls: list[tuple[str, str]] = []
        self.refuse_import = 0  # passes whose last fixture is refused
        self.mismatch = 0  # passes whose post-import view misses one
        self.refuse_revoke = 0
        self.stuck_after_revoke = 0  # passes whose revoke leaves one active
        self._stuck: set[str] = set()
        self._just_imported = False
        # #780: 162's global instructions as the target's standing
        # instructions, one active row per PG (kept apart from the runbook
        # names, as the real ``active_runbooks`` reads ``kind='runbook'``).
        self.standing_by_pg: dict[int, dict[str, int]] = {}
        self.standing_imports: list[dict[str, Any]] = []

    @property
    def active(self) -> set[str]:
        return self.by_pg.setdefault(self.pg, set())

    def new_pg(self) -> None:
        before = set(self.active) if self.carry_over else set()
        self.pg += 1
        self.by_pg[self.pg] = before | set(self.preseed)
        self.preseed = set()
        self._stuck = set()

    def import_runbooks(self, web_url, account, question_dir, target):
        self.events.append("import")
        fixtures = mod("import_skills").read_fixtures(Path(question_dir))
        self.imports.append(
            {"web_url": web_url, "account": account, "question_dir": Path(question_dir),
             "target": target, "names": [f.name for f in fixtures]}
        )  # fmt: skip
        rows, refusals = [], []
        for i, fixture in enumerate(fixtures):
            if self.refuse_import and i == len(fixtures) - 1:
                refusals.append(f"approve {fixture.name}: RUNBOOK_STATE_CONFLICT")
                continue
            rows.append(
                {"entry_id": str(uuid.UUID(int=len(self.active) + 1 + 1000 * self.pg)),
                 "revision": 1, "entry_generation": 2, "name": fixture.name,
                 "source_ref": fixture.source_ref}
            )  # fmt: skip
            self.active.add(fixture.name)
        if self.refuse_import:
            self.refuse_import -= 1
        self._just_imported = True
        return rows, refusals

    @property
    def standing(self) -> dict[str, int]:
        return self.standing_by_pg.setdefault(self.pg, {})

    def import_standing_instructions(self, web_url, account, question_dir, target):
        self.events.append("import_standing")
        fixture = mod("import_skills").read_global_instructions(Path(question_dir))
        self.standing_imports.append(
            {"web_url": web_url, "account": account, "question_dir": Path(question_dir),
             "target": target, "content": None if fixture is None else fixture.content}
        )  # fmt: skip
        if fixture is None:
            return [], []
        entry_id = str(uuid.UUID(int=900 + 1000 * self.pg))
        self.standing[entry_id] = 1
        return [
            {"entry_id": entry_id, "revision": 1, "entry_generation": 2,
             "name": fixture.name, "source_ref": fixture.source_ref}
        ], []  # fmt: skip

    def active_standing_instructions(self, owner_dsn, target):
        self.events.append("active_standing")
        return sorted(self.standing)

    def run_standing_instructions(self, owner_dsn, run_id):
        return next(
            ({"entry_id": e, "revision": r} for e, r in sorted(self.standing.items())),
            None,
        )

    def revoke_runbooks(self, web_url, account, rows):
        self.events.append("revoke")
        rows = [dict(r) for r in rows]
        self.revokes.append({"web_url": web_url, "account": account, "rows": rows})
        if self.refuse_revoke:
            self.refuse_revoke -= 1
            return [f"revoke {r['name']}: RUNBOOK_GENERATION_CONFLICT" for r in rows]
        for row in rows:
            self.active.discard(row["name"])
            self.standing.pop(str(row["entry_id"]), None)
        if self.stuck_after_revoke:
            self.stuck_after_revoke -= 1
            self._stuck = {"left-behind-runbook"}
        return []

    def active_runbooks(self, owner_dsn, target):
        self.events.append("active")
        self.active_calls.append((owner_dsn, target))
        names = set(self.active) | self._stuck
        if self.mismatch and names and self._just_imported:
            self.mismatch -= 1
            names = set(sorted(names)[1:])
        self._just_imported = False
        return sorted(names)


class C2Harness(Harness):
    """The PR-B fake host plus namespace checks and the workbench."""

    def __init__(self, tmp_path: Path, start: datetime) -> None:
        super().__init__(tmp_path, start)
        self.wb = Workbench(self.events)
        self.ns_present: dict[str, bool] = {}
        self.ns_checks: list[str] = []
        self.catalog_mode = "ok"  # unavailable / omitted / wrong
        self.catalog_bad_passes = 0
        self.at_submit: list[str] = []
        self.submit_accounts: list[Any] = []
        self.catalog_reads = 0

    # -- PG: every pass a new database
    def start_pg(self, directory: Path, port: int) -> Any:
        pg = super().start_pg(directory, port)
        self.wb.new_pg()
        return pg

    def run_script(self, script, *, operation, cwd, env, timeout_s, log_path):
        qid = Path(cwd).name
        if qid == Q275:
            return super().run_script(
                script, operation=operation, cwd=cwd, env=env,
                timeout_s=timeout_s, log_path=log_path,
            )  # fmt: skip
        self.scripts.append(
            {"operation": operation, "script": script, "cwd": Path(cwd),
             "env": dict(env), "timeout_s": timeout_s}
        )  # fmt: skip
        self.events.append(f"script:{operation}")
        started = self.now()
        ns = NAMESPACE.get(qid)
        if ns:
            self.ns_present[ns] = operation == "before_test"
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(f"{operation} ok\n")
        ended = self.now()
        return mod("setup").ScriptResult(
            operation=operation, exit_code=0, timed_out=False, started_at=started,
            ended_at=ended, seconds=(ended - started).total_seconds(),
            log_sha256="0" * 64, skipped=not script,
        )  # fmt: skip

    def namespace_gone(self, namespace, env):
        self.events.append(f"ns_check:{namespace}")
        self.ns_checks.append(namespace)
        return not self.ns_present.get(namespace, False)

    def submit(self, web_url, account, *, question, key, target_id, **extra):
        self.at_submit = sorted(self.wb.active)
        self._fact = self.catalog()  # one fact per Run, fixed at creation
        self.submit_accounts.append(account)
        return super().submit(
            web_url,
            account,
            question=question,
            key=key,
            target_id=target_id,
            **extra,
        )

    def catalog(self) -> dict[str, Any]:
        rows = [
            {"name": n, "entry_id": str(uuid.UUID(int=i + 1)), "revision": 1,
             "description": f"How to look at {n}", "approved_at": START,
             "age_seconds": 5}
            for i, n in enumerate(self.at_submit)
        ]  # fmt: skip
        fact = build_catalog(rows, retrieved_at=START, max_age_seconds=86400 * 90)
        if self.catalog_bad_passes:
            self.catalog_bad_passes -= 1
            if self.catalog_mode == "unavailable":
                fact = dict(fact, status="unavailable", entries=[])
            elif self.catalog_mode == "omitted":
                fact = dict(fact, omitted_for_size=1)  # ids still equal
            elif self.catalog_mode == "wrong":
                fact = dict(
                    fact,
                    entries=[
                        dict(e, skill_id="other-runbook") for e in fact["entries"]
                    ],
                )
        return fact

    def collect(self, dsn: str, run_id: str) -> tuple[dict, dict]:
        block, ledger = super().collect(dsn, run_id)
        ledger = dict(ledger)
        ledger["run"] = dict(
            ledger.get("run") or {},
            input={
                "question": "q",
                "bound_target_id": TARGET,
                "scope_facts": {"runbook_catalog": self._fact},
            },
        )
        return block, ledger

    def run_catalog(self, owner_dsn: str, run_id: str) -> dict[str, Any] | None:
        self.catalog_reads += 1
        return getattr(self, "_fact", None)

    # -- the C5 data plane must stay untouched
    def _never(self, name: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            self.events.append(f"never:{name}")
            raise AssertionError(f"{name} called for a kubernetes question")

        return call

    def backends(self) -> Any:
        driver = mod("driver")
        names = {f.name for f in dataclasses.fields(driver.Backends)}
        over: dict[str, Any] = {
            "import_runbooks": self.wb.import_runbooks,
            "revoke_runbooks": self.wb.revoke_runbooks,
            "active_runbooks": self.wb.active_runbooks,
            "import_standing_instructions": self.wb.import_standing_instructions,
            "active_standing_instructions": self.wb.active_standing_instructions,
            "run_standing_instructions": self.wb.run_standing_instructions,
            "namespace_gone": self.namespace_gone,
            "start_gateway": self._never("start_gateway"),
            "switch_gateway": self._never("switch_gateway"),
            "start_victorialogs": self._never("start_victorialogs"),
            "start_fake_log_backend": self._never("start_fake_log_backend"),
            "start_port_forward": self._never("start_port_forward"),
            "loki_admin": self._never("loki_admin"),
        }
        assert "run_catalog" in names
        over["run_catalog"] = self.run_catalog
        return dataclasses.replace(super().backends(), **over)

    def secrets(self) -> list[str]:
        out = list(super().secrets())
        for imp in self.wb.imports:
            out.append(imp["account"].password)
        return out


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    questions, driver = mod("questions"), mod("driver")
    upstream = tmp_path / "upstream"
    for qid in (Q89, Q90, Q94, Q162):
        write_c2_question(upstream, qid, expected=EXPECTED_SENTINEL)
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
    env_file = tmp_path / "secrets.env"
    env_file.write_text(f"DEEPSEEK_API_KEY={MODEL}\n")
    env_file.chmod(0o600)
    kube = tmp_path / "engineer.kubeconfig"
    kube.write_text("apiVersion: v1\nkind: Config\n")
    made: list[Any] = []

    def make(qids: tuple[str, ...], start: datetime = START):
        from tests.holmes_evals_c_support import free_port

        h = C2Harness(tmp_path, start)
        ports = dataclasses.replace(driver.Ports(), es=free_port())
        cfg = driver.Config(
            round="t1", upstream=upstream, questions=qids, ports=ports,
            es_mode="fake", kubeconfig=kube, env_file=env_file, trace="off",
            judge=True, allow_dirty=True, tmp_root=tmp_path / "tmp",
            evidence_root=tmp_path / "evidence", poll_seconds=0.0,
        )  # fmt: skip
        round_ = driver.Round()
        driver.prepare_round(cfg, h.backends(), round_)
        made.append(round_)
        return h, cfg, upstream

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


def _clean(h: C2Harness, text: str) -> None:
    for needle in (*SENTINELS, *h.secrets()):
        assert needle not in text, needle[:12]


def _last(ev: list[str], name: str) -> int:
    return len(ev) - 1 - ev[::-1].index(name)


# -- one full pass -------------------------------------------------------------------------


def test_c2_k8s_trial_end_to_end(world) -> None:
    driver = mod("driver")
    h, cfg, upstream = world((Q94,))
    assert "start_fake_es" not in h.events  # no B1 question: no ES
    record = driver.run_trial(cfg, h.backends(), Q94, _load(cfg, Q94), 1)
    assert record["status"] == "completed", record
    assert not record["history"]
    ev = h.events
    assert not [e for e in ev if e.startswith("never:")]
    first = ev.index

    # Order of the pass.
    ops = [s["operation"] for s in h.scripts]
    assert ops == ["after_test", "before_test", "after_test"]
    assert first("ns_check:app-94") < first("script:before_test")
    assert first("script:before_test") < first("start:pg") < first("start:web")
    assert first("start:web") < first("import") < first("start:worker")
    assert first("start:worker") < first("submit") < first("collect")
    # Before the import: one residue look; after it: one equality look.
    actives = [i for i, e in enumerate(ev) if e == "active"]
    assert actives[0] < first("import") < actives[1] < first("start:worker")
    assert first("collect") < first("revoke") < actives[-1]
    assert actives[-1] < first("stop:web") < _last(ev, "script:after_test")
    assert _last(ev, "script:after_test") < _last(ev, "ns_check:app-94")
    assert h.started == h.stopped == {"pg": 1, "web": 1, "worker": 1}

    # The import: the workbench of this pass, the question's own directory,
    # the one target, a separate random account.
    (imp,) = h.wb.imports
    assert imp["web_url"].rstrip("/") == f"http://127.0.0.1:{cfg.ports.web}"
    assert imp["question_dir"] == upstream / FIXTURES / Q94
    assert imp["target"] == TARGET
    account = imp["account"]
    assert account is cfg.import_account
    assert account.name.startswith(IMPORT_PREFIX)
    assert re.fullmatch(re.escape(IMPORT_PREFIX) + r"[A-Za-z0-9_-]+", account.name)
    (asker,) = h.submit_accounts
    assert asker.name != account.name and asker.password != account.password
    assert not asker.name.startswith(IMPORT_PREFIX)
    (rev,) = h.wb.revokes
    assert [r["name"] for r in rev["rows"]] == fixture_names(Q94)
    assert rev["account"].name == account.name
    for dsn, target in h.wb.active_calls:
        assert dsn.startswith("postgresql://owner:") and target == TARGET
    assert h.at_submit == fixture_names(Q94)  # catalog visible before the Run
    assert h.catalog_reads >= 1

    # Environments.
    (web,) = h.web_envs
    (worker,) = h.worker_envs
    assert web["OPSPILOT_KNOWLEDGE_TOOL"] == "1"
    assert worker["OPSPILOT_KNOWLEDGE_TOOL"] == "1"
    users = [u.split("=", 1)[0] for u in web["OPSPILOT_UI_USERS"].split(",")]
    assert users == [asker.name, account.name]
    for env in (web, worker):
        assert not [
            k for k in env
            if k.startswith(("OPSPILOT_ES_", "OPSPILOT_LOKI_", "OPSPILOT_VICTORIALOGS_"))
        ]  # fmt: skip
        assert account.password not in json.dumps(env)
    for s in h.scripts:
        assert s["cwd"] == upstream / FIXTURES / Q94
        assert s["env"].get("KUBECONFIG") == str(cfg.kubeconfig)
        assert "VICTORIALOGS_URL" not in s["env"]
        assert not [
            k for k in s["env"] if k.startswith(("OPSPILOT_", "ELASTICSEARCH_"))
        ]

    # Delivered verbatim, no shift.
    (sub,) = h.submits
    assert sub["question"] == PROMPTS[Q94]
    assert sub["target_id"] == TARGET

    summary, raw = _summary(cfg, Q94)
    block = summary["holmes_eval"]
    assert block["shift"]["applied"] is False
    assert block["shift"]["reason"] == "RELATIVE_TIME"
    plane = block["data_plane"]
    assert plane["backend"] == "kubernetes" and plane["mode"] == "none"
    assert plane["product_url"] is None and plane["port_forwards"] == []
    assert plane["victorialogs_image"] is None

    k = block["knowledge"]
    names = fixture_names(Q94)
    qdir = upstream / FIXTURES / Q94
    assert [f["name"] for f in k["fixtures"]] == names
    for f in k["fixtures"]:
        assert set(f) == {"name", "kind", "source_ref", "content_bytes"}
        assert f["kind"] == "skill_md"
        path = qdir / "database-troubleshooting" / "SKILL.md"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert f["source_ref"] == (
            f"{SOURCE_PREFIX}{Q94}/database-troubleshooting/SKILL.md#sha256={digest}"
        )
        fixture = mod("import_skills").read_fixtures(qdir)[0]
        assert f["content_bytes"] in (
            len(fixture.content.encode("utf-8")),
            path.stat().st_size,
        )  # ASSUMPTION: either measure
    assert [i["name"] for i in k["imported"]] == names
    for got, row in zip(k["imported"], h.wb.revokes[0]["rows"], strict=True):
        assert got["entry_id"] == row["entry_id"] and got["revision"] == row["revision"]
    assert k["active_before_import"] == []
    assert k["active_after_import"] == names
    assert k["run_catalog"] == {
        "status": "ok",
        "skill_ids": names,
        "omitted_for_size": 0,
    }
    assert k["revoked"] == len(names)
    assert k["active_after_revoke"] == []
    assert k["import_account_prefix"] == IMPORT_PREFIX
    assert account.name not in raw  # the prefix only

    codes = block["disclosures"]
    assert "RUNBOOKS_IMPORTED_REVIEWED" in codes
    assert "K8S_SCOPE_DIFFERS" in codes  # app-94 is not otel-demo
    assert "GLOBAL_INSTRUCTIONS_IMPORTED_REVIEWED" not in codes
    assert "SKILL_ASSERTION_DROPPED" not in codes
    for c in (
        "DESHIFT_TIME_ONLY_UNCHANGED",
        "DESHIFT_DATE_MIDPOINT",
        "SHIFT_WHOLE_DAYS",
    ):
        assert c not in codes
    for c in (
        "TOOL_FACE_FIXED",
        "NO_INTERMEDIATE_OUTPUT",
        "JUDGE_DIFFERS_FROM_UPSTREAM",
    ):
        assert c in codes
    # Judged as usual.
    assert h.judge_jobs and summary["status"] == "completed"
    _clean(h, _evidence_text(cfg))


def test_162_global_instructions_over_the_limit_are_disclosed_not_imported(
    world,
) -> None:
    """#780: a text over the 4096-byte limit is skipped (never truncated) and
    the disclosure says so; the runbooks are imported as usual."""
    driver = mod("driver")
    h, cfg, upstream = world((Q162,))
    (upstream / FIXTURES / Q162 / "global_instructions.json").write_text(
        json.dumps({"instructions": ["x" * 2100, "y" * 2100]})
    )
    record = driver.run_trial(cfg, h.backends(), Q162, _load(cfg, Q162), 1)
    assert record["status"] == "completed", record
    assert h.wb.standing_imports == []
    block = _summary(cfg, Q162)[0]["holmes_eval"]
    s = block["knowledge"]["standing_instructions"]
    assert s["fixture"]["fits"] is False and s["fixture"]["content_bytes"] > 4096
    codes = block["disclosures"]
    assert "GLOBAL_INSTRUCTIONS_TOO_LARGE_NOT_IMPORTED" in codes
    assert "GLOBAL_INSTRUCTIONS_IMPORTED_REVIEWED" not in codes
    assert [r["name"] for rev in h.wb.revokes for r in rev["rows"]] == fixture_names(
        Q162
    )


def test_c2_without_namespace_or_setup(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world((Q89, Q90))
    for qid in C2_NO_K8S:
        record = driver.run_trial(cfg, h.backends(), qid, _load(cfg, qid), 1)
        assert record["status"] == "completed", (qid, record)
        block = _summary(cfg, qid)[0]["holmes_eval"]
        assert "K8S_SCOPE_DIFFERS" not in block["disclosures"]
        assert "RUNBOOKS_IMPORTED_REVIEWED" in block["disclosures"]
        assert block["knowledge"]["active_after_import"] == fixture_names(qid)
    assert h.ns_checks == []
    assert [s["question"] for s in h.submits] == [PROMPTS[Q89], PROMPTS[Q90]]
    assert not [e for e in h.events if e.startswith("never:")]
    _clean(h, _evidence_text(cfg))


def test_162_dal_runbooks_and_global_instructions(world) -> None:
    driver = mod("driver")
    h, cfg, upstream = world((Q162,))
    record = driver.run_trial(cfg, h.backends(), Q162, _load(cfg, Q162), 1)
    assert record["status"] == "completed", record
    (imp,) = h.wb.imports
    assert imp["names"] == fixture_names(Q162)
    block = _summary(cfg, Q162)[0]["holmes_eval"]
    k = block["knowledge"]
    assert [f["name"] for f in k["fixtures"]] == fixture_names(Q162)
    assert {f["kind"] for f in k["fixtures"]} == {"dal"}
    for f in k["fixtures"]:
        assert re.fullmatch(
            re.escape(f"{SOURCE_PREFIX}{Q162}/runbook_content_")
            + r"[0-9a-f-]+\.json#sha256=[0-9a-f]{64}",
            f["source_ref"],
        ), f["source_ref"]
    assert k["run_catalog"]["skill_ids"] == fixture_names(Q162)
    # #780 (780-10): the global instructions go through the same submit and
    # approve as the target's standing instructions, one ``* item`` line
    # each (780-6), and are revoked after the Run with the runbooks.
    (si,) = h.wb.standing_imports
    assert si["question_dir"] == upstream / FIXTURES / Q162
    assert si["target"] == TARGET and si["account"] is imp["account"]
    assert si["content"] == f"* {GLOBAL_SENTINEL}\n"
    s = k["standing_instructions"]
    assert s["fixture"]["name"] == "standing-instructions"
    assert s["fixture"]["source_ref"].startswith(
        f"{SOURCE_PREFIX}{Q162}/global_instructions.json#sha256="
    )
    assert s["active_before_import"] == []
    (row,) = s["imported"]
    assert s["active_after_import"] == [row["entry_id"]]
    assert s["run_fact"] == {"entry_id": row["entry_id"], "revision": 1}
    assert [r["name"] for rev in h.wb.revokes for r in rev["rows"]] == [
        *fixture_names(Q162),
        "standing-instructions",
    ]
    assert h.wb.standing == {}
    codes = block["disclosures"]
    assert "GLOBAL_INSTRUCTIONS_IMPORTED_REVIEWED" in codes
    assert "GLOBAL_INSTRUCTIONS_NOT_IMPORTED" not in codes
    assert "RUNBOOKS_IMPORTED_REVIEWED" in codes
    assert "K8S_SCOPE_DIFFERS" in codes
    assert "app-162" in h.ns_checks
    _clean(h, _evidence_text(cfg))


def test_b1_trial_untouched_by_knowledge(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world((Q275,))
    _seed_275(h)
    record = driver.run_trial(cfg, h.backends(), Q275, _load(cfg, Q275), 1)
    assert record["status"] == "completed"
    assert h.wb.imports == [] and h.wb.revokes == [] and h.wb.active_calls == []
    assert cfg.import_account is None  # only created when C2 is selected
    assert h.catalog_reads == 0
    (web,) = h.web_envs
    (worker,) = h.worker_envs
    assert "OPSPILOT_KNOWLEDGE_TOOL" not in web
    assert "OPSPILOT_KNOWLEDGE_TOOL" not in worker
    assert "," not in web["OPSPILOT_UI_USERS"]
    block = _summary(cfg, Q275)[0]["holmes_eval"]
    assert "knowledge" not in block
    assert "RUNBOOKS_IMPORTED_REVIEWED" not in block["disclosures"]


# -- fail-closed points --------------------------------------------------------------------


def test_residue_before_import_aborts_round(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world((Q94,))
    h.wb.preseed = {"leftover-runbook"}
    with pytest.raises(driver.RoundAbort) as caught:
        driver.run_trial(cfg, h.backends(), Q94, _load(cfg, Q94), 1)
    assert caught.value.code == "RUNBOOK_RESIDUE"
    assert h.wb.imports == [] and h.submits == []
    assert "start:worker" not in h.events
    assert h.started == h.stopped


def test_refused_import_retries_pass_and_revokes_what_was_imported(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world((Q162,))
    h.wb.refuse_import = 1
    record = driver.run_trial(cfg, h.backends(), Q162, _load(cfg, Q162), 1)
    assert record["status"] == "completed", record
    (failed,) = record["history"]
    assert failed["kind"] == "infra" and failed["code"] == "RUNBOOK_IMPORT_FAILED"
    assert "RUNBOOK_STATE_CONFLICT" in failed["detail"]
    assert len(h.wb.imports) == 2 and len(h.submits) == 1
    # Chance 1: nothing delivered, the two imported runbooks revoked anyway.
    first_revoke = h.events.index("revoke")
    assert first_revoke < _last(h.events, "import")
    assert len(h.wb.revokes[0]["rows"]) == len(fixture_names(Q162)) - 1
    assert h.wb.by_pg[1] == set()


def test_missing_fixtures_is_deterministic(world) -> None:
    driver = mod("driver")
    h, cfg, upstream = world((Q94,))
    bare = upstream / FIXTURES / Q94
    for path in sorted(bare.rglob("SKILL.md")):
        path.unlink()
    out = driver.run_question(cfg, h.backends(), Q94, [1, 2, 3])
    assert h.submits == []
    for t in (1, 2, 3):
        summary = _summary(cfg, Q94, t)[0]
        assert summary["status"] == "missing"
    states = [out["trials"][t] for t in (1, 2, 3)]
    assert all(s["status"] == "missing" for s in states)
    codes = {s.get("code") for s in states}
    assert codes == {"RUNBOOK_FIXTURES_MISSING"}
    raw = _summary(cfg, Q94, 1)[1] + _summary(cfg, Q94, 3)[1]
    assert "RUNBOOK_FIXTURES_MISSING" in raw


def test_import_mismatch_retries(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world((Q162,))
    h.wb.mismatch = 1
    record = driver.run_trial(cfg, h.backends(), Q162, _load(cfg, Q162), 1)
    assert record["status"] == "completed", record
    assert [x["code"] for x in record["history"]] == ["RUNBOOK_IMPORT_MISMATCH"]
    assert len(h.submits) == 1  # chance 1 never delivered
    assert h.events.index("revoke") < _last(h.events, "import")  # finally path
    block = _summary(cfg, Q162)[0]["holmes_eval"]
    assert block["knowledge"]["active_after_import"] == fixture_names(Q162)


@pytest.mark.parametrize("mode", ["unavailable", "omitted", "wrong"])
def test_run_catalog_mismatch_voids_pass(world, mode: str) -> None:
    driver = mod("driver")
    h, cfg, _ = world((Q94,))
    h.catalog_mode, h.catalog_bad_passes = mode, 1
    record = driver.run_trial(cfg, h.backends(), Q94, _load(cfg, Q94), 1)
    assert record["status"] == "completed", record
    assert [x["code"] for x in record["history"]] == ["RUNBOOK_CATALOG_MISMATCH"]
    assert record["history"][0]["kind"] == "infra"
    assert len(h.submits) == 2
    # Chance 1 was revoked (finally path) before chance 2 imported.
    revokes = [i for i, e in enumerate(h.events) if e == "revoke"]
    imports = [i for i, e in enumerate(h.events) if e == "import"]
    assert len(imports) == 2 and revokes[0] < imports[1]
    assert h.wb.by_pg[1] == set()
    # Chance 1 was not judged; the frozen summary is chance 2's.
    assert len(h.judge_jobs) == 1
    summary = _summary(cfg, Q94)[0]["holmes_eval"]
    assert summary["chance"] == 2
    assert summary["knowledge"]["run_catalog"]["status"] == "ok"


def test_run_catalog_mismatch_every_chance_is_missing(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world((Q94,))
    h.catalog_mode, h.catalog_bad_passes = "wrong", 99
    record = driver.run_trial(cfg, h.backends(), Q94, _load(cfg, Q94), 1)
    assert record["status"] == "missing"
    assert [x["code"] for x in record["history"]] == ["RUNBOOK_CATALOG_MISMATCH"] * 3
    assert h.judge_jobs == []
    assert _summary(cfg, Q94)[0]["status"] == "missing"


def test_refused_revoke_aborts_round(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world((Q94,))
    h.wb.refuse_revoke = 1
    with pytest.raises(driver.RoundAbort) as caught:
        driver.run_trial(cfg, h.backends(), Q94, _load(cfg, Q94), 1)
    assert caught.value.code == "CLEANUP_FAILED"
    assert "runbook revoke" in caught.value.detail
    assert h.started["web"] == h.stopped["web"]


def test_residue_after_revoke_aborts_round(world) -> None:
    driver = mod("driver")
    h, cfg, _ = world((Q94,))
    h.wb.stuck_after_revoke = 1
    with pytest.raises(driver.RoundAbort) as caught:
        driver.run_trial(cfg, h.backends(), Q94, _load(cfg, Q94), 1)
    assert caught.value.code == "CLEANUP_FAILED"
    assert "runbook residue" in caught.value.detail


def test_finally_revoke_failure_is_recorded_not_raised(world) -> None:
    """Import mismatch (infra) and then a refused revoke in the finally
    path: the pass is retried, the new PG is clean, the round goes on."""
    driver = mod("driver")
    h, cfg, _ = world((Q162,))
    h.wb.mismatch = 1
    h.wb.refuse_revoke = 1
    record = driver.run_trial(cfg, h.backends(), Q162, _load(cfg, Q162), 1)
    assert record["status"] == "completed", record
    assert [x["code"] for x in record["history"]] == ["RUNBOOK_IMPORT_MISMATCH"]
    assert len(h.wb.revokes) == 2


def test_reused_environment_is_caught_on_next_pass(world) -> None:
    """A pass whose finally-path revoke failed on a reused database: the
    next pass's first look sees the leftovers and stops the round."""
    driver = mod("driver")
    h, cfg, _ = world((Q162,))
    h.wb.carry_over = True
    h.wb.mismatch = 1
    h.wb.refuse_revoke = 1
    with pytest.raises(driver.RoundAbort) as caught:
        driver.run_trial(cfg, h.backends(), Q162, _load(cfg, Q162), 1)
    assert caught.value.code == "RUNBOOK_RESIDUE"
    assert len(h.wb.imports) == 1


def test_c2_k8s_namespace_must_be_released(world) -> None:
    """Same release rule as C5: the namespace left over before the pass is
    an infrastructure failure of that chance."""
    driver = mod("driver")
    h, cfg, _ = world((Q94,))
    h.ns_present["app-94"] = True
    orig = h.run_script
    stuck = {"n": 1}

    def run_script(script, **kw):
        result = orig(script, **kw)
        if kw["operation"] == "after_test" and stuck["n"]:
            stuck["n"] -= 1
            h.ns_present["app-94"] = True  # deletion did not finish
        return result

    h.run_script = run_script  # type: ignore[method-assign]
    record = driver.run_trial(cfg, h.backends(), Q94, _load(cfg, Q94), 1)
    assert record["status"] == "completed", record
    assert record["history"][0]["code"] == "LOG_BACKEND_NOT_RELEASED"
    assert len(h.wb.imports) == 1  # nothing imported on the failed chance
