# PR16 question 输入与人工状态审计修复

基线a783fc2ee56a87bc45f11c56de0f620a731cfd4a，Code Review发现3982439739/3982439749。同提交Security于18:54:30Z完成无发现，该结果不覆盖本修复。按用户要求直接使用GitHub bot，没有重启本地eng:code-review。

## 最终实现与验证

Question输入：argparse后先检查已知私有路径、链接和非普通文件，不读取内容；合法路径仍等原scope/phase/run-id检查后、创建归档前才读取。共享reader拒绝.env/.env.*及private-protocol的大小写/解析路径别名；UTF8、CRLF原字节保留。原client认证dotenv路径不改，所有否定用例为合成非秘密文件+read-spy，未读真实.env。这是路径检查，不是通用DLP或OS隔离/并发敌对文件系统证明。

人工状态：当前M0没有pause转移，paused保留schema表达但严格校验拒绝；cancelled/waiting_human必须由最后接受的cancel/correct解释，包括没有控制事件的情况。原paused测试保留为拒绝用例，合法运行与人控配对仍通过；不新增暂停平台，不改schema/legacy/DB。

root合并隔离副本最终677 passed/44 PG默认skip（19.30s），ruff通过。首次误从/tmp收集导致4个scripts import错误，改回副本cwd后发现3项真实校验顺序回归；最终将“路径检查”和“内容读取”分开，保留原scope拒绝优先级，没有更改旧测试期待去迁就实现。日志分别保留。

最终代码补丁SHA256：a78b8c179285db66b2f1b82236478c2bd4d58beb1cf8e74a8d90699821956d04

```json
{
  "scripts/m0_environment/holmes_baseline.py": "c3b7a353dbde8f2f7095e7d8b918a04959336711f56a5f13c7fd800bf8b42eb6",
  "scripts/m0_environment/initial_evidence.py": "5e28e5b7f705593ea8cf7ab77a18167e5e4ed9ba65c418679d90e88f7d109732",
  "tests/test_m0_question_paths.py": "bb93c64e7e07c469955979eac423f09389372056893ba5c284d5b8c8792a9dc5",
  "scripts/m0/outcomes_v4.py": "445db5cf77cee4960130026e6feca4c89877347ca3420d019c97629a353a6487",
  "tests/test_m0_outcomes_v4.py": "ff143d067d95668304e364f8b0634c5da09792a75ed3efc060a1579336876bdd"
}
```

以下作者原始记录是合并前版本，原hash及33/195项结果保留为历史；其中“立刻读取question”的初版已按上述集成失败修正，不当作最终实现。最终字节以本节hash和本提交为准。

## Question作者初版

# PR16 bot 3982439739 question path fix

Base: a783fc2ee56a87bc45f11c56de0f620a731cfd4a; only tracked git archive into /tmp/m0-question-path-fix. Original worktree/index untouched. No real .env or private artifact content read/copied; all rejected file tests use SYNTHETIC_NON_SECRET. No service/model/backend/PG/network calls or installs.

- read_business_question delegates shared _bytes and is invoked immediately after argparse, before metadata reads, archive creation and client setup. Covers both report versions and all phases through one CLI read site.
- _bytes rejects .env/.env.* and private-protocol path components case-insensitively in both lexical and resolved paths, retaining symlink-leaf/nonregular denial. Symlinked parent aliases to restricted names resolve to denied paths. UTF-8 business content retains exact bytes including CRLF and Unicode after decode/encode; no content rewriting/DLP.
- Authentication's separate dotenv_values source remains untouched.
- This bounded path gate is not OS sandbox isolation, secret content detection or concurrent hostile filesystem protection.

Regression: same tests against original tracked archive: 12 failed / 4 passed (/tmp/m0-question-red.txt). Actual legacy CLI test reaches SYNTHETIC_SOURCE_OPENED_BEFORE_DENIAL read-spy on old source; patched CLI rejects before the source open, run directory creation and run_child call. New helper tests account for two missing-symbol red failures; older nonregular checks already passed.

