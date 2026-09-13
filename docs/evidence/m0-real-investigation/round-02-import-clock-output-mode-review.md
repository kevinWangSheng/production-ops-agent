# 导入时钟与审计输出权限独立复核

2026-09-10，接续同一独立审查上下文，原评论3978118746/3978118777由root完整分页核得。未参与修复实现。直接读取当前源码确认：verify_initial_entry仅在raw时钟非None时核 supplied，缺raw时可被manifest非空值补写；CLI使用Path.open("x")，创建权限受umask影响，不保证完整业务输出0600。

验收边界：缺失原operation_started_at/collection_completed_at不得由导入manifest补造；None保持unknown，非空冒充值明确拒绝，合法原值仍可用。完整CLI输出首次创建即0600，不能先宽权限写入再chmod；已存在文件、普通/悬空symlink均不覆盖，stdout继续仅元数据。旧原始证据及报告保持。

待稳定版本执行同字段/路径反例及实际CLI后追加独立结论。没有模型、网络、环境、PG或真实秘密读取。

## 稳定候选独立终验

固定SHA-256：initial_evidence.py `f3309cd462842a8f43114419186661f3056404283d5db3652780e30ab84fb309`；holmes_bridge.py `4b25735cfd701810b571c57a176a949c4c28d8c289ee4c26c58e77be246828ca`。

亲跑实际CLI subprocess权限矩阵：`tests/test_m0_holmes_bridge_v4.py -k 'created_private or exclusive_creation'`，7 passed，0.70秒。严格完整失败packet与显式legacy全文分别在umask022/000下创建文件，结果均0600；已有普通文件、普通symlink、悬空symlink全部拒绝覆盖，原文件/链接/目标保持不变。stdout不含完整输入/原报告/Scenario/Outcome。

源码核查确认唯一输出写口使用`os.open(O_CREAT|O_EXCL|O_WRONLY, 0o600)`，随后在该fd写JSON，finally关闭fd；权限从创建时即限制，并非先宽权限写入再chmod。O_EXCL对已有普通文件及包括悬空在内的symlink不跟随覆盖。未扩大为其他目录或文件清理。

本审查者独立构造18组时钟矩阵：两个原始字段operation_started_at、collection_completed_at，各自raw missing/null/recorded × manifest missing/null/recorded。所有原始missing/null与manifest非空组合明确INITIAL_COLLECTION_TIME_MISMATCH；双方缺失保持None，不生成新时间；原始known与manifest同值通过，manifest缺失/null不能擦除known原值。测试重新生成一致的合成raw/view/manifest hash，拒绝来自时钟规则而不是hash损坏。

同时核查source时间同族规则：unknown source basis不能携带非空边界；有event_time必须与实际可见事件字段一致，不能使用observed_at或导入时刻补source/capture。缺整个timing、timing:null及缺原时钟的保真unknown路径均由受影响回归覆盖。

亲跑组合：`.venv/bin/python -m pytest tests/test_m0_initial_evidence.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_outcomes_v4.py -q`，122 passed，4.01秒，包含上述权限CLI、时钟、既有初始来源/严格报告/无报告交接。

**两项稳定修复在导入时钟真实性和完整业务输出创建权限的上述离线范围通过独立审查，本组无未处理发现。** 缺时钟仍unknown，不升级为当前可用事实；文件排他私有创建不改变完整业务内容的审计保真。

本次未运行模型、网络、环境、PG或真实秘密读取，旧raw/view/报告/快照保持；原模型报告质量和M1门槛不变。最新提交CI及已触发远程审查仍是最终PR交付条件。
