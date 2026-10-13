# ARCHITECTURE.md — TeachOnce 架构设计

> 阅读顺序：`AGENTS.md` → `docs/PRODUCT.md` → 本文件。
> 本文件描述 v0.1.0 MVP 的**实际**架构；标注「计划中」的部分尚未实现。

## 1. 系统总览

TeachOnce 是一个**纯 Python 库 + MCP Server**，无独立 UI、无后台常驻进程（MVP 阶段）。

```
┌─────────────────────────────────────────────────────────────────┐
│  宿主 Agent（WorkBuddy / Claude Code / Cursor / 任意 MCP 客户端） │
└───────────────┬─────────────────────────────┬───────────────────┘
                │ MCP (stdio)                 │ 导入 Agent Skills
                ▼                             ▼
┌───────────────────────────────┐   ┌─────────────────────┐
│   mcp_server.py (FastMCP)     │   │  skill_export.py    │
│  11 个工具：导入/生成/匹配/     │   │  SKILL.md 标准导出   │
│  纠错/审批/回滚/评估/导出       │   └─────────────────────┘
└───────┬───────────────────────┘              ▲
        │ 编排（无业务逻辑）                     │
        ▼                                       │
┌───────────────────────────────────────────────────────────────┐
│                         核心引擎层                             │
│  learning.py    → 示范导入 → Skill 提取（决策规则/异常/反面）    │
│  matching.py    → 新任务 ↔ Skill 匹配（置信度/理由/转人工）      │
│  correction.py  → 纠错 → 修订版本（changelog/审批/回滚）        │
│  evaluation.py  → 未见案例 → 新旧版本可靠性对比                 │
└───────────┬───────────────────────────────────┬───────────────┘
            ▼                                   ▼
┌─────────────────────────┐         ┌───────────────────────────┐
│  store.py (SQLite)      │         │  llm.py (Provider 抽象)    │
│  append-only 版本表      │         │  ├ OpenAICompatibleLLM     │
│  ~/.teachonce/*.db      │         │  └ ScriptedLLM（仅测试）    │
└─────────────────────────┘         └───────────────────────────┘
                                                ▲
                        计划中：adapter 层 ───────┘
                        （OpenAdapt / Hermes，经适配器隔离，未实现）
```

## 2. 学习循环（产品灵魂）

```
 示范① 示范② 示范③       纠错反馈          未见案例
    │        │              │                │
    ▼        ▼              ▼                ▼
 [import] → [extract] → [match] → [correct] → [evaluate]
    │          │            │         │            │
    │          │            │         │            └─ v2 回归验证（不劣于 v1 且修复问题）
    │          │            │         └─ correction → v2（changelog / candidate → approve）
    │          │            └─ 推断适用性 + 理由；低置信 → requires_confirmation
    │          └─ LLM 从多示范中归纳：共性规则 + 分歧点 + 证据标注
    └─ schema 校验 + 敏感信息拦截 → SQLite
```

关键设计：**新版本永远不覆盖旧版本**（append-only），评估和回滚都作用于版本序列。

## 3. 数据模型（`models.py`）

全部为无副作用 `dataclass`：

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `Observation` | action, target, decision, rationale | 示范中的单步操作 |
| `Demonstration` | id, task_goal, context, steps, key_judgments, outcome, tags | 一次任务示范 |
| `DecisionRule` | id, condition, decision, rationale, confidence, sources | 决策规则（学习核心产物） |
| `SkillVersion` | skill_id, version, trigger_conditions, decision_rules, exception_handling, anti_patterns, changelog, status | 一个 Skill 的某一版 |
| `Skill` | id, name, description, active_version, created_at, updated_at | Skill 聚合根 |
| `Correction` | id, skill_id, target_version, feedback, correction_type | 一次用户纠错 |
| `EvalReport` | skill_id, version_a, version_b, method, score_a, score_b, results | 版本对比报告 |
| `MatchResult` | skill_id, confidence, rationale, requires_confirmation | 匹配结论 |

