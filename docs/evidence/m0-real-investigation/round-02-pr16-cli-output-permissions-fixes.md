# PR16 CLI业务输出原子私有权限修复

对应同一`f923897` review的P2 comment `3978118777`。只改bridge公共CLI输出写口及对应测试；无runtime/source clock修改，无模型/网络/PG/环境操作，未提交。与同轮其他修复分文件协作，Git由root统一处理。

原`Path.open('x')`虽独占创建，仍按umask产生0644或0666文件，完整业务packet可被其他本地用户读取。实际CLI subprocess在strict完整失败packet与legacy原报告两模式下，分别使用umask022/000复现：`4 failed, 3 passed, 61 deselected in 0.92s`；4个权限反例确实得到0644/0666，已有普通文件、symlink、悬空symlink的独占拒绝原本通过。

公共唯一输出口改为`os.open(path, O_CREAT|O_EXCL|O_WRONLY, 0o600)`，从创建瞬间限制权限，不依赖写后chmod，不预检路径，不覆盖任何已有文件或链接。通过`os.fdopen(..., closefd=False)`管理文本缓冲，外层finally关闭原fd；fdopen/写入/flush异常同样关闭描述符。该共同写口覆盖strict、legacy及结构错误摘要输出，stdout业务字段过滤不变。

```sh
.venv/bin/python -m pytest tests/test_m0_holmes_bridge_v4.py -k 'created_private or exclusive_creation' -q
.venv/bin/python -m pytest tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py -q --tb=short
.venv/bin/ruff check scripts/m0/holmes_bridge.py tests/test_m0_holmes_bridge_v4.py
.venv/bin/ruff format --check scripts/m0/holmes_bridge.py tests/test_m0_holmes_bridge_v4.py
```

修后7个实际CLI测试通过（0.73s）；82项桥接合同通过（3.71s）；ruff/format通过。权限测试验证文件0600、full packet/原报告仍在私有输出中、stdout不含完整业务；覆盖冲突测试验证原文件/链接/链接目标保持不变，悬空链接不创建目标。测试仅使用临时合成业务文件，不修改历史导出权限或原真实工件。

冻结SHA256已交fresh独立复验，作者不自称独立通过：

- scripts/m0/holmes_bridge.py：`4b25735cfd701810b571c57a176a949c4c28d8c289ee4c26c58e77be246828ca`
- tests/test_m0_holmes_bridge_v4.py：`8836c4462500187a5fba2f8d819162831d8526c16accaa5d33b9875e965702a9`

root负责整体fullcheck/快照/审查与PR收尾；本修复没有新增真实模型证据或改变历史qualityFAIL。
