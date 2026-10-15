# ARCHITECTURE.md — TeachOnce 架构设计

> 阅读顺序：`AGENTS.md` → `docs/PRODUCT.md` → 本文件。
> 本文件描述 v0.4.0 的**实际**架构；标注「计划中」的部分尚未实现。

## 1. 系统总览

TeachOnce 是一个**纯 Python 库 + MCP Server**，无独立 UI、无后台常驻进程（MVP 阶段）。

```
┌─────────────────────────────────────────────────────────────────┐
│  宿主 Agent（WorkBuddy / Claude Code / Cursor / 任意 MCP 客户端） │
└───────────────┬─────────────────────────────┬───────────────────┘
                │ MCP (stdio)                 │ 导入 Agent Skills
                ▼                             ▼
┌───────────────────────────────┐   ┌─────────────────────┐
│   mcp_server.py (MCPServer)   │   │  skill_export.py    │
│  22 个工具：上下文/学习/匹配/   │   │  SKILL.md 标准导出   │
│  纠错/审批/回滚/评估/导出/      │   └─────────────────────┘
│  历史/优化/GUI 导入            │
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
                        adapters/ 层 ────────────┘
                        （GEPA 优化 / OpenAdapt 录制导入）
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

- 默认路径 `~/.teachonce/teachonce.db`（`TEACHONCE_HOME` 优先，兼容 `TEACHCONE_HOME`）；持久化默认在本机，推理上下文会返回宿主或发送至配置的 API。
- 表：`demonstrations` / `skills` / `skill_versions` / `corrections` / `eval_reports` / `match_logs` /
  `activation_events` / `outcomes`。新增表使用 CREATE TABLE IF NOT EXISTS，保留旧数据。
- `skill_versions` 主键 `(skill_id, version)`，**只增不改**；`skills.active_version` 单独维护指针。
- 复杂结构（步骤、规则列表）以 JSON 文本列存储——SQLite + JSON 是"单文件、零运维、可检查"的平衡点；避免过早引入向量库/服务化。
- 时间戳统一 UTC ISO-8601。
- `save_revision` 使用 `BEGIN IMMEDIATE` 在同一事务中分配版本号、追加版本、更新纠错状态；
  对纠错所属 Skill 和 `open` 状态做条件更新，冲突时整笔回滚。模型调用在事务外完成。
- `create_skill_with_version` 原子保存初始聚合，失败不留孤立 Skill；优化用 `save_candidate`
  原子分配版本。`activate_version` 检查预期 active 和候选状态，原子更新指针/状态/审计。
  `save_outcome` 将上报和其显式反馈关联纠错一起保存。所有模型调用仍在事务外。

## 5. LLM Provider 层（`llm.py`）

```
LLMProvider (ABC)
├── OpenAICompatibleLLM   # 生产：httpx 直连 /chat/completions
│     —— 兼容 DeepSeek / OpenAI / Ollama / 任意 OpenAI 兼容端点
└── ScriptedLLM           # 仅测试：按序返回预置响应（fixture 道具）
```

- 配置：推荐 `TEACHONCE_LLM_API_KEY` / `TEACHONCE_LLM_BASE_URL` / `TEACHONCE_LLM_MODEL`，
  旧 TEACHCONE 拼写作为回退。远程 URL 必须 HTTPS，不允许用户信息、查询和片段；localhost 可用 HTTP。
- 未配置 Key → 抛 `LLMNotConfiguredError`（信息含修复指引）；**绝不返回假结果**。
- 为什么不用 `openai` SDK：保持依赖最小（httpx 已是 mcp 的传递依赖），且只需一个 POST 接口；模块化设计允许未来加 Provider。
- ScriptedLLM 仅由测试显式构造，生产默认路径不选它。测试验证管道，不代表模型质量。
- 宿主推理：learning_context / revision_context / matching_context 返回真实上下文；宿主提交明确
  proposal（纠错分类用 classification）后走相同校验和保存路径，不构造返回假答案的 Provider。
  SkillVersion.derived_from.inference_sources 记录 host-proposal；修订绑定明确基准及反馈 ID，
  匹配提案绑定当前 matched_version，避免旧上下文误用。
  `base_versions` 记录直接父版本，`activation_base` 记录该候选链起点的启用基准；
  支持审批前连续修订候选，批准时仍必须匹配起点，不能绕过期间发生的 active 变更。
- 所有模型 JSON 先敏感扫描，再严格解析；重复键、NaN/Infinity 拒绝。HTTP 错误不回显响应正文，
  LLMConfig 的 Key 不进入 repr。结构类型不再靠 str()/float() 把异常值强制当成正确输出。

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
- 显式纠错 ID 必须唯一、属于当前 Skill 且仍为 `open`；调用模型前检查，入库时再次验证状态以处理并发。
- 修订继承基准版本的累计纠错来源；校验器保留模型提供的规则 ID，并拒绝重复 / 非法 ID，使变更说明可对应到规则。
- 修订保留 `derived_from` 的全部来源类别，包括 `optimizations` 和 `optimization_cases`，
  避免纠错后丢失调优记录、使已用于学习的案例重新进入未见评估集。
- `approve_version` / `rollback_to`：人工审批后 active 指针移动；回滚仅移动指针，历史可追溯。
- `review_version` 显示实际字段差异与报告资格；批准需 evaluation_id、confirmed、reviewer、reason。
  初版报告对应候选自身，修订对应当前 active/候选且逐例通过；旧基准报告或候选被拒绝。
  确认值不是身份认证，宿主需取得真实授权。rolled_back 的既有批准版本可再次显式恢复。

### 6.4 evaluation.py（未见案例回归）
- 输入：cases = `[{situation, expected_decision, notes}]`（不得来自训练示范）。
- 三种评估方法，**报告必须标注所用方法**：
  0. `llm_blind`（有 Provider 时 auto 默认）：预测阶段只给规则/情境，不传期望；记录预测后
     用独立调用比较答案。每例两版本共四次调用，仍存在同模型/裁判偏差，未执行真实工具。
  1. `llm_judge`（兼容旧模式，需 API）：LLM 判断"该版本规则是否支持得出 expected_decision"；
  2. `heuristic`（离线）：规则文本与期望决策的关键词/短语重叠 —— **启发式，非语义理解**，报告中明确标注局限。
- 输出 `EvalReport`（score_a / score_b / per-case 明细）落库；比较结论遵循"v2 不劣于 v1 且修复被纠错问题"的验收口径。
- 评估与 GEPA 复用批次校验和裁判输出校验：先校验全部案例并扫描敏感信息，
  再请求 LLM；裁判结果要求 JSON 布尔值和非空判断 / 理由，非法输出拒绝保存报告/候选版。
- 方法局限始终写入报告，自定义备注仅追加；旧裁判能看到期望答案，盲测则先保存独立预测；二者
  不等同于独立执行成功率。案例身份由 `situation-v1:` + SHA-256 表示：对 situation 做
  Unicode NFKC、casefold 与空白折叠，不包含 expected_decision 或案例 ID。评估/优化批内
  重复情境会报错；评估另检查两个版本 `derived_from.optimization_cases` 的并集。
  检查在模型调用前完成；逐例报告保存 `situation_fingerprint` 便于核对。
  指纹只能发现规范化文本相同的情境，不能识别同义改写或示范/纠错的语义重叠，
  旧版本未记录的调优案例无法补回。报告明确携带这些局限；调用者仍需独立准备评估集。

## 7. MCP 接口（`mcp_server.py`，MCPServer）

工具面（22 个，全部纯编排调用引擎层、存储层或适配器层）：

| 工具 | 引擎调用 |
|---|---|
| `teachonce_import_demonstration` | learning.import_demonstration |
| `teachonce_list_demonstrations` | store.list_demonstrations（重启后恢复示范 ID） |
| `teachonce_generate_skill` | learning.extract_candidate_skill |
| `teachonce_learning_context` / `teachonce_revision_context` / `teachonce_matching_context` | 返回真实证据与宿主推理要求，不请求 API |
| `teachonce_list_skills` / `teachonce_get_skill` | store |
| `teachonce_match_skill` | matching.match |
| `teachonce_record_correction` | correction.record_correction |
| `teachonce_list_corrections` | store.list_corrections（支持 open / applied 过滤） |
| `teachonce_apply_correction` | correction.apply_correction |
| `teachonce_approve_version` / `teachonce_rollback` | correction 审批/回滚 |
| `teachonce_review_version` | 实际差异、资格与审批历史 |
| `teachonce_record_outcome` / `teachonce_list_outcomes` | feedback 记录及恢复未独立验证的宿主报告 |
| `teachonce_evaluate_versions` | evaluation.evaluate |
| `teachonce_list_evaluations` | store.list_eval_reports（包含方法局限和逐例结论） |
| `teachonce_export_skill_md` | skill_export |
| `teachonce_optimize_skill` | adapters.gepa_optimizer（可选 `[gepa]` extra；产物恒为 candidate） |
| `teachonce_import_gui_recording` | adapters.openadapt（需 `TEACHCONE_GUI_OBSERVATION=1`） |

传输：stdio（默认）。返回值一律为 JSON-able dict；错误以异常抛出（MCP 会转为工具错误），不吞错。
历史查询为本地只读操作，不初始化 LLM。重启后只要连接同一数据库即可恢复；查询结果返回给宿主 Agent。

## 8. Agent Skills 导出（`skill_export.py`）

输出标准 `SKILL.md`（YAML frontmatter: `name` / `description` + Markdown 正文：适用条件 / 决策规则 / 异常处理 / 反面模式 / 版本与来源）。
frontmatter 手写序列化（避免引入 PyYAML）；保证其他兼容 Agent 可直接导入。

## 9. 适配器层（已实现 2 个，可选依赖）

第三方项目一律经适配器隔离接入：**核心引擎不 import 适配器**，只有 `mcp_server.py` 编排层懒加载调用。
第三方包是可选 extra（`pip install "teachonce[gepa]"`），缺失时报 `AdapterUnavailableError` 并给出确切安装命令——核心库与 CI 不需要它们。

| 适配器 | 状态 | 设计 |
|---|---|---|
| `adapters/gepa_optimizer.py` | ✅ 已实现 | GEPA `optimize_anything` 根据调优案例进化规则文本；判分复用自有 LLM judge（`evaluation.py`）；反思模型用 `_ReflectionLM` 包装自有 `OpenAICompatibleLLM`（实现 GEPA `LanguageModel` 协议，不引入 litellm）；优化后由 LLM 重构回结构化 schema 存为 **candidate**；预算（`max_metric_calls`）显式且必须为正整数；changelog 记录来源与预算，`derived_from` 累计保存优化来源及调优情境指纹。沿用 JSON 字段，无数据库迁移；`best_score` 仅为调优分数 |
| `adapters/openadapt.py` | 仅预处理事件 JSON | 显式转换的 actions/events JSON → `Demonstration`，含敏感信息扫描；调用时检查 GUI 授权，新环境变量优先；不启动录制、不挂钩输入设备；低信号事件跳过并在 context 中计数。原生 OpenAdapt SQLite/视频目录与 CaptureSession API 尚未接入，JSON 字段别名不是上游标准承诺 |

实时屏幕观察（不经导出文件的直接捕获）仍未实现，属计划中，需另行显式授权设计。

## 10. 安全设计

| 威胁 | 对策 |
|---|---|
| 示范中夹带密码/Token | 入库前正则红灯扫描（`SensitiveContentError`） |
| Skill 自动执行危险操作 | TeachOnce 不直接操作用户文件系统；只输出计划，执行在宿主 Agent 且需授权 |
| 错误经验固化 | 更新一律为 candidate → 人工 approve；结构化 changelog；指针回滚 |
| 评估造假 | EvalReport 记录 method 字段；heuristic 模式报告内强制标注局限 |
| 数据泄露 | SQLite 本地默认、无遥测；上下文返回宿主，API 推理发送至配置端点，导入前须脱敏 |

`feedback.py` 只处理宿主报告和显式反馈，不执行任务、不自动批准学习。
`cli.py` 是编排入口；Skill 安装/导出只写用户请求的目录，拒绝覆盖既有 Skill。
包内操作 Skill 为 `src/teachonce/skills/teachonce/SKILL.md`，是指导使用工具的工作流，
与用户学到的个人 Skill 分开。包内 demo/scenario 是交互示例；顶层 demo 模块保留兼容入口。

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
- 覆盖：模型序列化、事务及并发冲突、学习与宿主提案、敏感内容错误路径、匹配风险、纠错/证据审批/回滚、三种评估方法、导出格式、执行报告、MCP stdio、CLI 与 Demo。
- 真实 LLM 手工验收：`python -m demo.run_demo`（需 Key；输出即真实学习结果，不做装饰性美化）。
