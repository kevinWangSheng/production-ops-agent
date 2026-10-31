"""Offline P7 contract fixtures. No driver implementation or real credentials.

Authority: 2026-10-19-eval-p7-driver-interface.md §§0, 2, 9.3, 13.
The CLI constructs Workspace; its constructor signature is absent from the
contract, so library checks retain that opaque public object without supplying
undocumented constructor arguments. Transport injection uses public send().
"""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from eval_b3_support import (
    CANARIES,
    COVERAGE,
    NetworkGuard,
    author_item,
    env_pairs,
    env_text,
    label_reply,
    load,
    user_payload,
    write_env,
)

from opspilot.evaluation import load_scenarios
from opspilot.evaluation.calibration_flow import final_labels, load_label_pack
from opspilot.evaluation.codec import canonical_bytes
from opspilot.evaluation.gate import parsed_report, run_gate
from opspilot.evaluation.judge import HTTPJudgeTransport, safe_input
from scripts import eval_live_lab

REPO = Path(__file__).resolve().parents[2]
CANARY_REPORT = "CANARY-P7-REPORT-PRIVATE"
CANARY_LABEL = "CANARY-P7-LABEL-PRIVATE"
CANARY_DECISION = "CANARY-P7-DECISION-PRIVATE"
CANARY_INTENT = "CANARY-P7-AUTHOR-INTENT"
SECRETS = (*CANARIES, CANARY_REPORT, CANARY_LABEL, CANARY_DECISION, CANARY_INTENT)
ROUND = "eval-p7-canary"
ATTEMPT = "00000000-0000-4000-8000-000000000001"
PROVIDERS = ("zai", "openai", "deepseek")
# Keep a private reader for evidence snapshots; the runtime .env guard stays
# installed while the driver runs and while snapshots are taken.
_SNAPSHOT_OPEN = io.open


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(canonical_bytes(value))
    path.chmod(0o600)


def snapshot(root):
    """Include empty directories, symlinks, bytes and modes; ignore Git internals."""
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        mode = path.lstat().st_mode
        if path.is_symlink():
            value = ("link", path.readlink().as_posix())
        elif path.is_file():
            with _SNAPSHOT_OPEN(path, "rb") as handle:
                value = ("file", handle.read())
        else:
            value = ("dir",)
        result[str(relative)] = (mode, value)
    return result