Patched command: /Users/shenghuikevin/dev/AI/production-ops-agent-m0-01/.venv/bin/python -m pytest tests/test_m0_question_paths.py tests/test_m0_initial_evidence.py -q
Result: 33 passed in 0.33s (/tmp/m0-question-green.txt). Ruff check passed on all 3 changed files; format applied.

Patch: /tmp/m0-bot-question-path-fix.patch
SHA256 db5229118817e0cd8d485f51e1fe42f7266717181f55f205015348614e62d625
Changed file SHA256:
907e6f1deb9881d38e5a126dcc4614f43b8a433dc98d21b9b829adbf6c506ba5 scripts/m0_environment/holmes_baseline.py
c6310c56434afaae88b5034cb8dbf3c5d89e67c5d34ef5241b48a2bdba310cd1 scripts/m0_environment/initial_evidence.py
1e0a59faa05f123fb0a319e093a485b135b8d273c30bc3378776ce2d2b670589 tests/test_m0_question_paths.py

Root owns applying with required filesystem approval, full combined checks, durable task evidence, commit/push and GitHub bot review. No independent review was spawned/performed here.

## 人工状态作者记录

# Bot P1 3982439749 — bounded human-state authority

Base: `a783fc2ee56a87bc45f11c56de0f620a731cfd4a`.
Code-only tracked archive: `/tmp/m0-bot-human-state-code`.
Original worktree unchanged. No ignored/private/DB copy, original writes/index/commit, model/network/PG/services, local review agents or changes to parallel runtime/initial_evidence work.

Frozen patch `/tmp/m0-bot-human-state-fix.patch` SHA256:
`4e801ecc43653416f3c1c61f488f05d8ebd5b0a2e043522c37912fd61bcc3f25`.

Two file hashes:
- scripts/m0/outcomes_v4.py `445db5cf77cee4960130026e6feca4c89877347ca3420d019c97629a353a6487`
- tests/test_m0_outcomes_v4.py `ff143d067d95668304e364f8b0634c5da09792a75ed3efc060a1579336876bdd`

## Red evidence

`/tmp/m0-bot-human-state-red.txt`:4 failed,5 passed,70 deselected in0.14s. The original paused-after-new_run matrix item and paused/cancelled/waiting_human with no control events all returned checker[]. The formerly incorrect paused positive was retained in the existing parameter matrix and changed to a negative assertion; it was not deleted.

## Minimal change

The strict v4 seam keeps its forward mapping (last cancel→cancelled, last correct→waiting_human) and now also checks the reverse mapping. A cancelled or waiting_human state requires the matching latest accepted cancel/correct event, even when the audit list is empty. The current bounded StepStore has no pause transition, so paused is always CONTROL_STATE_MISMATCH. No pause feature, migration, schema extension or timing invention is introduced.

Unsupported states remain representable for audit but cannot be certified. Existing runtime progress/blocked/failed/budget-exhausted/completed outcomes and properly audited human states retain their prior checks. Explicit legacy/schema/history remain unchanged. Tests retain both human-control→new_run and new_run→human-control sequences, correct and wrong latest-event pairs, and paused borrowing unrelated control actions.

## Self-test

Commands from `/tmp/m0-bot-human-state-code`:

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_outcomes_v4.py -k 'latest_new_run_allows or human_state_without_control' -q --tb=short
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py -q --tb=short
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/ruff check scripts/m0/outcomes_v4.py tests/test_m0_outcomes_v4.py
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/ruff format --check scripts/m0/outcomes_v4.py tests/test_m0_outcomes_v4.py
```

Final `/tmp/m0-bot-human-state-green.txt`: **195 passed in0.20s**. Ruff and format pass. Root will combine with the separate question-path patch, run the full suite, apply with filesystem approval and obtain fresh GitHub bot review. No local review Agent was invoked, and these are implementer tests rather than independent certification.
