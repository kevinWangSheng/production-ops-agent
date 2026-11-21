# Holmes 题集全量（OpsPilot #666 + Holmes #668，2026-11-21 04:29–09:30 UTC）

性质：有界真实运行（opspilot-tool-1，main `8b912caa`，无代码改动）；53 题 × 3 遍 × 两侧，DeepSeek 真调，judge glm-5.3-flashx（两侧同一 opspilot 口径）；`--trace lab`。不是生产证明。

- 命令：`/home/opspilot/rental-logs/full/{lane,lane2,lane3,o_round,h_round,finish}.sh`（主机，不入库）。两线：B（主 checkout，端口 30000+）、S（poa-lane-d，35000+）；C5 全在 S 串行，同题两侧顺序跑。上游 K8s 禁用题（259_loki、257、258）不带 `--k8s-env-file`，其余都带。ES：自起 ES 8.19.25（127.0.0.1:30920，Holmes 侧 localhost），只读 key 由驱动用 elastic 基本认证建。
- 结果表：`compare-<组>.md/json`（merge.py）；各轮 `scores.json` 在 `opspilot/holmes-evals-full-o-*/`、`holmes/holmes-compare-full-h-*/`；trace 链接在各 summary.json `trace.roots`。
- 主集合（不含 C1-U2）：OpsPilot 133/159（A 133、B 11、no_report 15、env_broken 0）；Holmes 150/159（A 150、B 4、no_report 5、env_broken 0）。
- OpsPilot no_report 15 遍全是产品转人工（`REPORT_REJECTED_BY_PRODUCT`，Run 终态 waiting_human、decision handoff），judge 未调用：185×2、235×1、259_wrong×3、261×1、264×3、266×3、90×2。
- Holmes no_report 5 遍：229×3、230×2，pytest call 阶段 KeyError（229 t1）或 Skipped，记 HOLMES_NO_ANSWER；是否为上游 harness 问题未确认。
- C1-U2：todo-hard（184/185/193/280，TodoWrite 开）5/12；283 关 0/3（主跑 283 开 3/3）。
- 披露：96 夹具 CR 首次 apply 失败照原样（两侧相同）。未覆盖：OpenSearch 2 题冒烟（R8-12）与 B1-19 只读写入拒绝（驱动只支持 ES）。未跑：摇摆题补跑 4–6 遍。
- 凭据扫描：359 个文件、9 个凭据值（DeepSeek、LangSmith、ZAI、ES 工程师 key、elastic 口令、K8s token、两组 Prometheus 口令与账号名），唯一命中是 Prometheus 账号名作为 ClusterRole 名的子串（96 t2 summary，非口令）；正对照命中。
