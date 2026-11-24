| Group | Question | Rerun round | Full run (pass/n) | Rerun (pass/n) | Rerun categories | Flag |
|---|---|---|---|---|---|---|
| b1 | 183b_elasticsearch_index_discovery | rerun-o-b1-x1 | 3/3 | 1/1 | ok |  |
| b1 | 183c_elasticsearch_log_search | rerun-o-b1-x1 | 3/3 | 1/1 | ok |  |
| b1 | 183d_elasticsearch_aggregation | rerun-o-b1-x1 | 3/3 | 1/1 | ok |  |
| b1 | 183e_elasticsearch_field_mappings | rerun-o-b1-x1 | 3/3 | 1/1 | ok |  |
| b1 | 184_elasticsearch_index_explosion | rerun-o-b1-x1 | 3/3 | 1/1 | ok |  |
| b1 | 185_elasticsearch_cross_region_search | rerun-o-b1-x3 | 0/3 | 0/3 | ok, ok, ok |  |
| b1 | 187_elasticsearch_disk_space | rerun-o-b1-x1 | 3/3 | 1/1 | ok |  |
| b1 | 188_elasticsearch_mapping_explosion | rerun-o-b1-x1 | 3/3 | 1/1 | ok |  |
| b1 | 189_elasticsearch_timeseries_gap | rerun-o-b1-x1 | 3/3 | 1/1 | ok |  |
| b1 | 190_elasticsearch_cross_service_correlation | rerun-o-b1-x1 | 3/3 | 1/1 | ok |  |
| b1 | 191_elasticsearch_query_profile | rerun-o-b1-x1b | 3/3 | 1/1 | ok |  |
| b1 | 193_elasticsearch_large_mapping_search | rerun-o-b1-x1b | 3/3 | 1/1 | ok |  |
| b1 | 235_elasticsearch_cluster_mismatch | rerun-o-b1-235-isolated | 2/3 | 3/3 | ok, ok, ok |  |
| b1 | 235_elasticsearch_cluster_mismatch | rerun-o-b1-x3 | 2/3 | 1/3 | ok, ok, ok |  |
| b1 | 245_elasticsearch_trace_large_fields | rerun-o-b1-x1b | 2/3 | 1/1 | ok |  |
| b1 | 254_elasticsearch_dr_test_log_check | rerun-o-b1-x1b | 3/3 | 1/1 | ok |  |
| b1 | 259_wrong_cluster_logs_confusion | rerun-o-b1-x3 | 0/3 | 3/3 | ok, ok, ok |  |
| b1 | 260_global_es_remote_cluster_logs | rerun-o-b1-x3 | 3/3 | 3/3 | ok, ok, ok |  |
| b1 | 261_time_window_gap_external_data | rerun-o-b1-x3 | 2/3 | 3/3 | ok, ok, ok |  |
| b1 | 262_ambiguous_cluster_reference | rerun-o-b1-x3 | 3/3 | 3/3 | ok, ok, ok |  |
| b1 | 263_region_suffixed_service_siblings | rerun-o-b1-x3 | 3/3 | 3/3 | ok, ok, ok |  |
| b1 | 264_wrong_env_same_region | rerun-o-b1-x3 | 0/3 | 3/3 | ok, ok, ok |  |
| b1 | 265_multicluster_labeling_discipline | rerun-o-b1-x3 | 3/3 | 3/3 | ok, ok, ok |  |
| b1 | 266_toolset_disabled_vs_no_data | rerun-o-b1-x3 | 0/3 | 3/3 | ok, ok, ok |  |
| b1 | 267_cluster_name_alias | rerun-o-b1-x3 | 3/3 | 2/3 | ok, ok, ok | drop (full all-pass, rerun not) |
| b1 | 269_mixed_sources_route_to_external | rerun-o-b1-x3 | 3/3 | 3/3 | ok, ok, ok |  |
| b1 | 275_es_field_name_correction | rerun-o-b1-x1b | 3/3 | 1/1 | ok |  |
| b1 | 276_es_numeric_severity_field | rerun-o-b1-x1b | 3/3 | 1/1 | ok |  |
| b1 | 277_es_timestamp_keyword_field | rerun-o-b1-x1b | 3/3 | 1/1 | ok |  |
| b1 | 278_es_schema_discovery | rerun-o-b1-x1b | 3/3 | 1/1 | ok |  |
| c1 | 283_todowrite_multistep_audit | rerun-o-c1 | 3/3 | 1/1 | ok |  |
| c2 | 162_get_skills | rerun-o-c2 | 0/3 | 0/3 | ok, ok, ok |  |
| c2 | 89_skill_missing_cloudwatch | rerun-o-c2 | 2/3 | 2/3 | ok, ok, ok |  |
| c2 | 90_skill_basic_selection | rerun-o-c2 | 1/3 | 2/3 | ok, ok, ok |  |
| c2 | 94_skill_transparency | rerun-o-c2 | 1/3 | 2/3 | ok, ok, ok |  |
| c2 | 95_skill_memory_leak_detection | rerun-o-c2 | 3/3 | 3/3 | ok, ok, ok |  |
| c2 | 96_no_matching_skill | rerun-o-c2 | 3/3 | 3/3 | ok, ok, ok |  |
| c5k | 100a_loki_historical_logs | rerun-o-c5k | 3/3 | 0/1 | no_report | REGRESSION (passed in full, 0 now) |
| c5k | 100a_loki_historical_logs | rerun-o-c5k-100a | 3/3 | 3/3 | ok, ok, ok |  |
| c5k | 101_loki_historical_logs_pod_deleted | rerun-o-c5k | 3/3 | 1/1 | ok |  |
| c5k | 102_loki_label_discovery | rerun-o-c5k | 3/3 | 1/1 | ok |  |
| c5k | 102a_loki_logs_transparency | rerun-o-c5k | 3/3 | 1/1 | ok |  |
| c5k | 102b_loki_multiple_pods | rerun-o-c5k | 3/3 | 1/1 | ok |  |
| c5k | 282_loki_custom_stream_label | rerun-o-c5k | 3/3 | 1/1 | ok |  |
| c5n | 257_victorialogs_log_search | rerun-o-c5n | 3/3 | 1/1 | ok |  |
| c5n | 258_victorialogs_streams | rerun-o-c5n | 3/3 | 1/1 | ok |  |
| c5n | 259_loki_historical_logs_pod_deleted_docker | rerun-o-c5n | 3/3 | 1/1 | ok |  |
| c6 | 217_database_sql_query | rerun-o-c6 | 3/3 | 1/1 | ok |  |
| c6 | 218_mysql_query_performance | rerun-o-c6 | 3/3 | 1/1 | ok |  |
| c6 | 219_mariadb_query_performance | rerun-o-c6 | 3/3 | 1/1 | ok |  |
| c6 | 222_clickhouse_query_performance | rerun-o-c6 | 3/3 | 1/1 | ok |  |
| c6 | 223_sqlserver_query_performance | rerun-o-c6 | 3/3 | 1/1 | ok |  |
| c6 | 229_mongodb_schema_discovery | rerun-o-c6 | 3/3 | 1/1 | ok |  |
| c6 | 230_mongodb_aggregation_analysis | rerun-o-c6 | 3/3 | 1/1 | ok |  |
| sregym | p13_secret_rotation_stale_env_credentials_astronomy_shop | rerun | 1/3 | 1/3 | ok, ok, ok |  |
