# SupportFlow 简历项目描述

可直接粘贴进简历的两个版本。所有事实均有仓库源码、测试或本仓库 README 记录支持;测试数字对应 `tests/`(单元/图测试)与 `tests/integration/`(真实 PostgreSQL 16 集成测试)两个套件,验证日期 2026-10-11。未包含任何虚构的业务指标、用户数或生产部署经历。

## 版本一:中文简历

**SupportFlow —— 基于 LangGraph 与 PostgreSQL 的 AI 客服退款 Agent(个人项目)**

- 实现 LangGraph 编排的客服退款 Agent:LLM 只产出计划提议,工单绑定、退款政策、审批门禁、金额与幂等全部由确定性服务层校验,高风险退款必须经绑定计划内容哈希的人工审批;262 个单元测试覆盖决策与安全边界。
- 设计并实现 PostgreSQL 持久化层的退款安全机制:全意图幂等键 + 数据库唯一约束防重复退款、条件 UPDATE 原子预留额度 + 库级 CHECK 兜底、行锁 + 状态复查保证并发恰一次结算、审计与账务变更同事务提交;51 个集成测试在真实 PostgreSQL 16 上验证(含 multiprocessing 双进程竞态与审计故障注入回滚)。
- 解决「外部网关调用与本地事务不原子」的故障窗口:网关异常一律记 UNKNOWN 保留预留(fail-closed),实现基于数据库租约(owner token + 过期时间)与只读状态查询的恢复服务——结构上无执行路径、仅凭确凿证据结算或释放,迟到 worker 的过期结果被拒绝;以两进程崩溃恢复 demo 演示端到端收敛与不重复结算。

技术栈:Python · LangGraph · SQLAlchemy · PostgreSQL · Alembic · Pydantic · FastAPI · pytest · uv

## 版本二:英文简历

**SupportFlow — AI Customer-Support Refund Agent with LangGraph and PostgreSQL (personal project)**

- Built a LangGraph-orchestrated refund agent where the LLM only proposes plans: ticket-order binding, refund policy, approval gates, amounts and idempotency are enforced by a deterministic service layer, and high-risk refunds require human approval bound to a content hash of the exact plan reviewed (262 unit/graph tests).
- Designed PostgreSQL-backed refund safety: full-intent idempotency keys with unique constraints, conditional-UPDATE balance reservation with a database CHECK backstop, row-lock + status-recheck settlement that settles exactly once under concurrency, and audit events committed in the same transaction as the money movement (51 integration tests on real PostgreSQL 16, incl. multiprocessing two-process races and audit-failure rollback injection).
- Closed the "external gateway call vs. local transaction" crash window: gateway exceptions become UNKNOWN with the reservation held (fail-closed); a recovery service queries the original operation through a database lease (owner token + expiry) and settles only on definitive evidence — structurally no execute path, stale workers' late results are rejected (two-process crash-recovery demo shows exactly-once settlement).

Tech stack: Python · LangGraph · SQLAlchemy · PostgreSQL · Alembic · Pydantic · FastAPI · pytest · uv

## 使用说明

- 「个人项目」定位建议保留:本仓库为教学/作品集项目,退款经模拟网关执行,未接入真实支付网关。
- 面试中如需展开,配合 [DEMO_SCRIPT.md](DEMO_SCRIPT.md)(3–5 分钟演示)与 [INTERVIEW_QA.md](INTERVIEW_QA.md)(12 题问答)。
- 若简历篇幅受限,每条描述的第二句(测试证据)可整句删去,保留「实现 + 解决」部分。
