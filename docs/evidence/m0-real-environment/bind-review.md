# PR #15 全部挂载输入保全修复

Code Review 对`1efa66c`的P2评论`3971972974`指出：原入口只核对4个生成文件hash，bind-mounted上游flagd/Grafana/Collector extras/product数据改变后仍可能显示configuration verified。

修复仅扩展同一入口的写前校验：以已有source-downloads中的OTel tar SHA256固定唯一原来源；完整比对9条bind中的文件内容/类型与目录成员（含空目录），只允许固定源码包或已记录生成文件。未知源/新成员在读取内容前拒绝，所有软链接及祖先软链接拒绝。仍先完成旧image lock和全部候选配置检查，再写runtime；所有docs archive保持只读。增加`--check-only`以明确执行无写入候选校验，不建设新工件平台。

原21项回归中，新增12项负例在旧实现失败，原9项保持通过：[红测](bind-review-red.txt)。修复后补齐祖先/嵌套链接、删除空目录、源树内外未知FIFO和check-only，总共[26项通过](bind-review-green.txt)。测试用隔离临时树、合成固定tar及fake Docker inspect执行真实CLI，拒绝前后docs/tmp字节快照不变；没有网络、模型或daemon启动。

[实际当前输入复核](bind-review-current-inputs.json)使用保留的真实OTel tar、原9条bind源及4份生成文件，`--check-only`通过；3份历史manifest和4份真实runtime的前后SHA不变。Docker inspect使用原记录替身，不声称重新检查已停止daemon的镜像。该检查发生在本次审查修复时，不能追溯认证历史实验运行期间每一时刻的挂载状态。

独立flash_review实际运行26项临时树CLI回归（7.83s），并复跑当前真实tar/9bind/4hash只读入口，通过；未知path/name早拒绝与无真实写入均已核对。其结论由协调者写入主closeout-review。原始测试输出及hash见[日志manifest](bind-review-log-manifest.json)，旧实验工件未覆盖。Ruff/format/py_compile通过；最终PR仍等待覆盖新提交的Code/Security Review。
