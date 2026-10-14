# TeachOnce

> **Teach your AI once. Let it learn how you work.**
> 教你的 AI 一次，让它学会你的工作方式。

TeachOnce 是一个从人类**实际操作、任务示范和纠错反馈**中持续学习的「AI 工作经验引擎」。

它不是工作流录制器。普通录制只能知道用户**做了什么**；TeachOnce 的目标是学会用户**为什么这样做**——在什么情况下采用哪种方法、如何判断例外、如何处理没见过的情况——并把这些经验变成可复用、可迁移、可纠正、可回滚的 **Agent Skill**，通过 **MCP** 提供给任何兼容的 Agent。

## 为什么需要它

| 现有做法 | 问题 | TeachOnce 的做法 |
|---|---|---|
| 屏幕录制 / RPA | 机械回放固定操作序列，任务一变就失效 | 从多个示范中提取**决策规则**（条件 → 做法 → 理由），适应任务变化 |
| 手工写 prompt / 规则 | 写的人说不全部自己的隐性经验 | 从真实操作和**自然语言纠错**中反向提取经验 |
| 一次性教学 | 错了只能重教 | 纠错转化为**新版本 Skill**，保留历史、可审批、可回滚 |
| 绑定某个 Agent | 换个工具就要重教 | 导出为标准 **Agent Skills**，经 **MCP** 跨 Agent 复用 |

## 核心能力（MVP 范围）

1. **示范导入** — 记录一次任务的结构化示范：目标、操作步骤、关键判断、结果。
2. **Skill 提取** — 从多次示范中提取可迁移的：适用条件 / 决策规则 / 异常处理 / 反面模式。
3. **任务匹配** — 新任务到来时判断是否适用某个 Skill，并**说明匹配理由**；把握不足时要求人工确认而不是硬套。
4. **纠错更新** — 用户用自然语言指出错误后，生成改进版 Skill（含结构化变更说明），保留全部历史版本。
5. **版本评估** — 用**未见过的案例**对比新旧版本是否更可靠，评估结果落库、可查。
6. **MCP 接口** — 学习、匹配、执行准备、更新能力全部通过 MCP 工具暴露。
7. **标准化导出** — 将 Skill 导出为 Agent Skills 标准的 `SKILL.md`，可被其他兼容 Agent 直接使用。

## 快速开始

### 1. 安装

