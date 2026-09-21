# 稳伴 / Steady Companion

[English](README.md)

开源 AI 陪伴框架：用户可控记忆、实验性互动范围控制，可运行 Agent 与可复用 Skill。

**研究原型 / experimental：Agent 0.6.0.dev16、Skill 0.8.0-dev16。** 目标是持续陪伴、稳定的相处特点及用户掌控的连续性；自然度、心理学支持与实际帮助效果仍需验证。不是临床专业人员、治疗或实时监护服务。

## 从源码开始

需要 Python **3.10 或更高版本**，运行时没有第三方依赖。在本目录执行：

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
python3 -m steady_companion inspect --profile daily-deepseek --connection ./configs/deepseek-official.json
python3 -m steady_companion demo
```

inspect 不联网、不读取凭据；demo 使用临时合成数据，不调用模型。模拟输出不是模型效果。也可安装当前 wheel：`python3 -m pip install /path/to/steady_companion_agent-0.6.0.dev16-py3-none-any.whl`。

由你主动启动的**收费聊天**：

```sh
python3 -m steady_companion chat --profile daily-deepseek --connection ./configs/deepseek-official.json --max-calls 4
```

密钥在终端隐藏输入，仅当前进程使用，也可通过独立环境变量 STEADY_DEEPSEEK_API_KEY 提供；不要写进配置或发到 issue。输入消息后才发请求。4 次是整个会话请求上限，不保证 4 轮；已启用的辅助和显式 /learn 同样计费。没有自动扩额、重试、模型切换或启动探测。

非秘密配置已包含官方地址、deepseek-flash 别名、关闭思考、4096/90 宿主上限。普通主回复一次生成，无恢复；结构化辅助用 json_object 加宿主校验，不发送 store/json_schema/采样字段。路由别名不永久锁定权重，不发送 store 不保证供应商不留存数据。本轮仅离线验证，不宣称真实接通或效果改善。

competition-nebius 与无 profile 的旧入口仍为独立 Nebius＋NVIDIA checked 路径，使用 NEBIUS_API_KEY；Start.command 明确保持 Nebius。两个入口的运行策略不同。公开仓库不代表取得比赛资格。详见 [RUN](RUN.md)。

## dev16 改动边界

日常配置使用版本化 companion-v3 生成指导与新合成多轮对照，core/R-A/safety 不变。session 控制新增与主题词表无关的文字任务引用、个人经历局部边界及复合暂停；仍是有限规则，无法识别时明确 unknown。助手提案和用户采纳只建立本次请求的来源引用，不转为用户事实或新增记忆权限。程序行为、提示指导和待真实验证的能力分开列于 [dev16 说明](docs/DEV16.md)。

## 六轮连续体验（可选）

`eval-experience`（dev16 新情境）：固定 session / selective，新临时 manual 合成库，最多 6 轮 / 12 请求；默认离线预览，显式 `--confirm-live` 才收费。逐轮确认后续脚本是否接得上实际前答，失败或不适用即停。本地报告不自动上传。参见 [一次启动命令、脚本和费用未知项](docs/CONTINUOUS_EXPERIENCE.md)。

## Agent、Skill 与开关

Agent 执行存储、权限、预算、上下文和交付检查；[Skill](steady_companion/skill/SKILL.md) 仅提供可复用指导，单独安装不会获得宿主状态机、持久记忆或预算保证。Skill ZIP 解压到所用宿主支持的 skills 目录。

**互动控制和窄检查默认关闭。** 明确试用时，在上面的聊天命令后添加 `--interaction-control session --interaction-check selective`。触发窄检查最多增加 1 次请求，计入同一预算；冲突、未知或失败不提交草稿。all 模式逐轮检查会增加费用，不是默认。有限中文规则仍可能漏判、误触发，不能保证理解所有表达。状态仅本次会话有效，不形成心理画像。

## 记忆与删除

新用户 manual；`/remember preference ...` 显式保存，`/memory` 查看，`/correct ID ...` 更正，`/forget ID` 删除。`/memory-mode auto` 启用普通自动记忆；`/auto-memory`、`/auto-correct A1 ...`、`/auto-forget A1` 管理。`/memory-mode session` 不新增持久记忆；`/wipe` 清库需确认；`/new` 清会话但保留已保存记录。切换模型保留原授权与辅助设置。

自动授权不包含敏感健康史等内容，支持范围有限，不确定可跳过。删除/更正同时失效相关派生上下文；落盘失败会提示未保存，会话内临时更正不能当成跨会话生效。[详细说明](docs/MEMORY.md)。所选服务会收到当前对话及相关授权记忆；正常聊天不新增全文日志。本地 SQLite **没有应用层加密**，勿上传数据库。

## 离线验证

```sh
python3 scripts/test_offline.py
```

测试子进程清除凭据环境，阻断 socket 连接，仅用合成数据和模拟传输；CI 不推理、不探测账户。参见 [架构](docs/ARCHITECTURE.md)、[F1–F5 修复及限制](docs/FIXES.md)、[实际离线结果](docs/OFFLINE_RESULTS.md)、[公开范围](docs/PUBLICATION.md)、[贡献](CONTRIBUTING.md)、[版本](CHANGELOG.md)。

本轮真实推理 API 为 0。测试通过不能替代自然度、长期记忆可靠性或心理效果验证。MIT 许可；外部研究资料仍遵循各自条款。
