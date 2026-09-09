# 本轮命令与恢复位置

本实验运行目录固定为 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment`。主分支整合提交不会迁移其`tmp/m0-environment`、容器或卷；继续/停止现有实验应使用该工作区，先核查实际profile与project，避免重复环境。

源码下载地址及tar SHA256见[sources](source-downloads.json)；固定源码内保留Apache-2.0 LICENSE，已有仓库副本见`../m0-c/opentelemetry-demo-LICENSE`、`../m0-c/holmesgpt-LICENSE`。源码目录分别是tmp下`opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad`和`holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4`。

启动前实际盘点：`docker context ls`、`colima list`、`df -h .`、`memory_pressure`、`sysctl hw.memsize`。确认default停止，专属profile不存在，才执行：

```sh
colima start m0-otel --cpu 4 --memory 6 --disk 24 --activate=false --ssh-agent=false --ssh-config=false --mount /Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment:w
```

从固定公开源码模板解析的已执行命令（该.env是上游公开无秘密模板）：

```sh
DEMO_VERSION=2.0.2 docker compose -f tmp/m0-environment/opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad/docker-compose.yml --env-file tmp/m0-environment/opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad/.env config --format json > tmp/m0-environment/resolved-upstream.json
python3 scripts/m0_environment/prepare.py
docker --context colima-m0-otel compose -p opspilot-m0 -f tmp/m0-environment/compose.json pull
docker --context colima-m0-otel pull python:3.12.12-slim
python3 scripts/m0_environment/freeze_images.py
docker --context colima-m0-otel compose -p opspilot-m0 -f tmp/m0-environment/compose-pinned.json up -d --no-build --pull never
python3 scripts/m0_environment/capture.py normal-ready
```

`freeze_images.py`核实每个本地image ID/RepoDigest/arm64后写digest Compose。历史tag可能以后漂移；复验优先使用已归档`image-lock.json`的digest和本次保留的`compose-pinned.json`，不能以未来重新拉tag代替本次相同版本。

实际端口全部loopback：18080前端、19090 Prometheus、16686 Jaeger query、19200 OpenSearch；18081只读proxy。调查工具只使用18081，直接后端端口是工程盘点入口。代理内部DNS名`read-proxy:18081`，调查容器只能加入`opspilot-m0-investigation` internal网络，不能加入`opspilot-m0-telemetry`、挂socket或主机目录。模型服务出口若需要应另走可信授权边界；本轮宿主Holmes不能声称这个容器探针证明它自身OS隔离。

修改挂载的read_proxy.py后仅compose up不一定重启Python进程，实际执行`freeze_images.py`同步受限单文件副本，再显式restart read-proxy；须确保没有在途调查，不重置当前模型/工具总预算。

结束命令（待全部调查/评估证据采集后执行）：

```sh
docker --context colima-m0-otel compose -p opspilot-m0 -f tmp/m0-environment/compose-pinned.json stop
colima stop m0-otel
```

不使用down、rm、prune或删除profile。Prometheus/OpenSearch保留独立volume；Jaeger内存trace需在停止前导出需留的证据，停止本身不能保证其内存历史可恢复。模型私有协议记录及凭据不进入本公开工件目录。
