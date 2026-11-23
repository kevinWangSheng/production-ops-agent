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
        "    redacted = _redact_userinfo(text, _AUTHORITY_USERINFO, _ESCAPED_AUTHORITY_USERINFO)\n",
        "    redacted = text\n",
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
        pg=True,
    ),
    Mutation(
        "M32",
        "credentials: span detail is redacted before it is truncated",
        "opspilot/tools/otel_demo.py",
        '    raw = redact_credentials(text).encode("utf-8")\n',
        '    raw = redact_credentials(\n        text.encode("utf-8")[:MAX_DETAIL_BYTES_PER_FIELD].decode("utf-8", "ignore")\n    ).encode("utf-8")\n',
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
    Mutation(
        "M36",
        "credentials: assignments in escaped (double-encoded) JSON are redacted (#710)",
        "opspilot/redaction.py",
        "    redacted = _redact_escaped_quotes(redacted)\n",
        "    pass\n",
    ),
    Mutation(
        "M37",
        "credentials: runbook catalog descriptions are redacted before the v7 fact (#566 C2b)",
        # ASSUMPTION (test author, implementation unseen): catalog.py calls
        # ``redact_credentials(`` exactly once, on the description; the
        # runner reports ``stale`` otherwise and the implementer pins the
        # exact line.
        "opspilot/knowledge/catalog.py",
        "redact_credentials(",
        "str(",
        pg=True,
    ),
    Mutation(
        "M38",
        "credentials: TodoWrite task content is redacted before it is stored or returned (#565 C1)",
        "opspilot/investigation/todo.py",
        '            "content": redact_credentials(item["content"]),\n',
        '            "content": item["content"],\n',
    ),
    Mutation(
        "M39",
        "scope: with Helm on, only the exact Helm release page of Secrets is sent (#563 B4-5)",
        "opspilot/tools/kubernetes_core.py",
        '    if resource != "secrets":\n        return None\n',
        "    if True:\n        return None\n",
    ),
    Mutation(
        "M40",
        "credentials: Helm values, manifest and notes never leave the Helm tools (#563 R8-18)",
        "opspilot/tools/helm_releases.py",
        '        "rollback_revision": rollback,\n    }\n',
        '        "rollback_revision": rollback,\n        "config": record.get("config"),\n    }\n',
    ),
    Mutation(
        "M41",
        "credentials: inside a Secret reference only its name fields are readable (2026-11-22)",
        "opspilot/redaction.py",
        "                if key in readable and isinstance(item, (str, bool)):\n",
        "                if isinstance(item, (str, bool)):\n",
    ),
    Mutation(
        "M42",
        "credentials: a Secret reference name is still prose-redacted (2026-11-22)",
        "opspilot/redaction.py",
        "                        redact_credentials(item) if isinstance(item, str) else item\n",
        "                        item\n",
    ),
    Mutation(
        "M43",
        "credentials: a readable Secret reference field holds only a string or bool (2026-11-22)",
        "opspilot/redaction.py",
        "                if key in readable and isinstance(item, (str, bool)):\n",
        "                if key in readable:\n",
    ),
    # #773 R18: the three Redis/Valkey layers, killed by
    # tests/safety/test_safety_redis_inspect.py. The test author named the
    # targets; the implementer pinned each ``old`` to the real line in
    # opspilot/tools/redis_inspect.py.
    Mutation(
        "M44",
        "read-only: _send refuses any Redis command outside the frozen allowlist (#773 R3)",
        # Target: the allowlist assertion at the top of ``_send(argv)``, before
        # any byte is written. Mutated: the check never fires.
        "opspilot/tools/redis_inspect.py",
        "        if not _command_allowed(command):\n",
        "        if False:\n",
    ),
    Mutation(
        "M45",
        "read-only: a Redis account beyond the read-only grant is refused (#773 R4)",
        # Target: ``account_problem(getuser_reply)``. Mutated: it always
        # returns None (every account passes).
        "opspilot/tools/redis_inspect.py",
        "def account_problem(getuser_reply: object) -> str | None:\n",
        "def account_problem(getuser_reply: object) -> str | None:\n    return None\n",
    ),
    Mutation(
        "M46",
        "credentials: CONFIG GET always redacts denylisted Redis parameters (#773 R9)",
        # Target: the tool-side denylist test applied to each CONFIG GET
        # parameter name (requirepass, masterauth, masteruser, tls-*-pass,
        # names containing pass/auth/secret). Mutated: no name is denylisted,
        # leaving only the shared redaction (which misses requirepass,
        # masterauth and masteruser).
        "opspilot/tools/redis_inspect.py",
        "            if _config_secret(name):\n",
        "            if False:\n",
    ),
    # #366 (B1-U3 a): killed by
    # tests/safety/test_safety_narrow_credential_names.py.
    Mutation(
        "M47",
        "credentials: a *tokens name keeps its value only when the whole value is a number (#366)",
        # Target: ``_token_count``. Mutated: any value of a count-named field
        # is kept, so ``access_tokens=<credential>`` would reach the model.
        "opspilot/redaction.py",
        "    return _COUNT_NUMBER.fullmatch(value) is not None and _count_name(name)\n",
        "    return _count_name(name)\n",
    ),
    Mutation(
        "M48",
        "credentials: a *tokens name whose prefix is a credential name is not a count (#366)",
        # Target: the prefix check in ``_count_name``. Mutated: the
        # credential-name half is dropped, so ``auth_tokens: 5`` is shown.
        "opspilot/redaction.py",
        "    return not (_credential_name(rest) or words & _CODE_WORDS)\n",
        "    return not (words & _CODE_WORDS)\n",
    ),
    Mutation(
        "M49",
        "credentials: session before a word naming the session (jwt, store, ...) is a credential (#366)",
        # Target: the words after ``session`` that keep a name a credential.
        # Mutated: only the ruling's original id/key/cookie remain.
        "opspilot/redaction.py",
        '        *("id", "key", "cookie", "jwt", "ticket", "sid", "uuid", "nonce"),\n',
        '        *("id", "key", "cookie"),\n',
    ),
    Mutation(
        "M50",
        "credentials: otp/pin/mfa/passcode/refresh *tokens values are not counts (#366)",
        # Target: the code-word guard in ``_count_name``. Mutated: none.
        "opspilot/redaction.py",
        '_CODE_WORDS = frozenset({"otp", "pin", "mfa", "passcode", "refresh"})\n',
        "_CODE_WORDS: frozenset[str] = frozenset()\n",
    ),
    # #359 (B1-U4 a): one per encoded-authority rule, killed by
    # tests/safety/test_safety_encoded_userinfo.py. Mutated: the rule is
    # dropped from the encoded pass.
    Mutation(
        "M51",
        "credentials: userinfo after a percent-encoded // is redacted (#359)",
        "opspilot/redaction.py",
        "    _PERCENT_AUTHORITY_USERINFO,\n",
        "",
    ),
    Mutation(
        "M52",
        "credentials: userinfo after a // escaped two or more levels deep is redacted (#359)",
        "opspilot/redaction.py",
        "    _DEEP_ESCAPED_AUTHORITY_USERINFO,\n",
        "",
    ),
    Mutation(
        "M53",
        "credentials: userinfo after an HTML-entity // is redacted (#359)",
        "opspilot/redaction.py",
        "    _ENTITY_AUTHORITY_USERINFO,\n",
        "",
    ),
    # #574 (B1-U1 a, B1-U2 a): killed by tests/safety/test_safety_env_values.py.
    Mutation(
        "M54",
        "credentials: kubernetes_jq_query runs on redacted objects (#574)",
        # Mutated: the model's jq expression sees the raw objects again.
        "opspilot/tools/kubernetes_core.py",
        '            redact_payload(listed.document()), separators=(",", ":")\n',
        '            listed.document(), separators=(",", ":")\n',
    ),
    Mutation(
        "M55",
        "credentials: kubernetes_tabular_query reads columns from redacted objects (#574)",
        "opspilot/tools/kubernetes_tabular.py",
        "        items = redact_payload(listed.items)\n",
        "        items = listed.items\n",
    ),
    Mutation(
        "M56",
        "credentials: env[].value of a credential-named variable is redacted (#574)",
        "opspilot/redaction.py",
        '                and parent == "env"\n',
        '                and parent == "<never>"\n',
    ),
    Mutation(
        "M57",
        'credentials: a "name":"X","value":... env pair in JSON text is redacted (#574)',
        "opspilot/redaction.py",
        "    paired = _redact_env_pairs(redacted)\n",
        "    paired = redacted\n",
    ),
    Mutation(
        "M58",
        "credentials: a credential-named command-line flag's value is redacted (#802)",
        # Mutated: ``--password v`` and ``"--password","v"`` in text keep the value.
        "opspilot/redaction.py",
        "    flagged = _redact_flag_values(redacted)\n",
        "    flagged = redacted\n",
    ),
    # #807 (B1-U5): killed by tests/safety/test_safety_postmortem_report_input.py.
    Mutation(
        "M59",
        "data flow: postmortem drafting sees only the report's summary, claims and gaps (#807)",
        # Mutated: the model reads the incident's whole terminal step again.
        "opspilot/knowledge/generation.py",
        '        "investigation_report": report,\n',
        '        "investigation_report": incident["conclusion"],\n',
    ),
)
