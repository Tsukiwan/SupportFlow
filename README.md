# SupportFlow

AI 客服工单处理 Agent:查询订单、检查退款政策、提出处理方案,高风险操作前等待人工审批。所有退款均为模拟操作。

## 项目边界(重要)

- ✅ 构建:工单领域模型、订单查询、退款政策检查、处理方案生成、人工审批门禁、审计日志
- ❌ 不构建:RAG、向量数据库、知识库问答系统
- ⚠️ 退款为模拟操作,不连接真实支付网关
- 模型供应商:DeepSeek(OpenAI 兼容 API),Key 与模型名通过环境变量配置

## Day 1 状态

已就位:领域模型与枚举、金额规则(Decimal,拒绝 float)、统一 ToolResult、配置(环境变量 + SecretStr)、结构化 JSON 日志(密钥脱敏)、确定性模拟数据(6 个场景)、测试基线(50 个测试)。

## Day 2 状态

已就位:业务工具层与退款安全边界。5 个工具(`get_order` / `get_shipping_status` / `get_refund_policy` / `create_refund` / `add_ticket_note`)全部返回统一 ToolResult;退款工具在服务端独立完成全部安全校验(工单-订单绑定、订单存在性与状态、金额精度、实时可退余额、结构化政策、审批绑定订单/金额/计划版本/风险等级/有效期、全意图幂等键、并发互斥、单笔未决退款门禁),不信任 LLM 或调用方提供的任何状态声明——issue_type 与政策一律以工单记录为准,谎报直接拒绝;所有退款走模拟支付网关,支持业务拒绝/确认未执行的暂时性失败/结果未知三类非成功结果,网关异常与任务取消一律按未知结果处理(保留余额预留),未知结果必须先查询原操作再处理,绝不盲目二次执行;同一订单存在 PENDING/UNKNOWN 退款时,异键新退款被拒绝,直到原操作被确认处理、确认拒绝或确认未执行并安全释放;订单余额区分「已确认退款」与「预留」,未决退款不会被误报为已退款;每次工具调用写入审计事件(工具名、结果类别、耗时、run_id、关联 ID,无密钥无 PII),取消的调用单独记为 cancelled,不会误记 success。

未实现:Agent 循环(LangGraph,Day 3)与持久化(PostgreSQL,Day 4)。当前存储为内存实现(锁保护的原子操作),跨进程/跨重启的幂等与余额约束需在 PostgreSQL 中实现并验证。

## 测试

```bash
uv run pytest
```

测试不依赖真实 LLM、真实退款或网络(164 个测试,0 skip)。

## 项目结构

```
app/
├── __init__.py
├── config.py            # Settings(env 前缀 SUPPORTFLOW_,导入时不实例化)
├── logging_config.py    # 结构化 JSON 日志 + 密钥脱敏
├── api/                 # FastAPI 工厂(目前仅 /health)
├── agent/               # 预留:LangGraph Agent 循环(Day 3)
├── domain/              # 枚举、金额规则、Pydantic 模型(不依赖任何框架)
├── persistence/
│   ├── base.py          # SQLAlchemy Base(PostgreSQL 预留)
│   ├── memory_stores.py # 内存存储:锁保护的原子操作(Day 2 测试用)
│   └── mock_catalog.py  # 6 个确定性模拟场景(订单/工单/物流)
├── services/
│   ├── refund_policy.py # 结构化退款政策(纯函数,默认拒绝)
│   ├── payments.py      # 模拟支付网关(可注入失败/未知结果)
│   ├── refunds.py       # 退款安全边界:校验、审批门禁、幂等、结果解析
│   └── audit.py         # 审计事件记录器
└── tools/
    ├── result.py        # ToolResult 统一结果信封 + ToolErrorCode
    ├── context.py       # ToolContext(工单/运行上下文)
    ├── schemas.py       # 工具输入模型(Pydantic 校验)
    ├── audit.py         # audited 装饰器(审计 + 异常收敛)
    └── registry.py      # ToolSet + build_default_tool_set
tests/
├── fixtures/mock_data.py  # 向后兼容再导出(app.persistence.mock_catalog)
├── tools_util.py          # 构建注入式 ToolSet 的测试辅助
├── test_enums.py
├── test_money.py
├── test_domain_models.py
├── test_day2_models.py
├── test_tool_result.py
├── test_config.py
├── test_logging.py
├── test_fixtures.py
├── test_api.py
├── test_refund_policy.py
├── test_tools_query.py
├── test_create_refund.py
├── test_ticket_note.py
└── test_audit.py
```

