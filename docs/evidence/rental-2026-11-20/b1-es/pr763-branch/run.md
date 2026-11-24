# PR #763 分支验证：ES 只读 key 用基本认证创建（线 L，2026-11-20）

**在 PR 分支上验证，不是 main**：worktree /home/opspilot/poa-763，HEAD 4f58b636（fix/holmes-es-readonly-basic-auth），用该提交自带的驱动 `round --questions 254 --trials 1 --trace lab --es external --es-url http://127.0.0.1:30920`，**不带 `--es-readonly-env`**，后端为真实 ES 8.19.25（l-es），judge 开启。

- 有管理凭据（私有 env 文件里有 ELASTICSEARCH_USERNAME=elastic 和 ELASTICSEARCH_PASSWORD，值不进证据），round `l-pr763-254-admin`：驱动建出只读 key，写到 round 目录的 es-readonly.env（600），只有 OPSPILOT_ES_URL 和 OPSPILOT_ES_API_KEY 两项。用 elastic 账号查这个 key：name 为 opspilot-investigator，未作废，有过期时间，角色只有 opspilot_readonly。用这个 key 直接写 `POST /l-write-probe/_doc` 得到 403，索引也没有被建出（HEAD 404）；读 `_cat/indices` 得到 200。
  - Run `f164a25f-cb59-5389-9926-ad335c0955eb` completed，9 次模型请求；prompt 173,902（命中 166,016），completion 9,061。报告引用 3 条 ES 证据，judge A 通过（运行时判分）。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/15006d43-9bf9-4a32-a32d-fd54e35e63cb/r/00000000-0000-0000-cb8b-48fbe119583d 。summary：`holmes-evals-l-pr763-254-admin/254_elasticsearch_dr_test_log_check/trial-1/summary.json`。
- 无管理凭据（同一个 env 文件去掉那两项，仍保留工程师 API key），round `l-pr763-254-noadmin`：3 秒内退出码 2，输出 `{"status": "aborted", "code": "ES_ADMIN_CREDENTIALS_MISSING", "detail": "ELASTICSEARCH_USERNAME/ELASTICSEARCH_PASSWORD or --es-readonly-env"}`。fail-closed：没有起 PG、web、worker，没有调用模型，也没有写证据或 summary。原始输出见 `noadmin-abort.log`。
