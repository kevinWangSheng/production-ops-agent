# PR16 projector 执行前可信性校验

GitHub bot P1 3981914677；基线d61f80a。原路径把证据包自报hash当作可执行源码的可信依据；校验完整性不能替代可信来源。新增固定历史(source hash,完整dependency tuple)清单及独立于输入的当前仓库固定文件组合，所有入口在任何函数/default/decorator/annotation执行前验证整组字节，编译仅用已验证bytes。未知源码不执行、不复制。

完整隔离代码自测651 passed/44 PG默认skip（21.27s），相关安全/合同333项通过；实际wrapper拒绝未知projector为0执行/0凭据读取/0传输，marker仅无害临时文件，不读秘密或答案。原5类marker执行红结果保留；正常report-only及历史19/19、12/12重放保原结构结果，normal02旧FAILED_EVIDENCE_AS_FACT不改变。

这是执行前源码认证，不是新OS sandbox或整体隔离证明；没有真实模型/trace/服务操作，没有本地review Agent，按用户要求继续交GitHub bot复审。完整active假传输probe在原d61和候选均因既有截止条件失败，两个输出保留，不冒充通过，也不倒推旧真实InternalServerError原因。新实验必须重新固定授权时间，不能放宽旧已过期查询授权来让测试变绿。

以下保留实现者的版本、命令与详细边界；root实际全量结果以上文与对应日志为准。

# Bot SECURITY P1 3981914677 — projector authenticity before execution

Base `d61f80a4f6b5797bd0c02d3ff2a7c8ee58844ee6`.
Readonly original `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01` was not edited; no git index/commit, credentials/private/DB copy, service/PG/network/model operation or review-agent invocation. Code-only tracked archive `/tmp/m0-bot-projector-trust-code`; independent original-code comparison archive `/tmp/m0-bot-projector-trust-baseline`. Existing interpreters only, no installation.

Frozen patch `/tmp/m0-bot-projector-trust-fix.patch` SHA256:
`df936571883011a91a8a4d11c7a21f0098bbab048df336516f60a368fa7c52ea`.

## Approved trust source, not caller assertions

Root confirmed the exact historical pins against retained bytes and review records:

1. `7fd7326644b26fe00d0a8bad25aab4b865dfbd453bb5a7b4fa491eda5539a676`, no dependencies; retained `docs/evidence/m0-real-environment/immutable/<hash>.py.txt`.
2. `22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8`, no dependencies; actual fault/normal historical replay in `round-02-bridge-dependency-review.md:24`.
3. Same22a96 source with only `legacy_projections=507d4205fb5e65cdd831003ae45096703050534873018cbb0016c4f4678e4caf`; `round-02-semantics-preflight-review.md:19-21`.
4. `e15d10cc9fc5ea523f8fd73af5e71e08596bc87481d6f78f1f0c8828ec783b63` with exactly `legacy_projections=0ed7d37aeca6057f2fd59ca2da692cab73b1247bd0e209555200ef74f32e27fc` and `trace_view=0fdac927db8e633bdcae63d2dbacc988e3b3c5d66977ddddea22dbafb8b88f4b`; `round-02-offline-view-source-manifest.json` and `round-02-bridge-dependency-review.md:18-32`.

The current trusted tuple is derived only from fixed installed repository paths `scripts/m0_environment/{holmes_baseline,legacy_projections,trace_view}.py`, not bundle paths, bundle hashes, environment variables, operator-supplied manifests or directory enumeration. Historical trust is an exact source+sorted dependency tuple, not independent hash membership. No automatic trust of content-addressed filenames or arbitrary fixture projectors exists.

## Bounded mechanism

New `scripts/m0/projector_trust.py` rejects unknown tuples/names/duplicates before reading candidate executable paths; verifies all candidate source and dependency bytes before any selected code executes; rejects symlinks/nonregular files. Callers compile the returned verified bytes. `replay_projection` and standalone `load_dependencies` both require the full authenticity gate, so decorators/defaults/annotations cannot run via a secondary entry. Rechecking in dependency loading also remains authenticated; no unchecked path read is used for compilation.

Initial report-only and active imports authenticate after resolving paths and before reading/copying executable files or replaying entries. Standalone verify_initial_entry also authenticates; replay remains independently gated. Unknown bundles preserve unexecuted JSON manifest/input audit and handoff, without copying source/dependency code into initial-evidence or invoking a projector. Default source location is now the fixed current repository source rather than the old hardcoded environment-worktree path.

This is executable authenticity against an untrusted/altered evidence bundle, not an OS sandbox, code signer or resource/DoS platform. Prior review assumed an explicitly trusted caller providing executable pins; that prior scope is not relabeled as an OS security guarantee. Approved functions still execute with ordinary Python facilities, but only after their complete source/dependency bundle is independently authorized.

## Tests and red evidence

`/tmp/m0-bot-projector-trust-red.txt` first showed four self-supplied executable paths were accepted. An unknown dependency name was already rejected by the ordinary DTO; the low-level forged-object test now checks the loader also rejects it.