## 模拟数据

[app/persistence/mock_catalog.py](app/persistence/mock_catalog.py) 提供 6 个确定性场景,全部为硬编码常量(无随机、无系统时钟),重新生成永远得到相同数据:

| 场景 | 形态 |
| --- | --- |
| `refundable` | 已签收订单,尚未退款 |
| `already_refunded` | 已签收订单,已全额退款 |
| `cancelled` | 发货前取消的订单 |
| `amount_mismatch` | 工单索赔金额高于订单总额 |
| `delivered_not_received` | 物流显示已签收,客户称未收到 |
| `non_existent` | 工单引用的订单号不存在 |

查看/重新生成:

```bash
uv run python -m app.persistence.mock_catalog
```

## 环境要求

- Python 3.12+(仓库内有 `.python-version`,uv 会自动获取匹配的解释器)
- [uv](https://docs.astral.sh/uv/)

## 安装

```bash
uv sync
```

## 配置

```bash
cp .env.example .env
# 按需填写;缺少 API Key 时本地测试照常运行
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SUPPORTFLOW_APP_ENV` | `development` | development / test / production |
| `SUPPORTFLOW_LOG_LEVEL` | `INFO` | 结构化 JSON 日志级别 |
| `SUPPORTFLOW_DEEPSEEK_API_KEY`(或 `DEEPSEEK_API_KEY`) | 空 | DeepSeek API Key(可选,Day 2 起使用) |
| `SUPPORTFLOW_DEEPSEEK_MODEL`(或 `DEEPSEEK_MODEL`) | `deepseek-chat` | 使用的模型名 |
| `SUPPORTFLOW_DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 地址 |
| `SUPPORTFLOW_DATABASE_URL` | `sqlite:///supportflow.db` | 后续切换 PostgreSQL |
| `SUPPORTFLOW_MAX_AGENT_ITERATIONS` | `10` | Agent 循环硬上限 |
| `SUPPORTFLOW_TOOL_TIMEOUT_SECONDS` | `30` | 单次工具调用超时 |

安全约定:API Key 只通过环境变量注入,禁止写入源码或日志;日志层对 secret 类字段(key/token/password/credential 等)统一脱敏。

## 测试

```bash
uv run pytest
```

测试不依赖真实 LLM、真实退款或网络。

## 项目结构

```
app/
├── __init__.py
├── config.py            # Settings(env 前缀 SUPPORTFLOW_,导入时不实例化)
├── logging_config.py    # 结构化 JSON 日志 + 密钥脱敏
├── api/                 # FastAPI 工厂(目前仅 /health)
├── agent/               # 预留:LangGraph Agent 循环(Day 2+)
├── domain/              # 枚举、金额规则、Pydantic 模型(不依赖任何框架)
├── persistence/         # SQLAlchemy Base(PostgreSQL 预留)
├── services/            # 业务服务(订单查询、退款策略等,Day 2+)
└── tools/               # ToolResult 统一结果信封 + ToolErrorCode
tests/
├── fixtures/mock_data.py  # 6 个确定性模拟场景
├── test_enums.py
├── test_money.py
├── test_domain_models.py
├── test_tool_result.py
├── test_config.py
├── test_logging.py
├── test_fixtures.py
└── test_api.py
```

## 模拟数据

[tests/fixtures/mock_data.py](tests/fixtures/mock_data.py) 提供 6 个确定性场景,全部为硬编码常量(无随机、无系统时钟),重新生成永远得到相同数据:

| 场景 | 形态 |
| --- | --- |
| `refundable` | 已签收订单,尚未退款 |
| `already_refunded` | 已签收订单,已全额退款 |
| `cancelled` | 发货前取消的订单 |
| `amount_mismatch` | 工单索赔金额高于订单总额 |
| `delivered_not_received` | 物流显示已签收,客户称未收到 |
| `non_existent` | 工单引用的订单号不存在 |

查看/重新生成:

```bash
uv run python -m tests.fixtures.mock_data
```