class DriverHarness:
    def __init__(self, root, monkeypatch, capsys, driver):
        self.root, self.mp, self.capsys, self.driver = (
            root.resolve(),
            monkeypatch,
            capsys,
            driver,
        )
        root = self.root
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / ".gitignore").write_text("tmp/\nprivate-source/\n")
        monkeypatch.chdir(root)
        guard = NetworkGuard()
        guard.install(monkeypatch)
        self.network = guard
        # A driver must not discover or read a general .env.
        (root / ".env").write_text("CANARY-P7-DOTENV-MUST-NOT-BE-READ=invalid\n")

        def guard_open(original):
            def checked(file, mode="r", *args, **kwargs):
                if (
                    isinstance(file, (str, Path))
                    and Path(file).name == ".env"
                    and "r" in mode
                ):
                    raise AssertionError("P7 must not read a general .env")
                return original(file, mode, *args, **kwargs)

            return checked

        monkeypatch.setattr(builtins, "open", guard_open(builtins.open))
        monkeypatch.setattr(io, "open", guard_open(io.open))
        for name, _ in env_pairs():
            monkeypatch.delenv(name, raising=False)
        env = write_env(root, env_text(), name="judge-config")
        self.env = env
        self.configs = {p: load(env, p) for p in PROVIDERS}
        self.calls = []
        self.labels_by_raw = {}
        # P7 r3: optional fn(payload, provider, label) -> response | None that
        # replaces the label reply for one call (None keeps the label reply).
        self.responder = None
        self.observer = None
        self.balance_observer = None
        self.balance_calls = []

        def send(_transport, **kwargs):
            payload = user_payload(kwargs["body"])
            if self.observer:
                self.observer(payload)
            self.calls.append(payload)
            provider = next(
                p
                for p, config in self.configs.items()
                if config.api_key in kwargs["headers"].values()
                or any(config.api_key in str(v) for v in kwargs["headers"].values())
            )
            label = self.labels_by_raw[payload["report_raw"]]
            if self.responder:
                custom = self.responder(payload, provider, label)
                if custom is not None:
                    return custom
            return label_reply(label, provider)

        def balance(_reader):
            if self.balance_observer:
                self.balance_observer()
            self.balance_calls.append("deepseek")
            return Decimal("100.00")

        monkeypatch.setattr(HTTPJudgeTransport, "send", send)
        monkeypatch.setattr(eval_live_lab.DeepSeekBalance, "balance", balance)
        self.workspace = "canary-workspace"
        self.private = root / "tmp/eval-p7"
        self.items = ()
        self.labels = ()
        self.pack_hash = None
        self.library_workspace = None
        original_init = driver.Workspace.__init__

        def retain_workspace(workspace, *args, **kwargs):
            original_init(workspace, *args, **kwargs)
            self.library_workspace = workspace

        monkeypatch.setattr(driver.Workspace, "__init__", retain_workspace)

    @property
    def ws(self):
        return self.private / self.workspace

    def cli(self, command, *args, code=0, reason=None, workspace=None):
        self.capsys.readouterr()
        argv = [command, "--workspace", workspace or self.workspace, *map(str, args)]
        try:
            actual = self.driver.main(argv)
        except SystemExit as exc:
            actual = exc.code
        captured = self.capsys.readouterr()
        # json.loads rejects an extra JSON object/log line; one newline is fine.
        result = json.loads(captured.out)
        assert isinstance(result, dict)
        assert list(result) == sorted(result)
        assert {"command", "status", "exit_code", "reason"} <= result.keys()
        assert result["command"] == command
        assert actual == result["exit_code"] == code, result
        expected_status = {
            0: {"ok", "selected"},
            1: {"failed", "no_baseline"},
            2: {"usage"},
            3: {"refused"},
            5: {"stopped"},
            6: {"pending_user_decision"},
            130: {"interrupted"},
        }
        assert result["status"] in expected_status[code]
        if reason is not None:
            assert result["reason"] == reason
        for secret in SECRETS:
            assert secret not in captured.out
        assert not self.network.attempts
        return result

    def init(self):
        self.cli("init")
        return self

    def seed(
        self, *, balanced=True, debug_positive_counts=None, swap_source_splits=False
    ):
        """Write only contract DTOs (§9.3 explicitly allows direct fixture records).

        Every scenario/split uses one original and report-only rewrites. Labels
        are authored independently using the existing B3 public test builders.
        """
        records, items, labels, families = [], [], [], {}
        for n, (spec, answer) in enumerate(load_scenarios(REPO / "evals/scenarios")):
            for split, count in (("debug", 2), ("validation", 4)):
                # Logical split can differ across workspaces. Keep the same
                # original report, IDs and provenance for each source family;
                # only the number of its report-only rewrites changes.
                source_split = (
                    ("validation" if split == "debug" else "debug")
                    if swap_source_splits
                    else split
                )
                attempt = f"00000000-0000-4000-8000-{n * 2 + (source_split == 'validation') + 1:012d}"
                family = f"live/{ROUND}/{attempt}"
                families[family] = split
                negative = (
                    "hidden_normal_cause"
                    if spec.kind == "normal"
                    else "extra_wrong_cause"
                )
                variants = ["positive"] + [negative] * (count - 1)
                if split == "debug" and debug_positive_counts is not None:
                    positives = debug_positive_counts.get(spec.scenario_id, 1)
                    variants = ["positive"] * positives + [negative] * (
                        count - positives
                    )
                if not balanced and split == "validation":
                    variants[0] = negative
                source = None
                provenance = None
                for k, variant in enumerate(variants):
                    authored = author_item(
                        spec, answer, split, k + 1, variant, COVERAGE
                    )
                    raw = json.loads(authored.item.request.report.raw)
                    raw["next_steps"] = [f"{CANARY_REPORT}-{n}-{source_split}-{k}"]
                    request = replace(
                        authored.item.request,
                        report=replace(
                            authored.item.request.report, raw=json.dumps(raw)
                        ),
                    )
                    digest = sha(canonical_bytes(safe_input(request)))
                    item_id = "p7-" + digest[:24]
                    item = replace(
                        authored.item,
                        item_id=item_id,
                        source_family=family,
                        input_hash=digest,
                        request=request,
                    )
                    label = replace(
                        authored.label,
                        item_id=item_id,
                        input_hash=digest,
                        diagnoses=tuple(
                            replace(d, reason=CANARY_LABEL)
                            for d in authored.label.diagnoses
                        ),
                    )
                    assert run_gate(
                        request.spec,
                        request.answer,
                        request.outcome,
                        request.report,
                        request.delivered,
                        request.authorized,
                    ).passed
                    rewrite = None
                    if source is None:
                        source = item
                        provenance = dict(
                            round_id=ROUND,
                            attempt_id=attempt,
                            product_run_id=f"CANARY-P7-RUN-{n}-{source_split}",
                            summary_sha256="1" * 64,
                            recording_sha256="2" * 64,
                            manifest_hash="3" * 64,
                            source_report_sha256=sha(request.report.raw.encode()),
                        )
                    else:
                        rewrite = dict(
                            label="controlled-rewrite-of-real-run-evidence",
                            source_item_id=source.item_id,
                            source_report_sha256=provenance["source_report_sha256"],
                            derived_report_sha256=sha(request.report.raw.encode()),
                            change_description=CANARY_INTENT,
                            purpose=CANARY_INTENT,
                            author_identity="CANARY-P7-AUTHOR",
                            author_model_identity="CANARY-P7-AUTHOR-MODEL",
                        )
                    parsed = parsed_report(request.report)
                    assert parsed is not None
                    record = dict(
                        item_id=item_id,
                        kind="original" if rewrite is None else "controlled_rewrite",
                        scenario_id=spec.scenario_id,
                        source_family=family,
                        input_hash=digest,
                        request=request,
                        provenance=provenance,
                        report_semantic_sha256=sha(
                            canonical_bytes(parsed.model_dump(mode="json"))
                        ),
                        coverage=COVERAGE,
                        coverage_note=CANARY_INTENT,
                        rewrite=rewrite,
                    )
                    records.append(record)
                    items.append(item)
                    labels.append(label)
                    self.labels_by_raw[request.report.raw] = label
        self.items = tuple(sorted(items, key=lambda i: i.item_id))
        self.labels = tuple(sorted(labels, key=lambda label: label.item_id))
        for record in records:
            put(self.ws / "items" / f"{record['item_id']}.json", record)
        plan = self.root / "split-plan.json"
        put(
            plan,
            dict(
                schema="opspilot-p7-split-plan-1",
                families=families,
                items=[item.item_id for item in self.items],
            ),
        )
        self.cli("assemble", "--plan", plan)
        return self

    def export(self):
        from opspilot.evaluation import judge

        self.convention = self.root / "convention.txt"
        # No prescribed convention constant exists: any nonempty literal part
        # of PROMPT satisfies §6.1; do not invent a P7 convention API.
        self.convention.write_text(judge.PROMPT.strip())
        self.cli("export-blind", "--convention", self.convention)
        self.packet_hash = sha((self.ws / "blind/packet.json").read_bytes())
        return self

    def response(self, *, packet_hash=None):
        path = self.root / "response.json"
        put(
            path,
            dict(
                schema="opspilot-p7-blind-response-1",
                packet_sha256=packet_hash or self.packet_hash,
                labels=self.labels,
            ),
        )
        return path

    def annotations(self, *, disagree=False, finalize=True):
        response = self.response()
        for slot, model in ((1, "claude-opus-5-5"), (2, "gpt-6-astra")):
            selected_response = response
            if disagree and slot == 2:
                labels = list(self.labels)
                index = next(i for i, label in enumerate(labels) if label.diagnoses)
                labels[index] = replace(
                    labels[index],
                    diagnoses=tuple(
                        replace(d, reason="CANARY-P7-SECOND-ANNOTATION")
                        for d in labels[index].diagnoses
                    ),
                )
                selected_response = self.root / "response-2.json"
                put(
                    selected_response,
                    dict(
                        schema="opspilot-p7-blind-response-1",
                        packet_sha256=self.packet_hash,
                        labels=labels,
                    ),
                )
            self.cli(
                "import-annotation",
                "--slot",
                slot,
                "--response",
                selected_response,
                "--annotator-identity",
                f"CANARY-ANNOTATOR-{slot}",
                "--model-identity",
                model,
                "--evidence-note",
                "CANARY-ISOLATED-SESSION",
            )
        if not finalize:
            return self
        self.cli("brief")
        brief_hash = sha((self.ws / "adjudication/brief.json").read_bytes())
        decisions = self.root / "decisions.json"
        # Agreeing items receive no decision; differences are explicitly
        # adjudicated to A with private decision text.
        brief = json.loads((self.ws / "adjudication/brief.json").read_bytes())
        put(
            decisions,
            dict(
                schema="opspilot-p7-decisions-1",
                brief_sha256=brief_hash,
                decisions=[
                    dict(
                        item_id=entry["item_id"],
                        choice="A",
                        label=None,
                        decision_record=CANARY_DECISION,
                    )
                    for entry in brief["differences"]
                ],
            ),
        )
        self.cli("adjudicate", "--decisions", decisions)
        return self

    def costs(self, session):
        for provider in ("zai", "openai"):
            self.cli(
                "balance",
                "--session",
                session,
                "--provider",
                provider,
                "--phase",
                "before",
                "--unknown",
            )

    def debug(self, session="canary-debug"):
        self.costs(session)
        self.cli("debug", "--session", session, "--judge-env", self.env)
        return self

    def freeze(self, *args):
        self.cli("freeze", *args)
        from opspilot.evaluation.codec import parse

        # Obtain the hash via the public read-only summary, no private helpers.
        summary = self.cli("summary")
        self.pack_hash = summary["pack_hash"]
        self.pack = load_label_pack(
            self.ws / "label-pack", expected_pack_hash=self.pack_hash
        )
        assert set(final_labels(self.pack)) == {i.item_id for i in self.items}
        assert parse((self.ws / "assembled/items.json").read_bytes())
        return self

    def ready(self):
        self.seed().export().annotations().freeze().debug()
        self.costs("canary-formal")
        self.calls.clear()
        self.balance_calls.clear()
        return self

    def formal(self, **kwargs):
        return self.cli(
            "formal",
            "--run",
            "canary-formal",
            "--pack-hash",
            self.pack_hash,
            "--judge-env",
            self.env,
            **kwargs,
        )

    def acceptance(self, *, include_debug_distribution=False):
        distribution = {}
        debug_distribution = {}
        by_id = {label.item_id: label for label in self.labels}
        for item in self.items:
            if item.split not in {"validation", "debug"}:
                continue
            target = debug_distribution if item.split == "debug" else distribution
            counts = target.setdefault(item.scenario_id, {"positive": 0, "negative": 0})
            counts["positive" if by_id[item.item_id].verdict else "negative"] += 1
        brief_hash = sha((self.ws / "adjudication/brief.json").read_bytes())
        value = dict(
            brief_sha256=brief_hash,
            adjudications_sha256=sha(
                (self.ws / "adjudication/adjudications.json").read_bytes()
            ),
            distribution=distribution,
            decision_record=CANARY_DECISION,
        )
        if include_debug_distribution:
            value["debug_distribution"] = debug_distribution
        path = self.ws / "adjudication/distribution-acceptance.json"
        put(path, value)
        return path, value


