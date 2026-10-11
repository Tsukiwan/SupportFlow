# SupportFlow

AI 客服工单处理 Agent:查询订单、检查退款政策、提出处理方案,高风险操作前等待人工审批。所有退款均为模拟操作。

## 项目边界(重要)

- ✅ 构建:工单领域模型、订单查询、退款政策检查、处理方案生成、人工审批门禁、审计日志、LangGraph Agent 主流程
- ❌ 不构建:RAG、向量数据库、知识库问答系统
- ⚠️ 退款为模拟操作,不连接真实支付网关
- 模型供应商:DeepSeek(OpenAI 兼容 API),Key 与模型名通过环境变量配置;未配置 Key 时使用确定性离线实现

## Day 1 状态

已就位:领域模型与枚举、金额规则(Decimal,拒绝 float)、统一 ToolResult、配置(环境变量 + SecretStr)、结构化 JSON 日志(密钥脱敏)、确定性模拟数据(6 个场景)。

## Day 2 状态

已就位:业务工具层与退款安全边界。5 个工具(`get_order` / `get_shipping_status` / `get_refund_policy` / `create_refund` / `add_ticket_note`)全部返回统一 ToolResult;退款工具在服务端独立完成全部安全校验(工单-订单绑定、订单存在性与状态、金额精度、实时可退余额、结构化政策、审批绑定订单/金额/计划版本/风险等级/有效期、全意图幂等键、并发互斥、单笔未决退款门禁),不信任 LLM 或调用方提供的任何状态声明——issue_type 与政策一律以工单记录为准,谎报直接拒绝;所有退款走模拟支付网关,支持业务拒绝/确认未执行的暂时性失败/结果未知三类非成功结果,网关异常与任务取消一律按未知结果处理(保留余额预留),未知结果必须先查询原操作再处理,绝不盲目二次执行;同一订单存在 PENDING/UNKNOWN 退款时,异键新退款被拒绝,直到原操作被确认处理、确认拒绝或确认未执行并安全释放;订单余额区分「已确认退款」与「预留」;每次工具调用写入审计事件(工具名、结果类别、耗时、run_id、关联 ID,无密钥无 PII),取消的调用单独记为 cancelled。

## Day 3 状态

已就位:LangGraph Agent 主流程([app/agent/graph.py](app/agent/graph.py)),固定图结构、模型不选工具:

**图结构与路由** — `init_run → classify → gather_context → select_plan → validate_plan`,然后条件路由:`continue`(执行)/ `await_approval`(请求审批)/ `business_rejection`(拒绝,直接回复)/ `fatal`(终止,运行记 FAILED);执行侧 `execute → verify_result`,仅对确认未执行的暂时性失败按 `retry` 受限重试(硬迭代上限,`recursion_limit` 兜底),`unknown` 进入待核实(`needs_verification`,绝不盲目重试),最后 `reply → finalize`。工单只在业务完成条件经核验后进入 RESOLVED;等待审批与失败运行永不解决工单。

**可信计划与审批绑定(核心安全)** — LLM 只产出计划「提议」;服务端生成 `plan_id` 并注册进 [PlanStore](app/services/plans.py)(内容 SHA-256,同 id 异内容注册报错,读取时重算哈希防篡改);[Approval](app/domain/models.py) 记录 `plan_content_hash` 并绑定工单/订单/金额/风险等级/动作;执行前从 PlanStore `load_verified` 重新加载计划并复核哈希与字段——不接受 LLM 单方面声明的计划内容,修改计划金额或重用 plan_id 都无法让旧审批继续授权;**哈希缺失的审批一律按 mismatch fail-closed**(仅 Day 2 测试夹具的无 PlanStore 构造保留兼容路径,生产入口 `build_default_tool_set` 强制注入 PlanStore);同一校验在 graph 门禁与 RefundService 双重执行,审批风险等级必须达到「有效风险」(工单风险与政策要求取高,HIGH 及以上一律要求审批)——该规则由 [approval_requirement](app/services/refund_policy.py) 单一函数供两层共享,绕过 graph 直接调用服务同样被拦截。