```bash
git clone https://github.com/z2576632-collab/teachonce.git
cd teachonce
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. 配置 LLM（学习引擎需要）

TeachOnce 的学习引擎需要调用一个 **OpenAI 兼容**的 LLM API（支持 DeepSeek / OpenAI / 本地 Ollama 等）：

```bash
# 以 DeepSeek 为例
export TEACHCONE_LLM_API_KEY="sk-..."                       # Windows 用 set / $env:
export TEACHCONE_LLM_BASE_URL="https://api.deepseek.com/v1" # 可选，默认 OpenAI
export TEACHCONE_LLM_MODEL="deepseek-chat"                  # 可选，默认 gpt-4o-mini
```

未配置 API Key 时，学习类操作会**明确报错**并提示配置——不会伪装成功。

### 3. 跑端到端 Demo（「个人课程资料整理」场景）

```bash
python -m demo.run_demo
```

Demo 剧本：人类示范 3 次整理课程资料 → AI 提取 Skill → 遇到新任务自主判断 → 用户纠错 → 生成 v2 → 用未见过的案例验证 v2 是否比 v1 更可靠。

### 4. 作为 MCP Server 接入任意 Agent

```bash
teachonce-mcp   # 或 python -m teachonce.mcp_server
```

在支持 MCP 的 Agent（WorkBuddy / Claude Code / Cursor / 其他）中配置 stdio server 即可。
配置示例（写入 Agent 的 MCP 配置文件，如 WorkBuddy 的 `~/.workbuddy/mcp.json`、
Claude Code 的 `~/.claude.json`，用你实际环境的 Python 绝对路径替换 `command`）：

```json
{
  "mcpServers": {
    "teachonce": {
      "command": "/absolute/path/to/python",
      "args": ["-m", "teachonce.mcp_server"],
      "env": {
        "TEACHCONE_LLM_API_KEY": "sk-...",
        "TEACHCONE_LLM_BASE_URL": "https://api.deepseek.com/v1",
        "TEACHCONE_LLM_MODEL": "deepseek-chat"
      }
    }
  }
}
```

前提是该 Python 环境已 `pip install -e` 本仓库。不配 `env` 也能启动：
历史查询类工具不需要 LLM，学习类工具会在调用时明确提示缺 Key。

暴露的工具：

| MCP 工具 | 作用 |
|---|---|
| `teachonce_import_demonstration` | 导入一次任务示范 |
| `teachonce_list_demonstrations` | 找回已保存的示范和 ID，继续生成 Skill |
| `teachonce_generate_skill` | 从示范生成候选 Skill |
| `teachonce_match_skill` | 为新任务匹配 Skill 并说明理由 |
| `teachonce_get_skill` / `teachonce_list_skills` | 查看 Skill 与其全部版本 |
| `teachonce_record_correction` | 记录自然语言纠错 |
| `teachonce_list_corrections` | 查看待处理纠错或全部纠错历史 |
| `teachonce_apply_correction` | 生成改进版 Skill（保留历史） |
| `teachonce_approve_version` / `teachonce_rollback` | 审批新版本 / 回滚到历史版本 |
| `teachonce_evaluate_versions` | 用未见案例对比两个版本 |
| `teachonce_list_evaluations` | 读取历史评估的逐例结论与方法局限 |
| `teachonce_export_skill_md` | 导出 Agent Skills 标准 SKILL.md |

### 5. 换一个聊天后继续学习

只要 MCP Server 仍使用同一个数据库（默认 `~/.teachonce/teachonce.db`），
新聊天或服务重启后都可以从保存的记录继续：

1. 调用 `teachonce_list_demonstrations` 找回示范 ID，再用 `teachonce_generate_skill` 提取经验。
2. 调用 `teachonce_list_skills` 找到 Skill，用 `teachonce_list_corrections(skill_id, status="open")` 查看待处理反馈。
3. 调用 `teachonce_apply_correction` 生成候选版；已有规则 ID 与前几轮纠错来源会保留。
4. 用未见案例评估候选版；之后可随时用 `teachonce_list_evaluations(skill_id)` 重新读取报告。
5. 人工审查规则、变更说明和逐例评估结果后，调用 `teachonce_approve_version`。

这三个查询工具不需要 API Key，也不会重新请求模型。读取记录会将内容返回给调用它的宿主 Agent。
纠错只能用于所属 Skill，已应用的纠错不能重复消费；若需继续调整，请记录新反馈。
并发请求或写入失败不会留下“新版本已生成、纠错却没标记”的半完成状态。

评估仅接受真正的 JSON 布尔值 `true` / `false`；字符串 `"false"` 等无效输出会报错，
不会生成评估分数。报告始终保留方法局限，自定义备注不能覆盖它们。

## 项目结构

```
teachonce/
├── AGENTS.md              # AI 开发规范（每次开发前必读）
├── docs/
│   ├── PRODUCT.md         # 产品定位与方向（每次开发前必读）
│   └── ARCHITECTURE.md    # 架构设计与技术决策
├── src/teachonce/
│   ├── models.py          # 数据模型
│   ├── store.py           # SQLite 存储层（本地优先）
│   ├── llm.py             # LLM Provider 抽象（真实 API + 测试替身）
│   ├── learning.py        # 学习引擎：示范 → Skill
│   ├── matching.py        # 匹配引擎：任务 ↔ Skill
│   ├── correction.py      # 纠错引擎：反馈 → 新版本
│   ├── evaluation.py      # 评估引擎：未见案例对比
│   ├── skill_export.py    # Agent Skills 标准导出
│   └── mcp_server.py      # MCP Server
├── demo/                  # 「课程资料整理」端到端 Demo
└── tests/                 # pytest 测试套件
```

## 当前状态

**已实现（v0.1.0 MVP）**
- ✅ 结构化示范导入（JSON schema 校验 + 敏感信息红灯检查）
- ✅ 多示范 → Skill 提取（LLM + JSON schema 校验 + 证据标注）
- ✅ 任务匹配（置信度 + 匹配理由 + 低置信度转人工确认）
- ✅ 自然语言纠错 → 版本化更新（changelog / 审批 / 回滚）
- ✅ 未见案例版本评估（LLM 裁判 + 离线启发式两种模式，均如实标注方法论）
- ✅ MCP Server（14 个工具，含示范 / 纠错 / 评估历史查询）+ Agent Skills `SKILL.md` 导出
- ✅ 多轮纠错保留规则标识和累计证据，版本与纠错状态原子保存
- ✅ SQLite 本地存储，数据默认不出本机
- ✅ pytest 全套测试 + ruff + GitHub Actions CI

**计划中（未实现，勿在文档中宣称可用）**
- ⬜ GUI 操作观察 / 屏幕录制接入（计划通过 OpenAdapt 适配器，需用户显式授权）
- ⬜ 跨任务自动触发（后台 watcher，当前由宿主 Agent 显式调用 MCP 工具）
- ⬜ Hermes Agent Skill 格式双向同步
- ⬜ DSPy / GEPA 驱动的规则自动优化
- ⬜ 多用户 / 团队 Skill 共享

## 安全与隐私原则

- 个人操作记录**默认仅本地保存**（SQLite，位于 `~/.teachonce/`），不默认上传或公开。
- 导入示范时对疑似密码 / Token / Cookie 等内容做**红灯警告**，拒绝入库。
- 不对真实文件系统执行任何自动操作；Skill 产出「判断 + 行动计划」，执行权始终在宿主 Agent 和用户手中。
- Skill 更新带结构化变更说明，**人工审批**后生效，支持一键回滚。
- 不允许通过硬编码输出伪装成功；已实现与计划中功能严格区分。

## 开发

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```

**每次开发前请先阅读 [`AGENTS.md`](AGENTS.md) 与 [`docs/PRODUCT.md`](docs/PRODUCT.md)。**

## License

[MIT](LICENSE)
