# SPEC axis independent fix revalidation

Original axis finding and red evidence remain immutable in /tmp/m0-local-spec-review.md, /tmp/m0-local-spec-probe.py, /tmp/m0-local-spec-probe.txt.

## Reviewed artifact

- Pure tracked-source archive: /tmp/m0-local-spec-fix-code
- Base HEAD: af24646eb02da3de2839005a7a3ea361fd7ac914
- Patch: /tmp/m0-local-spec-fix.patch
- SHA256: 1b0553501438c50722c6c8d88daaeaa08414d19428f63660ebd9067fe58aa319
- Bridge SHA256: 5d48f6634d191d0e08eb8bf5ae8ad3a3e79a47144fd49b65aac68b6939a2b220
- Bridge tests SHA256: 7e0efff250da72d2f2c38b73188a7800f83f5d8065d7ef80a8743f8c973faef9
- Actual-Holmes fake-pipe probe SHA256: b40e51ca8d7ec42f003815e0a028436034068d05f1ef100a85e1889db5fe521d
- Original m0-01 HEAD still af24646; clean worktree. This is proposed patch verification, not evidence of application/commit/PR CI.

## Independent result

P2 is resolved in the reviewed patch. Runtime failed/incomplete/unknown and explicit validation/exception/boundary failures no longer derive completed execution from a parseable report. Parsed report/raw/capture remain available. Genuine completed execution with incomplete assessment stays completed with accurate handoff. CLI full --output also preserves report, execution, reasons, inputs and provenance for accurate contract-consistent handoffs; ordinary completed/no-handoff summaries retain existing scope.

Actual mixed final JSON/tool-call fake-pipe run reproduces runner incomplete with original final_business_content null. The bridge preserves the same-attempt safe response as an unaccepted candidate, records source and original-null reasons, does not fabricate ReportCapture or rewrite original result, and does not certify completed execution. Success/unknown missing-content, wrong Run/attempt and nonempty conflicting-content cases still reject.

## Checks and boundaries

1. Reviewer-authored 10-case status/assessment matrix, each exercising load_packet -> check_outcome and actual subprocess CLI: all passed. Covers success, failed, incomplete, missing/unknown status, protocol rejection, contradictory success+rejection, truthful incomplete-assessment handoffs, preservation and 0600 output. Script /tmp/m0-local-spec-revalidate-v3.py; output /tmp/m0-local-spec-revalidation-matrix-final.txt.
2. Reviewer-authored eight-case fallback/reportless matrix: all passed. Script /tmp/m0-local-spec-fallback-probe.py; output /tmp/m0-local-spec-independent-fallback.txt.
3. Actual pinned Holmes loop with fake HTTP transport, one mixed final response: passed, 0 real HTTP. Command uses HOLMES_TEST_UPSTREAM=/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4 and corresponding holmes-venv Python, tests/fixtures/m0_environment/initial_report_probe.py --mixed-final-tool-call. Output /tmp/m0-local-spec-independent-mixed.txt.
4. Six existing/updated targeted modules independently rerun with PYTHONDONTWRITEBYTECODE=1 and pytest -p no:cacheprovider; output /tmp/m0-local-spec-independent-tests-final.txt.

The early reviewer matrix expected all failed/incomplete outcomes to produce checker errors. This was too strict: contract consistency may correctly describe a failed/blocked handoff, without claiming a successful investigation. That expectation was corrected before final verification; initial matrix outputs are retained as review history. Explicit runtime rejection remains visible in handoff reasons/full CLI audit. No acceptance standard was weakened.

No private protocol/credential reads, live backend/model calls, PG/container operations, repository edits or remote review actions. Local review replacement only; no claim that remote reviewers executed or that report quality/M1 entry is now passed.