版本状态机：`candidate ──approve──▶ approved`；`approved ◀──rollback── 指针回退`（历史版本不可变）。
`Skill.active_version` 指针只指向 `approved` 版本；匹配引擎只考虑 active 版本的 Skill。

## 4. 存储层（`store.py`，SQLite）

- 默认路径 `~/.teachonce/teachonce.db`（可用 `TEACHCONE_HOME` 覆盖）；**数据默认不出本机**。
- 表：`demonstrations` / `skills` / `skill_versions` / `corrections` / `eval_reports` / `match_logs`。
- `skill_versions` 主键 `(skill_id, version)`，**只增不改**；`skills.active_version` 单独维护指针。
- 复杂结构（步骤、规则列表）以 JSON 文本列存储——SQLite + JSON 是"单文件、零运维、可检查"的平衡点；避免过早引入向量库/服务化。
- 时间戳统一 UTC ISO-8601。

## 5. LLM Provider 层（`llm.py`）

```
LLMProvider (ABC)
├── OpenAICompatibleLLM   # 生产：httpx 直连 /chat/completions
│     —— 兼容 DeepSeek / OpenAI / Ollama / 任意 OpenAI 兼容端点
└── ScriptedLLM           # 仅测试：按序返回预置响应（fixture 道具）
```

- 配置：`TEACHCONE_LLM_API_KEY` / `TEACHCONE_LLM_BASE_URL` / `TEACHCONE_LLM_MODEL`。
- 未配置 Key → 抛 `LLMNotConfiguredError`（信息含修复指引）；**绝不返回假结果**。
- 为什么不用 `openai` SDK：保持依赖最小（httpx 已是 mcp 的传递依赖），且只需一个 POST 接口；模块化设计允许未来加 Provider。
- 为什么需要 ScriptedLLM：LLM 输出不确定，测试必须验证"管道"（JSON 解析、schema 校验、版本流转、错误路径）而非模型质量；CI 环境无 Key 也必须全绿。ScriptedLLM 与生产路径完全隔离，**不可能**被生产代码导入使用（仅 `tests/` 内构造注入）。

## 6. 核心引擎设计

### 6.1 learning.py（示范 → Skill）
- 输入：一组 demonstration id + skill 名称。
- 流程：装载示范 → 组装归纳 prompt → LLM（JSON 模式）→ 清洗 & `json.loads` → schema 校验（必填字段、类型、规则 id 自动补全）→ 存为 `candidate` v1。
- Prompt 约束（产品灵魂，改动需测试覆盖）：
  - 提取**跨示范共性**与**条件性差异**，不逐条复述操作；
  - 每条规则必须引用证据来源（示范 id），证据不足标注 `open_questions`，**禁止编造**；
  - `rationale` 只能源于示范/纠错中的依据。

### 6.2 matching.py（任务 ↔ Skill）
- 输入：任务描述 + 候选 Skill 列表（active 版本）。
- 输出：`MatchResult`（含置信度、理由、缺失信息）。
- 规则：置信度 < 阈值（默认 0.6）→ `requires_confirmation=True`；不确定就转人工，**不硬套**。
- 匹配只做"选 Skill + 说明理由 + 给行动计划"，**不执行**任何真实操作。

### 6.3 correction.py（纠错 → 新版本）
- `record_correction`：LLM 归类（wrong_decision / missing_rule / too_broad / too_narrow / other）+ 摘要。
- `apply_correction`：输入原版本 + 全部相关纠错 → LLM 输出修订版规则（每条变更带 `op`(add/modify/remove) 与理由）→ 落库为 `candidate` 新版本，changelog 结构化。
- `approve_version` / `rollback_to`：人工审批后 active 指针移动；回滚仅移动指针，历史可追溯。

