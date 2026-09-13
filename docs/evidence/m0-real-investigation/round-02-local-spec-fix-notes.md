# Local Spec P2 runtime status/protocol failure fix

## Scope and isolation

Base: `af24646eb02da3de2839005a7a3ea361fd7ac914`.
Read-only source: `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`.
Isolated tracked-code archive: `/tmp/m0-local-spec-fix-code` (created with git archive; no ignored .env, database, private response or real run files copied).
No original file/index modification, commit, package installation, service lifecycle, PG or real model/network call. Existing Python interpreters used read-only. Root owns application approval and original worktree changes.

Current patch: `/tmp/m0-local-spec-fix.patch`
SHA256: `1b0553501438c50722c6c8d88daaeaa08414d19428f63660ebd9067fe58aa319`.
Prior patch retained at `/tmp/m0-local-spec-fix-v1.patch`, exact SHA256 `ca6b4c99f43a3f2f57985cfc6bfd405b4879c3933e3d9671022b7cef3972157c`.
Only three tracked files change; no schema/platform expansion:

- scripts/m0/holmes_bridge.py — `5d48f6634d191d0e08eb8bf5ae8ad3a3e79a47144fd49b65aac68b6939a2b220`
- tests/test_m0_holmes_bridge_v4.py — `7e0efff250da72d2f2c38b73188a7800f83f5d8065d7ef80a8743f8c973faef9`
- tests/fixtures/m0_environment/initial_report_probe.py — `b40e51ca8d7ec42f003815e0a028436034068d05f1ef100a85e1889db5fe521d`

## Mechanism

Strict bridge classification now uses actual runner result status. `failed` remains failed; `incomplete` and absent/unrecognized status remain blocked with explicit handoff reasons. A parsed report cannot turn these into completed. Only `investigation_returned` with a parsed candidate and no recorded validation/exception/boundary rejection can remain completed. Contradictory success plus failure markers is blocked. Full parsed report, raw content/hash, input, artifacts, actions, delivery and capture are preserved. Fixed failure codes are exported, not arbitrary exception body text. Explicit legacy replay behavior is unchanged.

The actual fakepipe mixed JSON+tool_calls experiment exposed an additional same-family path: Holmes throws before ANSWER_END, so runtime result retains final_business_content=null, whereas the complete identity-accepted safe response-business file contains the JSON candidate. For exactly failed/incomplete runner status, and only the same run/ordinal complete identity-accepted response, the bridge now preserves that safe response content as an **unaccepted candidate**. It does not read private protocol, modify original result, or synthesize ReportCapture. Existing handoff codes `UNACCEPTED_CANDIDATE_FROM_RESPONSE` and `RUNNER_FINAL_CONTENT_NULL`/`MISSING` distinguish source and original runner absence. With missing capture, the strict checker still rejects the candidate. Success/unknown status with absent result content, cross-run/attempt responses and nonempty result/response differences remain rejected.

All CLI handoffs now retain full audit in the private output, even when the packet accurately reports incomplete investigation and is contract-consistent. stdout continues excluding full report/input/scenario/outcome. Per root's clarified scope, correctly represented failed/blocked incomplete handoff may be structurally consistent; this is not a completed investigation, quality PASS or model capability acceptance. The code does not invent a universal checker error for every accurate negative outcome.

## Red and green evidence

State-family red: `/tmp/m0-local-spec-fix-red.txt` — 7 failed, 1 passed in 0.36s. Incomplete, failed, missing/unknown status and contradictory validation/exception/boundary markers all incorrectly became completed; completed bounded runtime returning incomplete assessment stayed a valid positive control.

Actual mixed fakepipe initial failure: `/tmp/m0-local-spec-mixed-probe.txt` — runtime status incomplete, null final content, fixed protocol rejection, then old bridge REPORT_REQUEST_BINDING exception. The safe response retained JSON+stop. This was a real execution of pinned Holmes/wrapper using a patched fake transport, not an edited runtime status only.

Actual mixed fakepipe repaired result: `/tmp/m0-local-spec-mixed-green.txt` — 1 fake model step, 0 real HTTP, parsed report preserved as response candidate, execution blocked. Violations include ASSESSMENT_EXECUTION_MISMATCH and REPORT_OUTPUT_BINDING_MISMATCH; no capture was fabricated.

Final targeted suite: `/tmp/m0-local-spec-fix-green.txt` — **297 passed in 5.42s**. Includes strict/legacy compatibility, incomplete report controls, failed/incomplete NULL/MISSING candidate preservation, success/unknown/cross-run/cross-attempt/nonempty mismatch denials, and CLI preservation for both rejected completed assessments and accurate incomplete handoffs. Ruff checks passed. Fixture normal success now explicitly records actual `status=investigation_returned` instead of relying on omitted status.

Commands run from `/tmp/m0-local-spec-fix-code`:

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_holmes_bridge_v4.py -k 'runtime_failure_cannot or completed_runtime_may' -q --tb=short
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q --tb=short
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/ruff check scripts/m0/holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/fixtures/m0_environment/initial_report_probe.py
HOLMES_TEST_UPSTREAM=/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4 /Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmes-venv/bin/python tests/fixtures/m0_environment/initial_report_probe.py --mixed-final-tool-call
```

The mixed probe uses synthetic credentials via a patched dotenv reader and a fake child transport; no actual credentials are read. No private fields are inspected/exported by the bridge. The separate local_spec_review and local_standards_review agents are validating the final patch; their results are separate evidence, not implementer certification. Original two-axis findings and reviewer scripts/logs are left untouched.
