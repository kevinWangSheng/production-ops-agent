# 本轮命令与恢复位置

本实验运行目录固定为 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment`。主分支整合提交不会迁移其`tmp/m0-environment`、容器或卷；继续/停止现有实验应使用该工作区，先核查实际profile与project，避免重复环境。

源码下载地址及tar SHA256见[sources](source-downloads.json)；固定源码内保留Apache-2.0 LICENSE，已有仓库副本见`../m0-c/opentelemetry-demo-LICENSE`、`../m0-c/holmesgpt-LICENSE`。源码目录分别是tmp下`opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad`和`holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4`。

启动前实际盘点：`docker context ls`、`colima list`、`df -h .`、`memory_pressure`、`sysctl hw.memsize`。确认default停止，专属profile不存在，才执行：

```sh
colima start m0-otel --cpu 4 --memory 6 --disk 24 --activate=false --ssh-agent=false --ssh-config=false --mount /Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment:w
```

以下保留初次部署时已执行的历史命令（该.env是上游公开无秘密模板），不是可以无条件重跑的脚本；现有profile、历史label及已归档工件必须保留：

```sh
DEMO_VERSION=2.0.2 docker compose -f tmp/m0-environment/opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad/docker-compose.yml --env-file tmp/m0-environment/opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad/.env config --format json > tmp/m0-environment/resolved-upstream.json
python3 scripts/m0_environment/prepare.py
docker --context colima-m0-otel compose -p opspilot-m0 -f tmp/m0-environment/compose.json pull
docker --context colima-m0-otel pull python:3.12.12-slim
python3 scripts/m0_environment/freeze_images.py
docker --context colima-m0-otel compose -p opspilot-m0 -f tmp/m0-environment/compose-pinned.json up -d --no-build --pull never
python3 scripts/m0_environment/capture.py normal-ready
```

PR #15 审查后，`freeze_images.py`仅用于**核验本次已归档实验并生成相同运行文件**，不再创建或改写`docs/evidence/m0-real-environment/image-lock.json`或`configuration-hashes.json`。这两个历史文件必须存在；脚本先在内存收集全部镜像身份/架构，逐项对照旧lock，再核对候选Compose、proxy及Collector/Prometheus内容hash。任何漂移、配置差异或缺档都在写runtime proxy/Compose之前拒绝，原始证据字节保持不变。全部匹配才写`tmp/m0-environment/compose-pinned.json`和`read_proxy.py`。

未来tag可能漂移；不能直接沿用历史pull命令后自动重新“冻结”。应按已有lock的digest准备本次版本并核对相同标签引用，或使用已保留的digest Compose；如果标签指向不同image、架构或配置有变化，本入口会拒绝。不同路径/配置或新实验应建立独立、受审核的实验记录，不能靠重写本次历史hash让校验通过；本次修复没有增加新实验工件平台。

实际端口全部loopback：18080前端、19090 Prometheus、16686 Jaeger query、19200 OpenSearch；18081只读proxy。调查工具只使用18081，直接后端端口是工程盘点入口。代理内部DNS名`read-proxy:18081`，调查容器只能加入`opspilot-m0-investigation` internal网络，不能加入`opspilot-m0-telemetry`、挂socket或主机目录。模型服务出口若需要应另走可信授权边界；本轮宿主Holmes不能声称这个容器探针证明它自身OS隔离。

历史实验中曾修改挂载的read_proxy.py并同步/显式restart代理；仅compose up不会保证重启进程。现有已归档实验的核验入口会拒绝改动后的proxy，不再用上述历史流程覆盖旧证据；后续修改应另立实验记录，并遵守无在途调查及预算不重置的限制。

本轮在调查/恢复证据及Jaeger导出完成后，已实际执行以下结束命令：

```sh
docker --context colima-m0-otel compose -p opspilot-m0 -f tmp/m0-environment/compose-pinned.json stop
colima stop m0-otel
```

不使用down、rm、prune或删除profile。Prometheus/OpenSearch保留独立volume；Jaeger内存trace需在停止前导出需留的证据，停止本身不能保证其内存历史可恢复。模型私有协议记录及凭据不进入本公开工件目录。
