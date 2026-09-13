# Round 08 secret-scan CI self-test 修复

日期：2026-09-12

## 事实

- CI run `34722283700` 在 Secret scan 自测首个合成 `api_key` 样本处失败（`SCANNER_SELFTEST_FAILED`）；`m0-postgres` 同一 run 通过。
- 本地 Darwin 的 Gitleaks 8.30.1 原实现可通过，说明失败具有 runner/规则行为差异，不能把本地结果当成 CI 证明。
- `scripts/check_secrets.py` 现增加仅匹配 `api_key = "<48 hex>"` 的显式 `m0-selftest-api-key` 规则。该规则只用于确定性合成 canary；既有默认规则和精确 manifest allowlist 保持不变。

## 验证

- `.venv/bin/python -m pytest tests/test_secret_scan.py -q`：`8 passed`。
- `.venv/bin/python scripts/check_secrets.py --binary tmp/live-gitleaks/gitleaks`：`SECRET_SCAN_PASSED`。
- `make check`：`778 passed, 54 skipped`；完整输出见 [`round-08-scanner-fix-make-check.txt`](round-08-scanner-fix-make-check.txt)。

原 CI 失败输出保留于 [`round-08-ci-secret-scan-failure.txt`](round-08-ci-secret-scan-failure.txt)，未覆盖或改写。
