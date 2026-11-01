# Issue #430 acceptance ambiguities

- 回归 stimulus 映射按答案侧注入器推导：`polarity=positive` 时取 `flagd.params.flag`，与 v1 `answer.json.injected.flag` 唯一匹配；`polarity=negative` 时取任一 v1 normal 场景，因为两个 normal 的 stimulus 当前逐字相同。
- “stimulus byte-for-byte”按 stimulus JSON 对象的规范化 UTF-8 序列比较；scenario.json 外层格式、空白和键顺序不属于 stimulus payload。若合同要求保留原始 JSON 子串字节，lead 需提供可观察的原始片段 API 或固定序列化规则。
- H5 的“metric names”没有在 brief/seams 中给出完整枚举。测试目前覆盖现有评测证据中可核查的指标词，并覆盖全部 v2 category 枚举、`fault`/`normal` 和 v1 flag 名；新增指标词需要 lead 在 schema/fixture 中给出白名单后补入。
- “old summary 缺少 runbook/dataset 读作 unknown”没有在 seams 中指定返回字段或迁移 API。测试按现有 `read_legacy_summary()` 公共入口，预期返回对象暴露 `dimensions[...].kind == "unknown"`；若实现选择另一公开读取入口，需将该断言移到对应入口，保持“不报错 + unknown”语义不变。
- `tests/fixtures/eval_structural` 的冻结 `fixture.json` 由 `scripts/eval_structural_fixture.py` 读取实现侧 `REQUIRED_DIMENSIONS` 生成。当前实现仍是 28 维；已在隔离临时副本中仅将生成器看到的维度列表扩为 30 维并重生成 fixture，recording 字节保持不变。实现合并后需由 lead 再用真实实现运行生成器核对 fixture hash。