`/tmp/m0-bot-projector-trust-marker-red.txt` runs the final security tests against original d61 implementation only and explicitly records harmless temporary marker outcomes:
body/default/decorator/annotation/dependency executed=True; unknown_dependency executed=False (previous direct loader silently ignored it). All6 rejection assertions fail on that original implementation. No test accesses any actual .env, secret, private provider field or answer file.

Final `/tmp/m0-bot-projector-trust-green.txt`: **333 passed in6.67s**. Ruff and format checks pass. Coverage includes self-consistent malicious source/dependency hashes; bad bytes claiming known hashes; unknown names; unreviewed combinations of otherwise known pins; standalone dependency loader; report-only import before copy; fixed current bundle; all four historical tuples relocated byte-for-byte; symlink/nonregular path rejection. Existing scope/hash/version checks remain.

The old identity-projector contract fixtures are trusted only in tests: their fixed literal bytes/hash are pinned before mutation by pytest monkeypatch, and CLI contract tests use a test-only Python bootstrap that adds only that fixed fixture hash. No production flag, environment setting, manifest field or caller-provided pin extends trust. Marker attack tests and real wrapper probes use the production trust set without that bootstrap.

## Actual wrapper and historical seams

- `/tmp/m0-bot-projector-trust-wrapper-denied.txt`: actual wrapper report-only unknown self-pinned decorator source returns initial_evidence unknown, marker absent, source not copied, 0 credential reads, 0 model/tool transports, 0 real HTTP.
- `/tmp/m0-bot-projector-trust-wrapper-valid.txt`: actual pinned Holmes code with fake transport, trusted historical initial bundle: strict report-only PASS,1 fake model step,0 real HTTP; existing initial-provenance and scope negative probes retain expected results.
- Readonly legacy replay summaries: `/tmp/m0-bot-projector-trust-fault-legacy.txt` fault01 source22a96,19/19, violations[]; `/tmp/m0-bot-projector-trust-normal03-legacy.txt` normal03 source22a96,12/12,[]; `/tmp/m0-bot-projector-trust-normal02-legacy.txt` normal02 source7fd732,15/15,existing FAILED_EVIDENCE_AS_FACT retained. No original raw/view/result/private file was modified or copied; only metadata stdout summaries were saved. These are structural historical replays, not quality or current acceptance PASS.

Larger active fakeprobe limitation: `/tmp/m0-bot-projector-trust-current-runtime.txt` and `/tmp/m0-bot-projector-trust-baseline-runtime.txt` both fail with the same IndexError/failed result on patched and untouched d61 code. Root traced the fixed query authorization deadline in holmes_baseline.py to `2026-09-10T17:14:30+00:00`, now expired; the query guard denies before backend, while the fake model expects bindings. We did not raise/remove that guard or turn old authorization into a new runtime experiment. Report-only malicious/valid probes above are the relevant executed security evidence; active probe renewal belongs to future authorized calibration.

## Commands

From `/tmp/m0-bot-projector-trust-code`:

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_projector_trust.py tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q --tb=short
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/ruff check scripts/m0/projector_trust.py scripts/m0/holmes_bridge.py scripts/m0_environment/initial_evidence.py tests/test_m0_projector_trust.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/fixtures/m0_environment/initial_report_probe.py
HOLMES_TEST_UPSTREAM=/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4 /Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmes-venv/bin/python tests/fixtures/m0_environment/initial_report_probe.py --untrusted-projector-only
```

Omit `--untrusted-projector-only` for trusted report-only positive probe. Legacy replay CLI uses `--legacy-v3`, original safe Run directory, and the explicit retained reviewed source file/hash; no output artifact option was used.

Final production hashes:
- projector_trust.py `638a141ac5b73baff37768d5470142b909c763dda3053f7131f434dd73257398`
- holmes_bridge.py `49a0e36f7fa9796345e2324a4675fdfb9063c97606d9d601218b277c86449e8f`
- initial_evidence.py `52bea426a73d6ffe35b8f41b14f683accb31b522fb3c714c77e8a98a949bda5b`

Root owns final full-suite/patch application/filesystem approval and GitHub bot security review. This is author self-test, not independent certification; no local review Agent was used and no real model acceptance/qualityFAIL was altered.

Final test/probe hashes:
- tests/test_m0_projector_trust.py `d5f1d88a3cbedcef0b008c7b3a2ae16b82f44f0f24af2e386fee6b9cb0e6ba6f`
- tests/test_m0_holmes_bridge.py `ab3ab00e5e8f42c45a2a2c0e3bf5dbf2ee3827e2b42335f2d2f2393ee6747a83`
- tests/test_m0_holmes_bridge_v4.py `414b9d87fffe4749ee8f51cbeb3199d74caee92e927a467cf22b32a884d9100f`
- tests/fixtures/m0_environment/initial_report_probe.py `c4d34760de96300d1e209a07a112f0f6c6c108c1feb9a2cbf9adfe5f5f7e6412`
