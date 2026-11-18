"""The fixed invariant mutation list (test-suite slim task, 2026-11-18).

Each entry breaks one product invariant the kept suite must guard. The
runner (``tests/mutation/run.py``) applies it to a scratch copy of the
checkout, runs the suite there and expects at least one failure. Every edit's ``old``
must occur exactly once in its file; a refactor that moves the code makes the
runner report ``stale`` so the entry is updated instead of silently passing.
``pg`` entries need the lab PostgreSQL (``M1_DURABLE_POSTGRES=1``) and run on
a freshly migrated database.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mutation:
    """One invariant broken by one or more text edits (all must apply)."""

    id: str
    invariant: str
    path: str
    old: str
    new: str
    pg: bool = False
    #: Further ``(path, old, new)`` edits for an invariant held in layers.
    more: tuple[tuple[str, str, str], ...] = ()

    @property
    def edits(self) -> tuple[tuple[str, str, str], ...]:
        return ((self.path, self.old, self.new), *self.more)


#: publish()'s whole human-control fence: Run state, incident state and the
#: lease/generation check (each alone is backed up by the others).
PUBLISH_FENCE = '                or row["run_state"] != "running"\n                # `paused` 在当前实现下是纵深防御而非承重判定：暂停必然递增\n                # control_generation，且 claim() 已拒绝在暂停期间发放租约，\n                # 因此 _lease_revoked() 的 generation 栅栏已经拦下同一批调用。\n                # 变异测试确认去掉本项不会导致任何用例失败。保留它是为了在\n                # 栅栏被削弱时仍然兜底。\n                or row["incident_state"] in {"completed", "cancelled", "paused"}\n                or self._lease_revoked(row, lease, self._db_now(conn))\n'

MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "M01",
        "read-only: the registry refuses write capability",
        "opspilot/tools/registry.py",
        "if self.read_only is not True or self.verb in FORBIDDEN_VERBS:",
        "if False:",
    ),
    Mutation(
        "M02",
        "scope: a model-named target never selects an unregistered target",
        "opspilot/tools/executor.py",
        "        # display name, endpoint or service label never selects a target.\n"
        "        target = self._targets.resolve(request.target_ref)\n",
        "        # display name, endpoint or service label never selects a target.\n"
        '        target = self._targets.resolve(request.target_ref) or self._targets.resolve("m0-otel-20260909")\n',
    ),
    Mutation(
        "M03",
        "scope: a query window outside the Run's authorization is refused",
        "opspilot/tools/executor.py",
        "if not scope.window.contains(window):",
        "if False:",
    ),
    Mutation(
        "M04",
        "scope: Kubernetes Secrets are never read",
        "opspilot/tools/kubernetes_core.py",
        'if "secret" in kind.lower():',
        "if False:",
    ),
    Mutation(
        "M05",
        "scope: PromQL offset/@ cannot move evaluation outside the window",
        "opspilot/tools/otel_demo.py",
        'if "@" in expr or re.search(r"\\boffset\\b", expr, re.IGNORECASE):',
        "if False:",
    ),
    Mutation(
        "M06",
        "scope: a PromQL lookback longer than the window is refused",
        "opspilot/tools/otel_demo.py",
        "if lookback is None or lookback > window_seconds:",
        "if lookback is None:",
    ),
    Mutation(
        "M07",
        "credentials: URL userinfo is redacted",
        "opspilot/redaction.py",
        "for rule in (_AUTHORITY_USERINFO, _ESCAPED_AUTHORITY_USERINFO):",
        "for rule in ():",
    ),
    Mutation(
        "M08",
        "credentials: name=value assignments are redacted",
        "opspilot/redaction.py",
        "    redacted = _redact_assignments(redacted)\n",
        "    pass\n",
    ),
    Mutation(
        "M09",
        "credentials: quoted credential values are redacted",
        "opspilot/redaction.py",
        "    redacted = _redact_quoted(redacted)\n",
        "    pass\n",
    ),
    Mutation(
        "M10",
        "credentials: Authorization scheme values are redacted",
        "opspilot/redaction.py",
        "    return _CREDENTIAL_SCHEME.sub(\n"
        "        lambda m: f\"{m.group('scheme')} {REDACTED_CREDENTIAL}\", redacted\n"
        "    )\n",
        "    return redacted\n",
    ),
    Mutation(
        "M11",
        "human control: a suspension refuses reads before dispatch",
        "opspilot/tools/executor.py",
        '        if control.suspended:\n            return "SUSPENDED"\n',
        "",
    ),
    Mutation(
        "M12",
        "human control: a decision during a read makes its result history only",
        "opspilot/tools/executor.py",
        "        if self._generations_changed(control):\n"
        '            return "CONTROL_GENERATION_CHANGED"\n',
        "",
    ),
    Mutation(
        "M13",
        "recovery: missing required signals never confirm health",
        "opspilot/domain/observation.py",
        "        and sample.required_signals_present\n",
        "",
    ),
    Mutation(
        "M14",
        "recovery: without a bound HealthProfile recovery is never confirmed",
        "opspilot/domain/observation.py",
        "        session.health_profile_revision is not None\n        and ",
        "        ",
    ),
    Mutation(
        "M15",
        "recovery: a stale reading is never usable",
        "opspilot/observer/health_profile.py",
        "    if age < 0 or age > profile.freshness_seconds:",
        "    if False:",
    ),
    Mutation(
        "M16",
        "human control: the store fences a Run's writes after a human decision",
        "opspilot/persistence/steps.py",
        PUBLISH_FENCE,
        "",
        pg=True,
        more=(
            (
                "opspilot/persistence/base.py",
                '        return (\n            row["owner"] != lease.owner\n',
                '        return False and (\n            row["owner"] != lease.owner\n',
            ),
        ),
    ),
    Mutation(
        "M17",
        "human control: a paused incident is never claimed",
        "opspilot/persistence/runs.py",
        '            if row["incident_state"] in {"completed", "cancelled", "paused"}:\n'
        '                raise PersistenceError("CONTROL_DENIED")\n',
        "",
        pg=True,
    ),
    Mutation(
        "M18",
        "runtime roles: migrations grant only the declared privileges",
        "opspilot/migrations/versions/0011_runtime_roles.py",
        'return f"GRANT {kind} {on}ON {relation} TO {role};"',
        'return f"GRANT {kind} {on}ON {relation} TO {role}; GRANT DELETE ON {relation} TO {role};"',
        pg=True,
    ),
    Mutation(
        "M19",
        "credentials: the gateway's configured token never reaches the model",
        "opspilot/tools/otel_demo.py",
        "                        body = error.read(ERROR_BODY_BYTES)\n",
        "                        body = error.read(ERROR_BODY_BYTES).replace(\n"
        '                            b"after 401", (token or "").encode()\n'
        "                        )\n",
    ),
    Mutation(
        "M20",
        "report validation: a claim citing an undelivered evidence id is refused",
        "opspilot/investigation/reports.py",
        "        if unknown_ids:\n"
        "            failures.append(\n"
        "                ClaimCitationFailure(index, UNKNOWN_EVIDENCE_ID, unknown_ids)\n"
        "            )\n"
        "            continue\n"
        "        if target_catalog is not None:\n",
        "        if target_catalog is not None:\n",
    ),
    Mutation(
        "M21",
        "report validation: a fact may not cite a failed or no-data view",
        "opspilot/investigation/reports.py",
        "        if non_ok:\n"
        "            failures.append(ClaimCitationFailure(index, CITES_NON_OK_VIEW, non_ok))\n"
        "            continue\n",
        "",
    ),
    Mutation(
        "M22",
        "report validation: a claim's target must be one its views observed",
        "opspilot/investigation/reports.py",
        "        if any(ref not in observed for ref in claim.target_refs):",
        "        if False:",
    ),
    Mutation(
        "M23",
        "report validation: an incomplete investigation is never a finding",
        "opspilot/investigation/reports.py",
        '        if self.assessment_status == "incomplete" and (',
        "        if False and (",
    ),
    Mutation(
        "M24",
        "report validation: a supported conclusion needs a cited fact",
        "opspilot/investigation/reports.py",
        '        if self.conclusion == "supported" and not any(',
        "        if False and not any(",
    ),
    Mutation(
        "M25",
        "recovery: the Observer imports nothing from the investigation side",
        "opspilot/observer/loop.py",
        "from __future__ import annotations\n",
        "from __future__ import annotations\n\nimport opspilot.tools.executor  # noqa: F401\n",
    ),
    Mutation(
        "M26",
        "data egress: traces go only to the allowed LangSmith endpoint",
        "opspilot/tracing.py",
        "def _is_langsmith_endpoint(url: str) -> bool:\n",
        "def _is_langsmith_endpoint(url: str) -> bool:\n    return True\n",
    ),
    Mutation(
        "M27",
        "credentials: the intake question is redacted before the model sees it",
        "opspilot/investigation/context.py",
        '        {"role": "user", "content": redact_credentials(input.question)},\n',
        '        {"role": "user", "content": input.question},\n',
        pg=True,
    ),
    Mutation(
        "M28",
        "credentials: Alertmanager labels and annotations are redacted",
        "opspilot/alertmanager.py",
        "def _redacted(value: Any, key: str | None = None) -> Any:\n",
        "def _redacted(value: Any, key: str | None = None) -> Any:\n    return value\n",
        pg=True,
    ),
    Mutation(
        "M29",
        "credentials: workbench messages are redacted before the model sees them",
        "opspilot/investigation/context.py",
        "            value = redact_credentials(value)\n",
        "            pass\n",
        pg=True,
    ),
    Mutation(
        "M30",
        "credentials: Observer Prometheus bodies are redacted before storage",
        "opspilot/observer/prometheus.py",
        "    return redact_json_bytes(body)\n",
        "    return body\n",
    ),
    Mutation(
        "M31",
        "credentials: the evidence page shows redacted raw bytes",
        "opspilot/web/app.py",
        "    shown = redact_json_bytes(raw)\n",
        "    shown = raw\n",
    ),
    Mutation(
        "M32",
        "credentials: span detail is redacted before it is truncated",
        "opspilot/tools/otel_demo.py",
        '    raw = redact_credentials(text).encode("utf-8")\n',
        '    raw = redact_credentials(\n        text.encode("utf-8")[:MAX_DETAIL_BYTES_PER_FIELD].decode("utf-8", "ignore")\n    ).encode("utf-8")\n',
        pg=True,
    ),
    Mutation(
        "M33",
        "human control: a global suspension revokes a held lease (parking and generation)",
        "opspilot/persistence/base.py",
        '            or int(row.get("global_generation", 0))\n            != lease.global_suspension_generation\n            or int(row.get("target_generation", 0))\n            != lease.target_suspension_generation\n',
        "",
        pg=True,
        more=(
            (
                "opspilot/persistence/controls.py",
                "\"UPDATE opspilot_runs SET owner=NULL,lease_until=NULL,state='paused' WHERE state IN ('queued','running','waiting_human')\"\n",
                '"UPDATE opspilot_runs SET state=state WHERE false"\n',
            ),
        ),
    ),
    Mutation(
        "M34",
        "evidence: the postmortem projection calls no model, telemetry or SQL (F13 R12)",
        "opspilot/acceptance_postmortem.py",
        "from __future__ import annotations\n",
        "from __future__ import annotations\n\nimport opspilot.tracing  # noqa: F401\n",
    ),
    Mutation(
        "M35",
        "credentials: the Chat panel's model input is redacted (workbench P7)",
        "opspilot/chat/context.py",
        "    text = redact_credentials(str(value))\n",
        "    text = str(value)\n",
        pg=True,
    ),
)