### 6.4 evaluation.py（未见案例回归）
- 输入：cases = `[{situation, expected_decision, notes}]`（不得来自训练示范）。
- 两种评估方法，**报告必须标注所用方法**：
  1. `llm_judge`（默认，需 API）：LLM 判断"该版本规则是否支持得出 expected_decision"；
  2. `heuristic`（离线）：规则文本与期望决策的关键词/短语重叠 —— **启发式，非语义理解**，报告中明确标注局限。
- 输出 `EvalReport`（score_a / score_b / per-case 明细）落库；比较结论遵循"v2 不劣于 v1 且修复被纠错问题"的验收口径。

## 7. MCP 接口（`mcp_server.py`，FastMCP）

工具面（11 个，全部纯编排调用引擎层）：

| 工具 | 引擎调用 |
|---|---|
| `teachonce_import_demonstration` | learning.import_demonstration |
| `teachonce_generate_skill` | learning.extract_candidate_skill |
| `teachonce_list_skills` / `teachonce_get_skill` | store |
| `teachonce_match_skill` | matching.match |
| `teachonce_record_correction` | correction.record_correction |
| `teachonce_apply_correction` | correction.apply_correction |
| `teachonce_approve_version` / `teachonce_rollback` | correction 审批/回滚 |
| `teachonce_evaluate_versions` | evaluation.evaluate |
| `teachonce_export_skill_md` | skill_export |

传输：stdio（默认）。返回值一律为 JSON-able dict；错误以异常抛出（MCP 会转为工具错误），不吞错。

## 8. Agent Skills 导出（`skill_export.py`）

输出标准 `SKILL.md`（YAML frontmatter: `name` / `description` + Markdown 正文：适用条件 / 决策规则 / 异常处理 / 反面模式 / 版本与来源）。
frontmatter 手写序列化（避免引入 PyYAML）；保证其他兼容 Agent 可直接导入。

## 9. 适配器层（计划中，未实现）

- `adapters/openadapt.py` — GUI 观察/示范采集（需显式授权）；`adapters/hermes.py` — Skill 互操作。
- 设计约束：适配器只做协议转换，核心引擎不感知外部 Agent 类型；未集成前文档不得宣称可用。

## 10. 安全设计

| 威胁 | 对策 |
|---|---|
| 示范中夹带密码/Token | 入库前正则红灯扫描（`SensitiveContentError`） |
| Skill 自动执行危险操作 | TeachOnce 不直接操作用户文件系统；只输出计划，执行在宿主 Agent 且需授权 |
| 错误经验固化 | 更新一律为 candidate → 人工 approve；结构化 changelog；指针回滚 |
| 评估造假 | EvalReport 记录 method 字段；heuristic 模式报告内强制标注局限 |
| 数据泄露 | 本地 SQLite 默认；无任何上传逻辑（MVP） |

## 11. 技术选型记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.12+ | MCP SDK / Agent 生态默认；类型标注成熟 |
| 存储 | SQLite + JSON 列 | 单文件、零运维、可 diff；规模未到向量库需求 |
| LLM 调用 | httpx 直连 OpenAI 兼容 API | 最小依赖；多供应商无锁定 |
| MCP Python SDK | MCP Python SDK（v2 `MCPServer`，原 `FastMCP`） | 标准协议实现，不依赖第三方分支 |
| 不用 | LangChain 等重框架 | MVP 复杂度不需要；保持可审计 |

## 12. 测试策略

- `tests/` 全量 ScriptedLLM 注入，CI 无 Key 可跑（`.github/workflows/ci.yml`：ruff + pytest）。
- 覆盖：模型序列化、存储版本流转、学习管道（含坏 JSON/敏感内容错误路径）、匹配阈值、纠错版本/审批/回滚、评估两种方法、导出格式、MCP 工具端到端、Demo 全流程回归。
- 真实 LLM 手工验收：`python -m demo.run_demo`（需 Key；输出即真实学习结果，不做装饰性美化）。