**人工审批暂停与恢复** — 高风险计划暂停:`ticket → PENDING_APPROVAL`,`run → AWAITING_APPROVAL`,绝不 resolved;决策只经 [ApprovalService.decide](app/services/approvals.py)(人工侧入口,记录 actor、decided_at、有效期、计划哈希),Agent 图没有 approve/decide 工具;恢复时重新运行工单,图复用审批绑定的存储计划(不再咨询 LLM 新提议),并重验审批状态、有效期与计划哈希;拒绝/过期/不匹配均不执行退款。

**执行安全与结果核验** — 图只调用审计过的 ToolSet,禁止直接改余额/退款状态/网关状态(有静态测试守卫);PROCESSED 经持久化记录核验后才算成功;UNKNOWN 进入待核实且同订单异键新退款被门禁拒绝;REJECTED 不标成功、预留释放;退款成功后回复生成失败只换模板、绝不重发退款;**工单级索赔封顶**:同一工单的全部退款(已结算/在途/未决)累计不得超过其索赔额,检查与余额预留在同一原子临界区内完成——并发双跑、新 plan_id、新幂等键、或退款已成功而工单尚未 RESOLVED 的重跑,都无法产生第二笔超索赔退款;同一工单在索赔额内的多次部分退款仍然合法;客服回复只用可信数据与核验结果,与结果冲突的模型文本被机械校验替换(required/forbidden token)。

**审计与 run_id** — 全部事件贯穿 `ticket_id` 与 `run_id`,区分:运行开始、分类(提议与可信记录分歧)、上下文查询、计划提议(含回退重试数)、计划校验(通过/业务拒绝)、审批门禁与请求、审批决策(`actor=human`)、工具调用(含 `create_refund.approval_verified`)、执行、结果核验、回复(成功/模板回退)、运行终止;「生成退款计划」与「退款已执行」是不同事件。

**LLM 协作与提示注入** — 分类/计划/回复均为有界重试(2 次)后回退确定性默认实现的提议;工单文本与用户消息视为不可信输入,订单归属/金额/政策/审批/执行权限全部由服务端重验;LLM 无法通过自然语言改变服务端规则,也无法接触工具。

**离线确定性演示** — [app/agent/demo.py](app/agent/demo.py) 覆盖 4 个场景(低风险自动退款、高风险暂停→人工批准→恢复执行、索赔超额拒绝、订单不存在拒绝),强制离线协作者,输出可复现:

```bash
uv run python -m app.agent.demo
```

PG 后端演示(需本地 PG 已启动,幂等 seed;第二遍运行演示持久化——已退款订单被余额门禁拒绝,账务不变):

```bash
SUPPORTFLOW_STORE_BACKEND=postgres \
SUPPORTFLOW_DATABASE_URL=postgresql+psycopg://supportflow:<密码>@127.0.0.1:5432/supportflow \
  uv run python -m app.agent.demo
```

## Day 4 状态

已就位:PostgreSQL 持久化层与实机验收。

**数据模型与迁移** — 8 表 ORM 模型([app/persistence/models.py](app/persistence/models.py)):orders / tickets / refunds / approvals / plans / agent_runs / audit_events / ticket_notes;Alembic 迁移 [migrations/versions/0001_initial.py](migrations/versions/0001_initial.py) 含 CHECK 约束、`idempotency_key` 唯一约束、同订单至多一笔 PENDING|UNKNOWN 的部分唯一索引 `uq_refunds_order_unresolved`;`upgrade head` 幂等,连跑两次为 no-op。

**PgRefundStore([app/persistence/pg_refund_store.py](app/persistence/pg_refund_store.py))** — 实现 RefundStoreProtocol,与内存版语义逐条对齐:同 key 意图比较先于订单级扫描、余额预检先于 claim、FAILED 释放预留 / UNKNOWN 保留(fail-closed)、同订单至多一笔 PENDING|UNKNOWN、claim 封顶为跨订单聚合不变量(读自锁定工单行,不信任调用方参数)。并发机制:行锁顺序 orders→tickets→refunds、条件 UPDATE 做余额预留、唯一冲突按约束名精确映射(仅两个已知约束名,其余原样抛出)、审计与业务变更同事务。