def live_source(h):
    """Generate authentic live-format artifacts via the existing offline harness."""
    from scripts import eval_live
    from tests.acceptance import eval_c_support as c

    root = h.root / "private-source"
    root.mkdir()
    h.mp.setattr(c, "ROUND", ROUND)
    context = c.recording_context
    h.mp.setattr(
        c,
        "recording_context",
        lambda attempt, manifest: context(attempt, manifest, round_id=ROUND),
    )
    source = c.Harness(root)
    from opspilot.evaluation import ReportCapture

    source.investigator.outcome_transform = lambda outcome: replace(
        outcome,
        final_state="completed",
        decision="report_available",
        report_available=True,
        handoff_reasons=(),
    )
    raw = json.dumps(
        dict(
            schema_version="m0-report-v2",
            assessment_status="completed",
            conclusion="partial",
            summary=CANARY_REPORT,
            claims=[],
            gaps=["CANARY-P7-GAP"],
            next_steps=["CANARY-P7-NEXT"],
        )
    )
    source.investigator.result_transform = lambda result: replace(
        result, report=ReportCapture(raw, "stop")
    )
    code, _ = source.final(h.capsys, argv=source.argv(scenario=c.NORMAL))
    assert code == 0
    summaries = list(source.public().glob("runs/*/summary.json"))
    assert len(summaries) == 1
    path = summaries[0]
    aggregate = source.public() / "aggregate.json"
    # Verify the helper creates the same binding the driver must consume.
    from opspilot.evaluation import load_summary
    from opspilot.evaluation.codec import decode
    from opspilot.evaluation.storage import EvaluationCapture

    capture = decode(EvaluationCapture, json.loads(path.read_bytes())["capture"])
    load_summary(
        path,
        manifest=capture.manifest,
        resolve=eval_live.attempt_resolver(
            ROUND,
            capture.attempt.attempt_id,
            ledger_root=source.deps.ledger_root,
            recording_root=source.deps.recording_root,
        ),
    )
    args = [
        "--round",
        ROUND,
        "--attempt",
        capture.attempt.attempt_id,
        "--evidence-root",
        source.deps.evidence_root,
        "--ledger-root",
        source.deps.ledger_root,
        "--recording-root",
        source.deps.recording_root,
    ]
    return source, path, aggregate, capture, args


