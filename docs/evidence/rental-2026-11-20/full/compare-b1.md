# HolmesGPT 公开评测集：OpsPilot 与 Holmes 同题同模型对照

> Holmes 按官方默认配置运行，含 OpsPilot 按只读约束不提供的工具
>
> Holmes 判分口径：默认 opspilot（上游 harness 建的 judge 文本，用 OpsPilot #666 同一 judge 判分，两家同一 judge 口径）

| 题 | OpsPilot 通过/3 | 通过率 | 标记 | Holmes 通过/3 | 通过率 | 标记 | 去 skill 断言 | 时间平移（O/H） |
|---|---|---|---|---|---|---|---|---|
| 183b_elasticsearch_index_discovery | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 183c_elasticsearch_log_search | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 183d_elasticsearch_aggregation | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 183e_elasticsearch_field_mappings | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 184_elasticsearch_index_explosion | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 185_elasticsearch_cross_region_search | 0 | 0% | 无答2 | 3 | 100% |  |  | 是/是 |
| 187_elasticsearch_disk_space | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 188_elasticsearch_mapping_explosion | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 189_elasticsearch_timeseries_gap | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 190_elasticsearch_cross_service_correlation | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 191_elasticsearch_query_profile | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 193_elasticsearch_large_mapping_search | 3 | 100% |  | 3 | 100% |  |  | 是/是 |
| 235_elasticsearch_cluster_mismatch | 2 | 67% | 摇摆、无答1 | 1 | 33% | 摇摆 |  | — |
| 245_elasticsearch_trace_large_fields | 2 | 67% | 摇摆 | 1 | 33% | 摇摆 |  | 是/是 |
| 254_elasticsearch_dr_test_log_check | 3 | 100% |  | 3 | 100% |  |  | — |
| 259_wrong_cluster_logs_confusion | 0 | 0% | 无答3 | 3 | 100% |  |  | — |
| 260_global_es_remote_cluster_logs | 3 | 100% |  | 3 | 100% |  |  | — |
| 261_time_window_gap_external_data | 2 | 67% | 摇摆、无答1 | 3 | 100% |  |  | — |
| 262_ambiguous_cluster_reference | 3 | 100% |  | 3 | 100% |  |  | — |
| 263_region_suffixed_service_siblings | 3 | 100% |  | 3 | 100% |  |  | — |
| 264_wrong_env_same_region | 0 | 0% | 无答3 | 3 | 100% |  |  | — |
| 265_multicluster_labeling_discipline | 3 | 100% |  | 3 | 100% |  |  | — |
| 266_toolset_disabled_vs_no_data | 0 | 0% | 无答3 | 3 | 100% |  |  | — |
| 267_cluster_name_alias | 3 | 100% |  | 3 | 100% |  |  | — |
| 269_mixed_sources_route_to_external | 3 | 100% |  | 3 | 100% |  |  | — |
| 275_es_field_name_correction | 3 | 100% |  | 3 | 100% |  | 是 | 是/是 |
| 276_es_numeric_severity_field | 3 | 100% |  | 3 | 100% |  | 是 | 是/是 |
| 277_es_timestamp_keyword_field | 3 | 100% |  | 3 | 100% |  | 是 | 否/否 |
| 278_es_schema_discovery | 3 | 100% |  | 3 | 100% |  | 是 | 是/是 |
| 280_es_multi_quirk | 0 | 0% |  | 3 | 100% |  | 是 | 是/是 |

## 总分（两轨分列，不合并）

- OpsPilot：72/90（80%），摇摆 3 题，缺遍 0
- Holmes：86/90（96%），摇摆 2 题，缺遍 0

## 说明

- Holmes 判分口径：默认 opspilot（上游 harness 建的 judge 文本，用 OpsPilot #666 同一 judge 判分，两家同一 judge 口径）
- Holmes 按官方默认配置运行，含 OpsPilot 按只读约束不提供的工具
- 两家总分分列，不合并、不相减；同一计分规则：每题 3 遍，只有 judge 判 A 算通过，缺遍、无回答、judge 无结论均记 0 并留在分母。
- 同模型（DeepSeek deepseek-flash，thinking 开、effort high）、同题、同 judge 接收方与模型（Z.ai glm-5.3-flashx）。
- R8-13：标「去 skill 断言」的题两家都不判 SuggestSkills 建议条数，也不跑 replay。
- R10-1：固定时间题两家都按同一平移规则改写数据与题面，送 judge 前反平移；平移量随各自运行时刻不同。