**实机验收** — 22 个集成测试([tests/integration/](tests/integration/))在真实 PostgreSQL 16 上全部通过(连续 4 轮,含 6 个 multiprocessing 双进程竞态测试),Alembic 迁移幂等验证通过,全量单测无回归。实机验收发现并修复 4 个缺陷,其中 2 个并发安全缺陷:①锁后重读返回 identity map 陈旧对象,双进程 finalize 双双结算(修复:FOR UPDATE 重读加 `populate_existing`);②create 拿订单锁后缺幂等键重查,异意图输家被订单门禁抢答为 concurrent(修复:锁后重查,与内存版意图语义对齐)。

**开发数据库** — [compose.yaml](compose.yaml):postgres:16-alpine,仅回环端口 127.0.0.1:5432,凭据来自 .env,数据卷 pgdata,健康检查 pg_isready。集成测试要求专用测试库(库名必须以 `_test` 结尾,否则拒绝运行),每例前 TRUNCATE 清理。

未实现(如实说明):DeepSeek 路径未经测试(无真实 API Key,配置后由 [build_llm_collaborators](app/agent/llm.py) 构建);内存后端仍是默认——单进程、**不可跨重启恢复**,外部副作用**非 exactly-once**,靠全意图幂等键 + UNKNOWN 原操作查询语义收敛。

## Day 5 状态

已就位:PostgreSQL 全链路接入——Agent → services → tools 在同一组合入口上使用一致的 Store 依赖。

**后端选择** — `SUPPORTFLOW_STORE_BACKEND=memory|postgres`(默认 memory)。postgres 显式且 fail-loud:URL 非 `postgresql+psycopg://` 时启动即报错;PG 初始化/连接失败直接抛出,**绝不静默回退内存 Store**;wiring 拒绝注入内存 store 内容与注入计划注册表(禁止内存/PG 混用)。

**PG 模式 wiring**([build_agent_wiring](app/agent/runner.py) 为唯一组合入口)— 退款链路全 PG:退款([PgRefundStore](app/persistence/pg_refund_store.py):行锁、条件预留、审计同事务)、订单([PgOrderStore](app/persistence/pg_stores.py) 读穿透——余额只信 PG 行,余额变更方法直接抛错防误用)、计划([PgPlanStore](app/persistence/pg_stores.py),同一内容哈希契约;`refunds.plan_id` 为真实外键,计划必须先注册才能退款)、审计([PgAuditStore](app/persistence/pg_stores.py),graph 级事件落 `audit_events`)、运行([PgAgentRunStore](app/persistence/pg_stores.py),`audit_events.run_id` 为真实外键)。catalog 在 wiring 时幂等 seed([pg_catalog.py](app/persistence/pg_catalog.py),`ON CONFLICT DO NOTHING`,绝不覆盖已变化的账务)。接线时发现并修复 Day 4 遗留缺陷:`agent_runs.status` 列宽 16 放不下 `awaiting_approval`(17 字符),迁移 [0002](migrations/versions/0002_widen_agent_runs_status.py) 加宽(此前集成测试从未把该状态落库,全链路接线后才暴露)。

**真实 PG 验收** — 38 个集成测试全部通过(原 22 + 新增 16):完整 Agent 链路(自动退款、审批两阶段"申请→等待审批→审批后执行"、独立 session 读取、重建 wiring 后状态存在且二次运行被余额门禁拒绝、claim 封顶读自锁定工单行而非调用方参数)、异常结果处理(UNKNOWN 保留预留并经**原操作查询**解决而非重执行、FAILED 释放预留、REJECTED 释放、幂等不重复记账)、并发双线程至多一笔 PENDING、审计事务原子性(审计写失败则退款整体回滚)、审计↔退款引用完整性;部分唯一索引 `uq_refunds_order_unresolved` 直接约束测试(绕过 store 的裸 INSERT 被数据库按约束名拒绝,与 store 业务层拒绝分开验证);Alembic 0001→0002 downgrade/upgrade 往返与幂等;ORM-migration 逐表逐列 schema 一致性检查。

**持久化边界(如实说明)** — 跨进程重启保留:订单、退款、计划、审计事件、运行记录。仍为内存会话状态(重启丢失):工单状态流转、审批记录、工单备注、物流快照——审批丢失的后果 fail-closed:执行前必须重新走完整审批门禁,不可能凭丢失的审批放行。catalog 的 `non_existent` 场景(工单绑定不存在的订单)在 PG 引用完整性下**不可构造**,PG 模式演示跳过该拒绝路径(内存模式保留);对应地,PG 模式对未 seed 工单的运行在首个外键写入处显式失败,不静默降级。

