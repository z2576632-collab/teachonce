# TeachOnce

> **Teach your AI once. Let it learn how you work.**
> 教你的 AI 一次，让它学会你的工作方式。

TeachOnce 是学习个人工作经验的 **Agent Skill + MCP Server**。它从任务示范和纠错中
提取适用条件、判断原则和异常处理方式，供已有 Agent 在新任务中复用。

当前版本 **0.4.0，受控试用阶段**。学习、匹配、反馈、评估、人工审阅、审批、回滚和
Skill 导出都有实际实现。TeachOnce 输出建议，真实任务执行由宿主 Agent 在用户权限内完成。
尚未证明开放领域任务的成功率；测试通过不等同于模型效果。

## 在 Codex 中使用

需要 Python 3.12+。在仓库目录运行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\teachonce doctor
.venv\Scripts\teachonce probe
.venv\Scripts\teachonce install-skill --dest .agents/skills
.venv\Scripts\teachonce codex-config
```

把最后一个命令生成的内容添加到 Codex MCP 配置，再在 Codex 中使用 `$teachonce`。
配置只包含 Python 路径和环境变量名，不写入 API Key；审批和回滚工具要求宿主弹出授权。
完整步骤见 [Codex 接入](docs/CODEX.md)。

**无需额外 API Key 的宿主模式**：Codex 读取学习上下文，用自身模型生成明确的
`proposal`，TeachOnce 校验来源、数据结构并保存为候选。服务不会冒充自己调用了模型。
可使用同一路径提交匹配、纠错分类和修订提案。

**可选 API 模式**：配置 OpenAI 兼容 Provider 后，TeachOnce 自行调用模型学习、
匹配、盲测或 GEPA 优化。新环境变量使用 `TEACHONCE_*`；旧拼写 `TEACHCONE_*`
继续兼容。远程端点要求 HTTPS；本地端点允许 HTTP。

```powershell
$env:TEACHONCE_LLM_API_KEY = "<在本机设置，不提交到仓库>"
$env:TEACHONCE_LLM_BASE_URL = "https://api.deepseek.com/v1"
$env:TEACHONCE_LLM_MODEL = "deepseek-chat"
```

Codex 账户登录不等于给独立 Python 服务提供 API 凭据。诊断命令不会请求模型或验证 Key 的有效性。

## 学习闭环

1. 导入真实且已脱敏的示范，保留目标、操作、关键判断、理由和结果。
2. 综合示范生成 candidate，检查证据和未覆盖问题。
3. 准备独立案例。推荐 `llm_blind`：先看规则与情境作决策，再由单独调用比较期望答案。
4. 用 `teachonce_review_version` 查看实际差异与逐例评估；取得用户对该版本的明确批准后启用。
5. 匹配新任务并解释计划；风险说明、缺失信息、低置信或空计划要求确认。
6. 宿主执行后用 `teachonce_record_outcome` 保存实际报告。显式反馈成为 open correction，
   不自动改写规则、不自动批准版本。
7. 修订、再次独立评估、人工批准；出现问题可恢复以前批准的版本。

审批绑定已保存的评估 ID，候选必须通过全部审阅案例；比较基准必须为当前 active 版本。
状态、active 指针和审计事件在同一 SQLite 事务中保存。确认字段是宿主授权记录，
不验证用户身份；宿主必须实际取得人类许可。旧版本不会被覆盖。

## 命令行与 Demo

```powershell
teachonce --db local.db import examples/course-demonstration.json
teachonce --db local.db list
teachonce --db local.db review <skill-id> <version>
teachonce --db local.db approve <skill-id> <version> --evaluation <report-id> --reviewer "<姓名>" --reason "<审阅理由>" --yes
teachonce --db local.db rollback <skill-id> <version> --reviewer "<姓名>" --reason "<恢复理由>" --yes
teachonce --db local.db export <skill-id> --output learned-skills
teachonce demo
```

`teachonce demo` 已包含在安装包中，使用真实配置的 API Provider。没有配置时明确报错。
它先评估再交互审阅；不通过、非交互环境或用户拒绝时保留候选，不宣称全部完成。
兼容入口 `python -m demo.run_demo` 仍可在仓库中使用。
示范是合成输入，规则由运行时模型生成，不预置成功结果。

## MCP 工具（22 个）

| 用途 | 工具 |
|---|---|
| 示范与学习 | `teachonce_import_demonstration`、`teachonce_list_demonstrations`、`teachonce_learning_context`、`teachonce_generate_skill` |
| 任务迁移 | `teachonce_matching_context`、`teachonce_match_skill` |
| 纠错 | `teachonce_record_correction`、`teachonce_list_corrections`、`teachonce_revision_context`、`teachonce_apply_correction` |
| 历史与审阅 | `teachonce_list_skills`、`teachonce_get_skill`、`teachonce_review_version` |
| 评估 | `teachonce_evaluate_versions`、`teachonce_list_evaluations` |
| 审批/恢复 | `teachonce_approve_version`、`teachonce_rollback` |
| 执行反馈 | `teachonce_record_outcome`、`teachonce_list_outcomes` |
| 可移植输出 | `teachonce_export_skill_md` |
| 可选适配器 | `teachonce_optimize_skill`、`teachonce_import_gui_recording` |

## 评估与可选集成

- `llm_blind` 是配置 Provider 时的默认评估模式，决策阶段不接收期望答案；
  仍存在模型和裁判偏差，不能当作真实工具执行成功率。
- `llm_judge` 保留兼容，裁判可看到期望答案，只衡量规则支持性。
- `heuristic` 仅做关键词重叠检查；无 Provider 的 `auto` 使用它并明确标注方法。
  宿主模式也可以用它检查管道，但独立语义验收仍需人类或配置的评估 Provider。
- GEPA：`pip install "teachonce[gepa]"`。调优案例参与学习，`best_score` 标为 tuning。
  独立评估拒绝重复案例或已记录的调优情境；多轮优化、纠错与重启保留指纹。
  同义改写、示范/纠错语义重叠和旧记录缺失仍需人工识别。
- GUI 事件：只导入用户已授权、显式转换的事件 JSON，默认关闭；OpenAdapt 原生 SQLite/视频目录尚不支持；
  `TEACHONCE_GUI_OBSERVATION=1` 明确开启导入，不启动录制。实时屏幕观察未实现。
- Hermes/DSPy 的既有评估结论与复用边界见 [产品文档](docs/PRODUCT.md)，并未宣称新增集成。

## 隐私与维护

个人记录默认存入 `~/.teachonce/teachonce.db`，可用 `TEACHONCE_HOME` 指定目录。
无遥测。上下文会返回给宿主；启用 API Provider 时推理内容会发往所配置端点，
本地保存不意味着完全本地计算。敏感信息扫描是补充防线，请先脱敏示范和反馈。

```powershell
python -m pytest
ruff check .
ruff format --check .
```

CI 验证 Linux/Windows、Python 3.12/3.13、真实 stdio 子进程及独立 wheel 安装。
测试使用 ScriptedLLM 或明确的测试提案，不请求真实模型 API。

[产品约定](docs/PRODUCT.md) · [架构](docs/ARCHITECTURE.md) ·
[试用验收与边界](docs/READINESS.md) · [开发规范](AGENTS.md)

MIT License.
