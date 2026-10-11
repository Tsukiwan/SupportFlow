# SupportFlow 面试演示脚本(3–5 分钟)

面向面试场景的可复现演示流程。所有输出摘录自本仓库 demo 的实际运行结果(`REF-`/`RUN-`/`PLAN-` 后为随机 id,每次不同)。

## 演示前准备(提前 5 分钟做完)

前置:Python ≥ 3.12 + uv;本地 Docker(仅恢复 demo 需要);`.env` 已从 `.env.example` 复制。

```bash
uv sync                                        # 1. 依赖
docker compose up -d --wait                    # 2. 本地 PG(仅回环 127.0.0.1:5432)
uv run alembic upgrade head                    # 3. 开发库迁移
uv run alembic current                         # 4. 确认 → 0003 (head)
docker compose exec postgres dropdb --if-exists -U supportflow supportflow_demo
docker compose exec postgres createdb -U supportflow supportflow_demo
SUPPORTFLOW_DATABASE_URL=postgresql+psycopg://supportflow:<密码>@127.0.0.1:5432/supportflow_demo \
  uv run alembic upgrade head                  # 5. 隔离的演示库(drop 重建,绝不碰开发库)
```

**数据隔离**:恢复 demo 只使用 `supportflow_demo` 库,通过 drop/createdb 重置,不清理、不写开发库 `supportflow`。密码从 `.env` 读取,不要粘贴进任何文档或聊天窗口。

## 演示流程

### 第 1 步:项目与架构(20–30 秒)

口头介绍:

> 这是一个客服退款 Agent。LLM 只负责读懂工单和提方案,所有涉及钱的决定——能不能退、退多少、要不要人工审批、执行几次——全部由确定性服务层在数据库约束下完成。核心工程问题是:**外部网关调用和本地数据库事务不构成原子事务**——网关超时那一刻,钱可能已经退成功了。整个项目围绕这个窗口设计:预留、幂等、UNKNOWN 状态和恢复。

- **可展示**:本仓库 README 的架构图与状态机图。
- **面试官可能问**:LLM 在这里的权限边界是什么?→ 答:只产出计划提议,不选工具、不接触网关、不能改状态;审批决定走独立的人工入口。

### 第 2 步:正常退款 demo(40–60 秒)

```bash
uv run python -m app.agent.demo
```

预期输出摘要(内存模式,4 场景):

```
refundable (low-risk refund)         phase=completed          run=completed          plan=PLAN-… iterations=1
    reply: Refund of 199.99 USD for order ORD-1001 has been processed. …
delivered_not_received (first run)   phase=pending_approval   run=awaiting_approval  …
delivered_not_received (after APPROVED) phase=completed          run=completed          …
    reply: Refund of 129.90 USD for order ORD-1005 has been processed. …
amount_mismatch (claim > total)      phase=completed          run=completed          plan=-   …
non_existent (order missing)         phase=completed          run=completed          plan=-   …
```

- **关键字段**:低风险工单 `phase=completed`;高风险工单第一跑 `phase=pending_approval`(没执行)、人工批准后重跑才 `completed`;超额与订单缺失 `plan=-` 且直接拒绝。
- **解说点**:同一个高风险工单在批准前零账务变化;批准后重跑复用同一个 `PLAN-…`(计划哈希绑定,不是新提议)。
- **面试官可能问**:审批后为什么还要重新跑整个图?→ 答:恢复即重跑,图在审批节点复用存储的已批准计划并重验哈希/有效期,执行前不重新咨询 LLM。

### 第 3 步:UNKNOWN 的含义(30–45 秒)

口头介绍(不运行命令):

> 网关调用有三种结果:确认执行、确认失败、以及——超时、连接中断、取消——**不确定**。异常说明不了钱到底动没动,因为连接可能在钱划走之后断开。所以异常一律记 UNKNOWN:预留保留、同键重试时只查原操作。把 UNKNOWN 当失败释放预留,就可能「钱退了、预留又放给别人花」;当成功记,又可能实际上没退。两个方向都错,所以 fail-closed。

- **可展示**:README 中「退款状态机与 UNKNOWN 恢复」一节的状态图。
- **面试官可能问**:UNKNOWN 的行谁来收拾?→ 答:恢复服务(第 4 步演示),它只查询、结构上没有执行路径。

### 第 4 步:两阶段恢复 demo(60–90 秒)

阶段 1 —— 创建退款,网关记账成功,进程在本地结算前「崩溃」:

