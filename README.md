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

未实现:持久化(PostgreSQL,Day 4);DeepSeek 路径未经测试(无真实 API Key,配置后由 [build_llm_collaborators](app/agent/llm.py) 构建);当前存储为内存实现(单进程),**不可跨重启恢复**,外部副作用**非 exactly-once**(靠全意图幂等键 + UNKNOWN 原操作查询语义收敛,最终一致性由 Day 4 数据库约束保证)。

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
| `SUPPORTFLOW_DATABASE_URL` | `sqlite:///supportflow.db` | 后续切换 PostgreSQL |
| `SUPPORTFLOW_MAX_AGENT_ITERATIONS` | `10` | Agent 循环硬上限 |
| `SUPPORTFLOW_TOOL_TIMEOUT_SECONDS` | `30` | 单次工具调用超时 |

安全约定:API Key 只通过环境变量注入,禁止写入源码或日志;日志层对 secret 类字段统一脱敏。

## 测试

```bash
uv run pytest
```

测试不依赖真实 LLM、真实退款或网络(230 个测试,0 skip)。Day 3 场景断言实际工具调用次数、退款记录、余额、审批状态、工单状态与审计结果,而不只是回复文本;含静态守卫:Agent 源码不得引用 `ApprovalStatus.APPROVED`、`decide` 或直接修改退款状态。P1 加固回归([tests/test_p1_fixes.py](tests/test_p1_fixes.py))覆盖:哈希缺失审批 fail-closed(图 + 服务边界)、服务层独立风险审批规则(HIGH/CRITICAL 无审批拒绝、伪造低风险审批拒绝)、工单索赔封顶(并发双跑至多一笔、已结算未 RESOLVED 重跑不再退款、UNKNOWN 门禁保持、不同工单互不影响)。第二轮复审回归([tests/test_review2_fixes.py](tests/test_review2_fixes.py))覆盖:FAILED 退款同键重试在原子临界区内重查工单索赔额度(额度被占后拒绝且不预留、不触达网关;额度空闲时复用原记录重试成功)、相同 requested_at 审批的确定性排序规则(后插入者胜,服务层与审批服务共用)、请求审批节点经 `load_verified` 加载计划(漂移 fail-closed)、工单状态原子条件转换(失败运行无法把已 RESOLVED 工单拉回 IN_PROGRESS)。

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
│   ├── base.py          # SQLAlchemy Base(PostgreSQL 预留)
│   ├── memory_stores.py # 内存存储:锁保护的原子操作
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
└── test_*.py              # Day 1-2 的领域/工具/审计测试
```
