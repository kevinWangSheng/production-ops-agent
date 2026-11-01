# 开发集 dev-v2（schema `opspilot-eval-scenario-2`）

评测开发集 v2（#430，收口合同 r1 V2-R1–R4、R7）。加载器是 `opspilot/evaluation/model.py` 的 `load_scenarios_v2`；`evals/scenarios/` 下的 v1 场景和 v1 解析器保持不变。

- `regression/`：v1 的 6 个场景复制成 `version: "2"`，刺激逐字相同，问题入口；`incident.kind` 统一为中性值 `eval`（v1 的 `fault`/`normal` 即极性，只写在答案侧 `polarity`）。
- `capability/`：能力组（12 题在 #432）。`_example/` 是 schema 样例，以 `_` 开头的目录加载器跳过。

每个场景目录名就是 `scenario_id`（`dv2-` + 6 位小写 hex）：

- `scenario.json`：模型可见一侧，精确键 `scenario_id, version, entry, incident, stimulus`；`entry` 为 `question` 或 `alertmanager`。不写类别、极性、flag 名或指标名。
- `answer.json`：只在评测侧，精确键 `scenario_id, version, category, polarity, injectors, expected_outcome, expected_output, signature, reference_report`。

清单 `dataset` 维记 `dev-v2:<dataset_tree_hash>`：对本目录下全部文件按 POSIX 相对路径排序，逐个喂入 `路径\0内容\0` 的 sha256。改动本目录任何文件（含本说明）都会改变它。

回归组与 v1 的对应（评测侧记录，不进模型输入）：

| v2 | v1 |
| --- | --- |
| `dv2-f725cb` | `eval-cart-failure` |
| `dv2-8e9455` | `eval-normal-1` |
| `dv2-f80b9b` | `eval-normal-2` |
| `dv2-2b214b` | `eval-payment-failure-1` |
| `dv2-455c6d` | `eval-payment-failure-2` |
| `dv2-c690ce` | `eval-product-catalog-failure` |
