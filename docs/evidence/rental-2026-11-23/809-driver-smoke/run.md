# #809 评测驱动（PR #824）：驱动冒烟

代码是 main `e199727e`。命令：`.venv/bin/python -m scripts.holmes_evals.driver round --round cp1-809-183a --upstream tmp/holmes-compare/upstream --questions 183a_elasticsearch_cluster_health --trials 1 --es external --es-url http://127.0.0.1:30920 --es-readonly-env <私有文件> --trace lab --env-file <rental.env> --evidence-root docs/evidence/rental-2026-11-23/809-driver-smoke`。工程侧 ES key 通过环境变量传入，不写入任何文件。

- Run `4b3b1031-3463-5e51-b660-aef862d0bf3e`：completed，2 次模型请求；prompt 34,750（命中 32,768），completion 1,640。驱动归类 ok，judge A（glm，1 次）。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/52395b85-808c-4bac-9515-d26cff59b9a4/r/00000000-0000-0000-387d-605c6e0eb011。
- 提交时带了最大回看窗口：Run 的授权窗口为 2026-10-24T15:20:34Z 到 2026-11-23T15:20:34Z，即 30 天，tool_schema_revision 是 30 天上限下的 `otel-demo-ext-0fbad8ac5bbb`。驱动记录的 git 为 `e199727e`，dirty false。
- 披露码：`TOOL_FACE_FIXED`、`NO_INTERMEDIATE_OUTPUT`、`JUDGE_DIFFERS_FROM_UPSTREAM`；`time_box_limited` 为 false。183a 不在 {184, 235} 内，符合预期。
- summary：`holmes-evals-cp1-809-183a/183a_elasticsearch_cluster_health/trial-1/summary.json`。
- 判定：通过。
