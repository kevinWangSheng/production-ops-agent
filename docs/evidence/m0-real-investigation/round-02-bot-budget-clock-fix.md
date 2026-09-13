# CI 截止时间 rollover 修复

CI run 34553834336 在日期跨过实验截止时间后，四个 Budget 测试还直接使用现实 clock，未进入原本要验证的未知用量/释放/阶段预算断言。修复仅给 `Budget` 加默认仍为现实 `time.time()` 的 `_clock()` seam，并让这四个测试显式注入 `PROFILE.deadline - 1`；生产 deadline、费用、阶段上限和历史 ledger 不变。

作者定向48项通过；root基于合并后的question/human与projector代码再次运行 `make check`，结果见 `round-02-bot-budget-full-check.txt`。没有模型、trace、PG、服务或依赖安装操作。旧CI失败原因与作者补丁记录保留，未把环境失败改写为代码通过。

补丁SHA256：`126e3c41caca49a1f9f301028096fde3e177b42bc206767f1a8e4b9a2d74276b`；基线 `aefa0d7eb7350bff836caf3504bf01e19611db78`。这只是离线测试时钟稳定性修复，不延期旧实验授权，不开启新的真实运行。

作者原始说明：

# CI deadline rollover — round02 budget test clock

Base `aefa0d7eb7350bff836caf3504bf01e19611db78`.

The original worktree is read-only and unchanged. Isolated tracked archive: `/tmp/m0-bot-timing-test-code`; no `.env`, database, private run, service, network or model access. Production `Profile.deadline` remains the fixed real contract deadline (`2026-09-11T01:49:00Z`); it is not changed.

## Failure and bounded fix

After UTC rollover, four Budget tests called `reserve` after the real deadline and failed before exercising their intended phase, unknown-usage and restart assertions. The test module now scopes a `predeadline_budget_clock` fixture to exactly these four tests. `Budget` adds a tiny `_clock()` seam used only for its timestamp/deadline reads; production default remains `time.time()`. The fixture returns `PROFILE.deadline - 1`, so all intended deterministic budget assertions run. Other tests—including process supervisor timing, envelope checks and any test that explicitly monkeypatches the production deadline—use the real clock and are not masked by the fixture.

No production deadline, phase limit, reservation amount, status semantics or historical ledger is altered. The fixture is opt-in per test via `pytest.mark.usefixtures`, not an autouse or runtime switch.

## Verification

Original failing CI artifact: `run logs 34553834336` (root-provided; four Budget tests failed after rollover). In the isolated archive:

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01/.venv/bin/python -m pytest tests/test_m0_holmes_round02.py -q --tb=short
```

Result: **48 passed in 1.62s**. Existing tests use synthetic temporary ledgers; no external allocation or real currency spend. The patch changes only:

- `scripts/m0_environment/round02.py` — `_clock` method and its three internal timestamp calls.
- `tests/test_m0_holmes_round02.py` — scoped fixture and four decorators.

Patch: `/tmp/m0-bot-timing-delivery.patch`, SHA256 `126e3c41caca49a1f9f301028096fde3e177b42bc206767f1a8e4b9a2d74276b`.

File hashes:
- `scripts/m0_environment/round02.py`: `0dad312da9de2c666ac9367109ccf99c95bbd2b77e077c73b773aa95abfb0623`
- `tests/test_m0_holmes_round02.py`: `7bf93b283524da6271d8041a8fe0401e53f98fe77ca977bc88dc1619a2bf7c15`

This is implementer self-test; no local code-review agent was invoked. Root will apply the patch with the approved command, run the aggregate check and obtain the GitHub bot review. Production deadline remains real and will still be enforced in runtime paths.
