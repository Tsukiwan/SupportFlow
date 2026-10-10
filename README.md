# SupportFlow

AI 客服工单处理 Agent:查询订单、检查退款政策、提出处理方案,高风险操作前等待人工审批。所有退款均为模拟操作。

## 项目边界(重要)

- ✅ 构建:工单领域模型、订单查询、退款政策检查、处理方案生成、人工审批门禁、审计日志
- ❌ 不构建:RAG、向量数据库、知识库问答系统
- ⚠️ 退款为模拟操作,不连接真实支付网关
- 模型供应商:DeepSeek(OpenAI 兼容 API),Key 与模型名通过环境变量配置

## Day 1 状态

已就位:领域模型与枚举、金额规则(Decimal,拒绝 float)、统一 ToolResult、配置(环境变量 + SecretStr)、结构化 JSON 日志(密钥脱敏)、确定性模拟数据(6 个场景)、测试基线(50 个测试)。

未实现:Agent 循环(LangGraph)与持久化(PostgreSQL)——依赖已加入,目录已预留。

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