def replace_source(path, aggregate, capture):
    """Recompute cached fields with the public storage writer, never forge cache."""
    from opspilot.evaluation.storage import write_summary

    path.unlink()
    write_summary(path, capture)
    value = json.loads(aggregate.read_bytes())
    value["capture"]["manifest"] = json.loads(canonical_bytes(capture.manifest))
    put(aggregate, value)


# ---------------------------------------------------------------------------
# P7 r3 interface assumptions (docs/tasks/2026-10-18-eval-p7-calibration.md
# P7-D17..D21). r3 fixes the behaviour but not these names; each is the most
# natural reading and is listed in EVAL_P7_R3_AMBIGUITIES.md. LEAD: if the
# implementation chose differently, adapt ONLY this block.

# D18: JudgeObservation records the model's self-reported verdict and whether
# it equals the recomputed verdict (None when no valid answer was decoded).
MODEL_VERDICT_FIELD = "model_verdict"
VERDICT_MATCH_FIELD = "verdict_matched"
# D20: JudgeObservation records the transport exception class name (None when
# the transport raised nothing); absent in pre-r3 artifacts.
TRANSPORT_EXCEPTION_FIELD = "transport_error"


def tuning_debug(h, session, *, items=None, code=0, reason=None):
    """D21 CLI surface: one tuning (debug) session over ``items``.

    Assumed mechanism: the existing ``debug`` command, whose ``--item`` now
    also accepts validation items already exposed by a formal run (never
    unexposed validation items), and whose packet binding no longer refuses
    a workspace exported under an older convention. LEAD: if the implementer
    chose a flag or a separate command, change only this function. Callers
    record the session's manual balances first (``h.costs(session)``).
    """
    args = ["--session", session, "--judge-env", h.env, "--tuning"]
    if items:
        args += ["--item", *items]
    return h.cli("debug", *args, code=code, reason=reason)
