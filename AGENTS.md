# AGENTS.md — TeachOnce AI 开发规范

> 本文件是所有 AI 开发助手（Codex / Claude / WorkBuddy / 其他）在本仓库工作的**强制规范**。
> **每次开始开发前，必须先完整阅读本文件与 `docs/PRODUCT.md`，确保产品方向一致。**
> 若两者与用户最新指令冲突，以用户最新指令为准，并同步更新这两份文档。

## 0. 开工前检查清单（每次会话必做）

1. 读 `AGENTS.md`（本文件）+ `docs/PRODUCT.md` — 确认产品定位没有漂移。
2. 读 `docs/ARCHITECTURE.md` — 确认模块边界与技术决策。
3. 检查 `git status` 与当前分支 — **禁止直接提交到 `main`**。
4. 明确本次任务属于「已实现功能的修改」还是「计划中功能的新建」——若是后者，先确认在 MVP 范围内。

## 1. 项目定位（不可漂移）

TeachOnce = **Agent Skill + MCP Server**，学习个人工作经验的引擎。

- ✅ 做：个人决策经验学习、跨任务经验迁移、纠错驱动的持续进化、Skill 标准化与跨 Agent 复用。
- ❌ 不做：独立桌面 AI 应用、通用 AI Agent、通用 RPA/屏幕录制工具、通用文件管理器。
- 成功标准不是"生成一个 SKILL.md"，而是：**人类教几次 → AI 学会判断原则 → 新任务自主正确判断 → 纠错后越用越好**。

## 2. 架构与模块职责（详见 docs/ARCHITECTURE.md）

| 模块 | 职责 | 禁止 |
|---|---|---|
| `models.py` | 纯数据模型，无 IO 无副作用 | 不引入 LLM / 数据库调用 |
| `store.py` | SQLite 持久化，版本 append-only | 不写业务逻辑 |
| `llm.py` | LLM Provider 抽象 + OpenAI 兼容实现 + ScriptedLLM 测试替身 | 业务模块不得绕过此层直连 HTTP |
| `learning.py` | 示范导入 → Skill 提取 | 不写数据库 SQL，不经 store 以外的持久化 |
| `matching.py` | 任务 ↔ Skill 匹配 + 理由 | 不自动执行任务，只输出匹配结论与计划 |
| `correction.py` | 纠错 → 新版本 + changelog + 审批/回滚 | 不允许覆盖历史版本 |
| `evaluation.py` | 未见案例的版本对比 | 必须如实标注评估方法及其局限 |
| `skill_export.py` | Agent Skills 标准导出 | 不发明非标准格式 |
| `mcp_server.py` | MCP 工具面 | 工具函数只做编排，不含业务逻辑 |
| `adapters/` | 第三方项目隔离接入（GEPA 优化、OpenAdapt 录制导入） | 可选 extra、懒加载、报错给安装命令；GUI 能力调用时检查显式授权 |

依赖方向：`mcp_server → {learning, matching, correction, evaluation, skill_export, adapters(懒加载)} → {store, llm} → models`。
**不允许反向依赖或循环依赖**；核心引擎不感知任何适配器。

## 3. 代码规范

- Python **3.12+**，全量类型注解（`from __future__ import annotations`）。
- 格式化与 lint 一律以 `ruff check . && ruff format .` 为准（配置见 `pyproject.toml`），提交前必须通过。
- 数据模型用 `dataclass`；对外边界（MCP 工具、导入导出）用普通 `dict`/JSON。
- 错误处理：可预期错误定义专用异常（如 `LLMNotConfiguredError`、`SchemaValidationError`、`SensitiveContentError`），错误信息必须对用户可操作（说清"怎么修"）。
- 注释和文档字符串用中文或英文均可，但同一文件内保持一致；公开 API 必须有 docstring。
- 不引入新依赖前先确认：标准库/已有依赖能否解决？能则不加。新依赖需在 PR 描述中说明理由。
- 保持轻量、模块化；单文件超过 ~500 行时考虑拆分。

## 4. 测试规范

- 框架：`pytest`，测试放 `tests/`，命名 `test_*.py`。
- **测试中绝不调用真实 LLM API**：一切 LLM 交互通过 `ScriptedLLM`（预置响应的测试替身）注入。
  - `ScriptedLLM` 是**测试道具**，仅用于验证管道逻辑（数据流/校验/版本管理/错误路径）；
  - 生产代码路径**永远真实调用** API 或明确报错，禁止任何形式的"硬编码伪装成功"。
- 每个公开函数至少覆盖：正常路径 + 至少一个错误路径。
- 涉及版本管理的改动，必须测：新版本不覆盖旧版本、审批、回滚。
- 涉及 prompt 改动的 PR，必须在 `tests/` 中更新对应 ScriptedLLM 剧本并跑通全量测试。
- CI（GitHub Actions）必须全绿；CI 只跑 `ruff` + `pytest`，不接触任何 API Key。

## 5. 安全红线（违反即打回）

1. **不记录敏感信息**：密码、Token、Cookie、私钥等一旦在示范/纠错文本中被识别，必须拦截（`SensitiveContentError`）并提示用户移除。
2. **不自动执行危险操作**：TeachOnce 自身不对真实文件系统 / 系统做写操作。Skill 只输出判断与行动计划；执行交宿主 Agent 且需用户授权。
3. **GUI 录制需显式授权**：屏幕/操作录制能力（未来阶段）默认关闭，必须用户显式开启。
4. **数据本地优先**：个人学习记录默认仅存本地 SQLite（`~/.teachonce/`），不默认上传、不默认公开。
5. **更新可解释、可审批、可回滚**：任何 Skill 更新必须带结构化 changelog；未审批的版本不得成为 active 版本。
6. **诚实原则**：不允许虚构模型表现、任务成功率或测试数据；文档必须区分「已实现」与「计划中」；评估报告必须标注方法与局限。

## 6. Git 与提交规范

- **分支策略**：`main` 受保护。功能开发一律独立分支：`feat/xxx`、`fix/xxx`、`docs/xxx`、`chore/xxx`；通过 Pull Request 合入，禁止本地直推 `main`。
- Commit message：`<type>: <简短英文或中文摘要>`（type ∈ feat/fix/docs/test/chore/refactor），正文说明动机与影响。
- 一个 commit 只做一件事；生成物/本地数据（`.teachonce/`、`*.db`）绝不入库。
- PR 描述必须包含：改动内容、测试方式与结果、对产品方向的影响、未完成/已知问题。

## 7. 产品方向变更流程

任何影响下列内容的改动，必须在同一 PR 中更新 `docs/PRODUCT.md`：
- 目标用户 / 核心场景 / 成功标准；
- 「已实现 / 计划中」功能边界；
- 安全与隐私承诺。

架构级变更（新增模块、改变依赖方向、引入新外部系统）必须同步更新 `docs/ARCHITECTURE.md`。

## 8. 复用与集成原则

- 优先复用成熟开源能力（OpenAdapt、Hermes、MCP SDK、Agent Skills 标准等），但：
  - 使用前必须核查其 API、维护状态、许可证（必须与 MIT 兼容）；
  - 一律通过**适配器/模块化接口**接入（见 `docs/ARCHITECTURE.md` 的适配器层），禁止与任一 Agent 深度耦合；
  - 未实际集成的项目，**禁止在文档中宣称已集成**。
- 我们自主开发的核心（不可外包给依赖）：个人经验学习、决策规则迁移、面向新任务的持续改进。