## 安装与配置

```bash
uv sync
cp .env.example .env   # 缺少 API Key 时一切照常离线运行
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SUPPORTFLOW_APP_ENV` | `development` | development / test / production |
| `SUPPORTFLOW_LOG_LEVEL` | `INFO` | 结构化 JSON 日志级别 |
| `SUPPORTFLOW_DEEPSEEK_API_KEY`(或 `DEEPSEEK_API_KEY`) | 空 | DeepSeek API Key(可选;缺失时用离线默认实现) |
| `SUPPORTFLOW_DEEPSEEK_MODEL`(或 `DEEPSEEK_MODEL`) | `deepseek-chat` | 使用的模型名 |
| `SUPPORTFLOW_DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 地址 |
| `SUPPORTFLOW_DATABASE_URL` | `sqlite:///supportflow.db` | 应用数据库;`SUPPORTFLOW_STORE_BACKEND=postgres` 时必须为 `postgresql+psycopg://`,否则启动报错 |
| `SUPPORTFLOW_STORE_BACKEND` | `memory` | `memory`(默认,单进程)或 `postgres`(退款链路全 PG,见 Day 5 状态) |
| `SUPPORTFLOW_TEST_DATABASE_URL` | 空 | PG 集成测试专用库(库名须以 `_test` 结尾);未设置时集成测试跳过 |
| `SUPPORTFLOW_MAX_AGENT_ITERATIONS` | `10` | Agent 循环硬上限 |
| `SUPPORTFLOW_TOOL_TIMEOUT_SECONDS` | `30` | 单次工具调用超时 |

安全约定:API Key 只通过环境变量注入,禁止写入源码或日志;日志层对 secret 类字段统一脱敏。

PG 后端启动与迁移:

```bash
docker compose up -d --wait          # 本地 PG(仅回环 127.0.0.1:5432),凭据来自 .env
uv run alembic upgrade head          # 建表/升级(0001 → 0002,幂等;downgrade 可回退)
```

## 测试

```bash
uv run pytest          # 243 个单元/图测试,不依赖网络与真实 LLM
```

PG 集成测试(38 个,含双进程竞态与 Day 5 全链路)需要本地 PostgreSQL 与专用测试库:

```bash
docker compose up -d --wait   # 启动本地 PG(仅回环),等待健康检查
docker compose exec postgres createdb -U supportflow supportflow_test
SUPPORTFLOW_TEST_DATABASE_URL=postgresql+psycopg://supportflow:<密码>@127.0.0.1:5432/supportflow_test \
  uv run pytest tests/integration -q
```

测试不依赖真实 LLM、真实退款或网络(243 个测试,0 skip;集成测试未设置 `SUPPORTFLOW_TEST_DATABASE_URL` 时跳过,库名不以 `_test` 结尾时直接失败,防止误连开发/生产库)。Day 3 场景断言实际工具调用次数、退款记录、余额、审批状态、工单状态与审计结果,而不只是回复文本;含静态守卫:Agent 源码不得引用 `ApprovalStatus.APPROVED`、`decide` 或直接修改退款状态。P1 加固回归([tests/test_p1_fixes.py](tests/test_p1_fixes.py))覆盖:哈希缺失审批 fail-closed(图 + 服务边界)、服务层独立风险审批规则(HIGH/CRITICAL 无审批拒绝、伪造低风险审批拒绝)、工单索赔封顶(并发双跑至多一笔、已结算未 RESOLVED 重跑不再退款、UNKNOWN 门禁保持、不同工单互不影响)。第二轮复审回归([tests/test_review2_fixes.py](tests/test_review2_fixes.py))覆盖:FAILED 退款同键重试在原子临界区内重查工单索赔额度(额度被占后拒绝且不预留、不触达网关;额度空闲时复用原记录重试成功)、相同 requested_at 审批的确定性排序规则(后插入者胜,服务层与审批服务共用)、请求审批节点经 `load_verified` 加载计划(漂移 fail-closed)、工单状态原子条件转换(失败运行无法把已 RESOLVED 工单拉回 IN_PROGRESS)。

## 模拟数据

[app/persistence/mock_catalog.py](app/persistence/mock_catalog.py) 提供 6 个确定性场景,全部为硬编码常量(无随机、无系统时钟):

