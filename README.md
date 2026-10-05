# 高空探测器标定包发布系统

操作员在控制台输入**发布标识**与 **Base64 工件（≤ 64KiB）**，控制服务将同一候选字节
切换至**两座离线镜像仓**（repo-a / repo-b），并可按标识查看进度、当前摘要及准备 /
激活证据。仓库内自带名为 **verify** 的一次性验收服务，以退出码报告验收结果。

## 架构

```
            ┌────────────┐   prepare/activate（仓端操作键幂等）  ┌────────┐
操作员 ───▶ │  control   │ ──────────────────────────────────▶ │ repo-a │
（浏览器）  │ 控制台+API │ ──────────────────────────────────▶ │ repo-b │
            └────────────┘                                     └────────┘
                  │ SQLite（WAL, synchronous=FULL）持久化：发布意图 + 仓端回执
            ┌────────────┐
            │   verify   │ 一次性验收：断连/重启场景 → 代码测试 → 构建检查 → HTTP 冒烟
            └────────────┘
```

全部组件仅使用 Python 标准库（零第三方依赖），镜像构建不需要任何网络下载。

## 快速开始

```bash
# 启动全部服务（含一次性验收）
docker compose up --build

# 仅以验收退出码作为结果（CI 用法）
docker compose up --build --exit-code-from verify verify

# 自定义宿主机端口（默认 8080）
CONTROL_PORT=9090 docker compose up --build -d control repo-a repo-b
```

控制台：`http://localhost:8080/`（健康响应：`GET /healthz` → `{"status":"ok"}`）。

## 关键语义

- **先持久化，后执行**：`POST /api/releases` 先把 `sha256` 与不可变发布意图
  （标识 → 摘要 + 字节）写入 SQLite，再由后台协调器驱动仓端操作。
- **仓端操作键**：`rel:{发布标识}:{仓}:{prepare|activate}`，由发布标识派生。
  仓端按键幂等：同键同摘要回放**首次回执**（receipt_id 不变、激活计数不增）；
  同键异摘要返回 `409 op_key_conflict` 明确拒绝。
- **证据与发布标识一一对应**：每份回执只证明它携带的仓端操作键
  `rel:{发布标识}:{仓}:{prepare|activate}`。即使两次发布的工件字节完全相同
  （摘要相同），不同发布标识也必须各自向两座仓取得与其标识派生键匹配的四份
  准备 / 激活回执，且两仓都真正激活该摘要后才能 `COMPLETED`；任何其他发布的
  回执都不能复用为本发布的证据。
- **错误完成可重新收敛**：若本地曾把别的发布的回执当作自身证据而误报完成，
  重新查询或控制服务重启时会逐份校验证据的派生操作键、签名与摘要：不属于本
  发布键的回执被丢弃，误报的 `COMPLETED` 回退并依据仓端真实结果重新收敛；
  `REJECTED` 仍然永久锁定。
- **完成条件**：仅当两仓的激活回执摘要都等于发布 SHA-256 时才置 `COMPLETED`，
  此时 `current_digest` 才对外可见。
- **断连收敛**：若一仓在持久化激活后断开响应，控制服务重启后会先向仓端
  `GET /v1/ops/{op_key}` 认领既有回执，依据仓端回执收敛为完成，绝不二次激活。
- **拒绝锁定**：任一仓返回不属于该发布的摘要（或证据签名不符、操作键冲突）时，
  发布锁定为 `REJECTED`，`current_digest` 保持为空，仓端活动指针不被改写，
  且状态不再漂移。
- **幂等提交**：相同标识 + 相同工件 → `200` 回放当前状态，不产生第二次激活；
  相同标识 + 不同工件 → `409 release_id_in_use`，既有成功发布的真实状态保留；
  非法 Base64 → `400 invalid_base64`；超限工件 → `413 artifact_too_large`
  （恰为 64KiB 可正常发布）。

## API 摘要（控制服务）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 控制台页面（发布表单 + 反馈区 + 按标识查询证据） |
| GET | `/healthz` | 健康响应（含 `boot_id`） |
| POST | `/api/releases` | 提交 `{release_id, artifact_b64}` → `202/200/409/400/413` |
| GET | `/api/releases/{id}` | 进度、当前摘要、双仓准备/激活证据（签名回执） |
| GET | `/api/releases` | 全部发布列表 |

仓端（仅内部网络）：`POST /v1/prepare`、`POST /v1/activate`、
`GET /v1/ops/{op_key}`、`GET /v1/state`、`GET /healthz`。

## verify 验收服务

`verify` 是执行后退出的一次性服务，按顺序执行：

1. **断连/重启场景**：武装 repo-b「激活提交后断开响应」→ 提交发布 →
   确认卡在未完成态 → 重启控制服务（新 `boot_id`）→ 恢复 repo-b →
   校验双仓最终摘要、激活次数恰为 1、准备/激活证据完整且为首次回执回放。
2. **同摘要 / 不同发布标识场景**：全新数据卷中先用一个新标识提交工件并等待
   双仓完成，再用另一个未使用过的标识提交解码后完全相同的字节，随后重启控制
   服务；逐份核对四份证据的发布标识派生操作键、仓端实际处理记录、两仓活动摘要
   与激活计数（每标识每仓恰好各激活一次），并确认同标识重放不产生新激活。
3. **代码测试**：`python -m unittest discover`（单元 + 进程内集成测试，
   集成测试覆盖断连/重启、同摘要不同标识独立证据、误报完成的重新收敛）。
4. **构建检查**：`python -m compileall` 字节编译全部源码。
5. **HTTP 冒烟**：健康页与发布接口（重复提交、标识复用、非法 Base64、
   超限与 64KiB 边界、拒绝锁定、仓端幂等直测、未知标识 404）。

退出码 `0` = 验收通过，非 `0` = 存在失败项（日志中逐条标注 `[FAIL]`）。

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CONTROL_PORT` | `8080` | 宿主机暴露控制台的端口 |
| `REPO_A_SECRET` / `REPO_B_SECRET` | `dev-secret-*` | 仓端证据 HMAC 签名密钥 |
| `FAULT_HOOKS` | `1`（compose 验收环境） | 故障注入开关，生产应置 `0` |
| `WORKER_INTERVAL_S` | `0.5` | 控制服务协调器轮询间隔 |

故障注入接口（仅 `FAULT_HOOKS=1` 时挂载）：仓端 `/fault/disconnect`、
`/fault/recover`、`/fault/disconnect-after-activate`、
`/fault/corrupt-next-activate`；控制端 `/fault/restart`（进程退出，
由 `restart: on-failure` 拉回，用于验收重启收敛）。

## 本地开发（无 Docker）

```bash
python3 -m unittest discover -s tests -t .        # 全部测试
python3 -m compileall -q app tests                # 构建检查
REPO_NAME=repo-a PORT=8001 DATA_DIR=/tmp/ra FAULT_HOOKS=1 python3 -m app.repo.server &
REPO_NAME=repo-b PORT=8002 DATA_DIR=/tmp/rb FAULT_HOOKS=1 python3 -m app.repo.server &
PORT=8080 DATA_DIR=/tmp/ctl FAULT_HOOKS=1 python3 -m app.control.server
```
