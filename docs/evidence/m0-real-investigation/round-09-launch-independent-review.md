# Round 09 launcher 独立复核

日期：2026-09-13

独立复核对象：`scripts/m0_lab/round07/launch.py`、`tests/test_m0_round07_launch.py`。

- `.env` 默认从当前仓库根解析，并支持 `M0_ENV_FILE`/`--env-file`；Holmes Python 支持仓库相对默认值及 `M0_HOLMES_PYTHON`/`--holmes-python`，缺失或不可执行在启动前明确失败。
- container arm 只构造 digest-pinned Docker 命令，固定 `run --rm -i --network none`、packet、runner 参数与仓库 `tmp/` 输出挂载；额外命令、镜像、mount、网络参数均拒绝。
- 命令验证在 `read_key()` 前执行；`/bin/cat` 反例确认拒绝路径不读取或注入凭据。
- candidate/upstream 的 HTTP cap、ledger 计数和参数边界未放宽。

结论：未发现 P0/P1/P2。该复核不替代 GitHub bot review；当前提交仍需 CI 与最新 HEAD bot 覆盖。