| 场景 | 形态 |
| --- | --- |
| `refundable` | 已签收订单,尚未退款 |
| `already_refunded` | 已签收订单,已全额退款 |
| `cancelled` | 发货前取消的订单 |
| `amount_mismatch` | 工单索赔金额高于订单总额 |
| `delivered_not_received` | 物流显示已签收,客户称未收到(高风险,需审批) |
| `non_existent` | 工单引用的订单号不存在 |

```bash
uv run python -m app.persistence.mock_catalog
```

## 项目结构

```
app/
├── __init__.py
├── config.py            # Settings(env 前缀 SUPPORTFLOW_)
├── logging_config.py    # 结构化 JSON 日志 + 密钥脱敏
├── api/                 # FastAPI 工厂(目前仅 /health)
├── agent/
│   ├── state.py         # AgentState(TypedDict)与阶段常量
│   ├── graph.py         # LangGraph 图:节点、条件路由、有界循环
│   ├── llm.py           # 分类/计划/回复接口 + 离线默认实现 + DeepSeek 实现
│   ├── runner.py        # build_agent_wiring / run_ticket(RunOutcome)
│   └── demo.py          # 确定性端到端演示(离线)
├── domain/              # 枚举、金额规则、Pydantic 模型(不依赖任何框架)
├── persistence/
│   ├── base.py          # SQLAlchemy Base
│   ├── memory_stores.py # 内存存储:锁保护的原子操作(运行时默认)
│   ├── refund_store.py  # RefundStoreProtocol + 创建结果类型(11 种 kind)
│   ├── models.py        # 8 表 ORM 模型(FK/CHECK/部分唯一索引)
│   ├── pg_refund_store.py  # PostgreSQL 退款存储:行锁 + 条件更新 + 审计同事务
│   ├── db.py            # engine/session 工厂(按 URL 方言选择,失败不静默回退)
│   ├── pg_stores.py     # PG 订单/计划/审计/运行存储(订单读穿透,余额变更抛错)
│   ├── pg_catalog.py    # PG 模式 catalog:幂等 seed(ON CONFLICT DO NOTHING)
│   └── mock_catalog.py  # 6 个确定性模拟场景
├── services/
│   ├── refund_policy.py # 结构化退款政策(纯函数,默认拒绝)
│   ├── payments.py      # 模拟支付网关(可注入失败/未知结果)
│   ├── refunds.py       # 退款安全边界:校验、审批门禁(含计划哈希复核)、幂等
│   ├── plans.py         # PlanStore:内容哈希、同 id 异内容拒绝、读取重算
│   ├── approvals.py     # ApprovalService:请求去重、decide(人工侧入口)
│   └── audit.py         # 审计事件记录器
└── tools/
    ├── result.py        # ToolResult 统一结果信封 + ToolErrorCode
    ├── context.py       # ToolContext(工单/运行上下文)
    ├── schemas.py       # 工具输入模型(Pydantic 校验)
    ├── audit.py         # audited 装饰器(审计 + 异常收敛)
    └── registry.py      # ToolSet + build_default_tool_set(唯一工具注册入口)
tests/
├── fixtures/mock_data.py  # 向后兼容再导出(app.persistence.mock_catalog)
├── tools_util.py          # 构建注入式 ToolSet 的测试辅助
├── test_plan_store.py     # PlanStore:哈希确定性、幂等重注册、漂移检测
├── test_approval_service.py  # ApprovalService:绑定、去重、decide 契约、审计
├── test_agent_day3.py     # 图行为 16 场景 + 静态守卫(断言状态而非文本)
├── test_day4_batch1.py    # Day 4 批 1:PG 模型/迁移/会话工厂契约
├── test_day5_wiring.py    # Day 5 wiring 单测:后端选择、fail-loud、注入拒绝(不连库)
├── integration/           # PG 集成测试(38 个:Day 4 双进程竞态 + Day 5 全链路/约束/schema)
└── test_*.py              # Day 1-2 的领域/工具/审计测试
migrations/                # Alembic:0001_initial(8 表)+ 0002 加宽 agent_runs.status,upgrade 幂等
compose.yaml               # 本地开发 PostgreSQL(postgres:16-alpine,仅回环)
```