```bash
SUPPORTFLOW_STORE_BACKEND=postgres \
SUPPORTFLOW_DATABASE_URL=postgresql+psycopg://supportflow:<密码>@127.0.0.1:5432/supportflow_demo \
  uv run python -m app.recovery_demo --phase crash
```

预期输出:

```
phase crash: refund REF-… created (PENDING, reserved 50.00)
  gateway executed successfully; application crashed before finalize
  gateway ledger written to demo_gateway_ledger.json
```

- **关键字段**:`PENDING, reserved 50.00` —— 数据库里留着预留,但没有任何进程会来 finalize;JSON 账本模拟网关侧状态跨崩溃存活。

阶段 2 —— 新进程恢复,只查原操作并结算;第二遍证明不重复:

```bash
SUPPORTFLOW_STORE_BACKEND=postgres \
SUPPORTFLOW_DATABASE_URL=postgresql+psycopg://supportflow:<密码>@127.0.0.1:5432/supportflow_demo \
  uv run python -m app.recovery_demo --phase recover
```

预期输出:

```
recovery run 1: discovered=1 settled_processed=1 settled_failed=0 kept_unknown=0
  REF-…: settled_processed (gateway succeeded)
  after recovery: status=processed refunded=50.00 reserved=0.00
recovery run 2: discovered=0 settled_processed=0 kept_unknown=0
  after second run: status=processed refunded=50.00 reserved=0.00
```

- **关键字段**:第一遍 `settled_processed=1`、`reserved=0.00`;第二遍 `discovered=0` —— 终态不再被发现,金额没动第二次。
- **解说点**:恢复 worker 先取租约(一条原子条件 UPDATE),再在事务外查网关,最后在带 owner token 校验的事务里结算;整个恢复模块没有 `execute` 调用,`execute_calls == 0` 有测试断言。
- **面试官可能问**:如果恢复时网关查询还是 UNKNOWN 呢?→ 答:状态和预留保持不动,租约过期后允许再查一次——过期只代表可以重新查询,绝不代表可以重新执行。

### 第 5 步:幂等、租约与事务边界(30–45 秒)

口头总结:

> 五层防线:①全意图幂等键 + 数据库唯一约束,重放解析到原记录;②同订单同时间至多一笔未决退款(部分唯一索引);③余额预留是条件 UPDATE 加库级 CHECK,不是先读后写;④结算在行锁下重查状态,双进程并发恰好结算一次(集成测试里用 spawn + barrier 实测);⑤审计和账务同事务,审计写失败整个退款回滚。

- **面试官可能问**:条件 UPDATE 和先读后写差在哪?→ 答:先读后写在两个事务交错时会互相覆盖;条件 UPDATE 让数据库在行锁上原子判定,`rowcount==0` 即失败。

### 第 6 步:诚实的边界(20 秒)

> 网关是 FakeGateway——一个带账本的确定性替身,不是真实支付网关。真实网关的查询契约、幂等窗口、最终一致性都没有验证。此外恢复需要外部触发(如 cron),长期 UNKNOWN 没有告警闭环,API 只有 /health。这是作品集,不是可以上线的支付系统。

## 失败预案(最小排障,不关闭任何安全检查)

| 症状 | 排查 | 解决 |
| --- | --- | --- |
| `connection refused / could not connect to server` | `docker compose ps` 看 PG 是否健康 | `docker compose up -d --wait`;仍失败查 `docker compose logs postgres` |
| `alembic current` 不是 `0003 (head)` | 迁移没跑或指向了错误库 | 对目标库执行 `uv run alembic upgrade head`;确认 `SUPPORTFLOW_DATABASE_URL` 指向的库 |
| 集成测试 `no tests ran` / 全部 skip | `SUPPORTFLOW_TEST_DATABASE_URL` 未设置 | 按 README 设置指向 `supportflow_test`(库名必须以 `_test` 结尾,否则测试直接失败而非误连) |
| 恢复 demo 报 `create returned kind=…` | demo 库有上次残留数据 | 按「演示前准备」drop 重建 `supportflow_demo`(只允许对演示库操作,勿对开发库 drop) |
| `address already in use`(API 演示) | 端口被占用 | `--port 8123` 换端口;不要 kill 未知进程 |
| `SUPPORTFLOW_STORE_BACKEND=postgres` 但 URL 是 sqlite | 环境变量没传给 demo 进程 | 每条命令都要带两个 `SUPPORTFLOW_` 前缀变量,postgres 模式对错误 URL fail-loud,不会静默降级 |
