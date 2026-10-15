# Codex 接入与首轮使用

## 安装与诊断

Python 3.12+，在本仓库目录运行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\teachonce doctor
.venv\Scripts\teachonce probe
.venv\Scripts\teachonce install-skill --dest .agents/skills
```

`doctor` 仅检查本地配置，不发送模型请求，也不证明 Key 有效。
`probe` 启动真实 stdio 子进程并读取工具目录，使用临时数据库，不触碰个人学习记录。
`install-skill` 写入 TeachOnce 操作指引，已有目录会拒绝覆盖；它与学到的个人 Skill 是不同的产物。

## 注册 MCP

用 `teachonce codex-config` 生成与当前 Python 环境对应的 TOML。
将内容添加到 `~/.codex/config.toml`，或受信任项目的 `.codex/config.toml`。
不要覆盖已有的其他配置或重复创建 `[mcp_servers.teachonce]`。
也可先用官方 CLI 注册本地命令：

```powershell
codex mcp add teachonce -- C:\absolute\path\to\.venv\Scripts\python.exe -m teachonce.mcp_server
codex mcp list
```

生成配置使用环境变量名转发，不包含凭据值。审批和回滚的单独工具策略为 `prompt`，
因此模型提交确认字段仍须经过宿主授权。不同主机的 Python 路径不同，请在实际运行机器上生成。
GUI 环境变量只转发已存在的值，不默认启用。

MCP 配置方式依据 [官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。
仓库 `.agents/skills` 的发现规则依据 [官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills)。
新配置/Skill 若未显示，重新打开 Codex 后再检查工具列表。

## 不额外配置 API Key 的宿主流程

在 Codex 中调用 `$teachonce`，例如：

> 我教你整理课程资料。先读取 TeachOnce 指引，把下面的示范保存下来。
> 根据示范提取判断原则，不要编造偏好。生成候选后给我看规则和不足，暂时不要批准。

1. `teachonce_import_demonstration` 保存已脱敏示范。
2. `teachonce_learning_context` 返回真实证据、提取要求和 JSON 格式；Codex 用当前模型归纳。
3. 把归纳结果作为明确的 `proposal` 交给 `teachonce_generate_skill`。
   来源必须使用真实示范 ID；服务验证并记录 `host-proposal`，不冒充自行推理。
4. 通过 `teachonce_review_version` 查看候选。独立准备评估案例，禁止把 GEPA 调优案例拿来评分。
5. 不配额外模型时，可使用 `heuristic` 检查规则文本与管道，但它不验证语义正确性。
   必须人工核查规则和逐例结果；需要独立语义回归时配置下述评估 Provider。
6. 用户批准具体版本和报告后，提交 `evaluation_id`、`confirmed=true`、`reviewer`、`reason`。
   初版报告比较 v1/v1；修订报告比较当前 active/候选。候选须通过全部审阅案例。
7. `teachonce_matching_context` 返回当前批准的规则。Codex 决策后把 `matched_version` 和计划
   放入明确的匹配 `proposal`，服务检查版本、置信值、缺失信息和风险，再返回建议。
8. Codex 用自己的工具执行，遵守当前任务授权。完成后用 `teachonce_record_outcome` 报告
   succeeded/failed/partial/cancelled。它是宿主报告，不是服务端独立验证。
9. 自然语言纠错可带明确的 `classification`（correction_type、analysis），不配 API 也能保存。
   读取 `teachonce_revision_context` 后生成修订 `proposal`，提交时明确绑定返回的
   `base_version` 和 `correction_ids`，防止误消费后来新增的反馈。

提案路径不提供默认答案；没有提案且未配 Provider 时仍明确报错。
不是所有 Host 都能遵循工具指引或审阅策略，需在具体宿主中验收。

## 可选独立 API 推理与盲测

在本机设置 `TEACHONCE_LLM_API_KEY`、`TEACHONCE_LLM_BASE_URL`、`TEACHONCE_LLM_MODEL`，
让启动 Codex 的进程可以读取它们。旧 `TEACHCONE_*` 拼写继续兼容，新拼写优先。
不要把 Key 写进仓库、示范、反馈或聊天。Codex 登录凭据与该 Python Provider 分开。

配置后，省略 proposal 的学习/匹配/纠错会实际调用 API。推荐 `llm_blind`：
先仅根据规则和情境输出预测，记录后再用另一次调用比较期望答案；两版本每例最多四次调用。
`llm_judge` 保留兼容；`heuristic` 只用于文本/管道检查。API 内容会送往所配置服务，
请自行选择适合数据隐私要求的 Provider；localhost HTTP 可用于本地模型。

## 恢复、回滚与导出

重启后使用相同 `TEACHONCE_HOME` 即可恢复。list_demonstrations / list_corrections /
list_evaluations / list_outcomes 均为本地只读，不重新请求模型。
审批与回滚事件可通过 review_version 查询，包含报告 ID、审阅者、理由和时间。
rolled_back 表示曾被批准的版本，可以再次显式恢复。
导出默认使用已批准 active 版本；显式导出候选会标注 REVIEW ONLY，不能当作已授权 Skill。

TeachOnce 不操作真实用户文件；计划执行、文件权限和危险动作确认仍由 Codex 管理。
