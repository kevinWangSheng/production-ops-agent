# PR16 最终 review disposition（2026-09-11）

## 已修复并复验

- `original_user_content`/`actual_user_content`：无 imported views 必须保持原字节；有 verified views 只允许确定性的 evidence append 或原文已包含且完全匹配的 views。
- stopped control generation 的 prepared 请求：无 clocks 仍必须由 `new_run` action 授权，cancel/correct generation 拒绝。
- question source：`.env*`、private-protocol、`.netrc`、`.npmrc`、`.pypirc`、`.aws/credentials/config`、`.docker/config.json`、SSH key 等路径在读取前拒绝。
- Envoy access-log projection：严格 pinned token/ISO/tail 校验、版本化 v4、legacy v2/v3 replay 保真、实际 proxy.access 接线和 AST helper whitelist。

## 合同裁定

- GitHub Security Review 按用户明确指示忽略；本地安全替代检查已通过。
- “恢复 aggregate paid-request limit”评论与当前项目 AGENTS 的明确授权冲突：用户已取消上一轮固定总量/请求/trace 上限。仍保留每 Run deadline、tool/query/request 限制、provider usage 记账、unknown 保留和只读/数据出口边界。不得把旧账本改写为新实验。

## 未闭合但非代码修复项

旧 m003e-normal-02/m003d-fault-02 报告原文的 P2 质量失败继续保留；新 projection 修复不倒推旧报告通过。当前 no-new-model/trace 约束下不重跑旧报告。
