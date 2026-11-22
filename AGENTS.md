# 项目约定（给 AI agent）

- 本项目是基督教公版文献中译工作流；用户说明见 `README.md`（中文）。
- 运行：`python3 scripts/tw.py <命令> <书名> ...`（书名在命令之后，如 `tw.py eval 1clement mqm --ch 5`）。只用 Python 标准库，不要引入依赖。
- 测试：`python3 -m unittest discover -s tests`
- 模型调用都经过 `scripts/agents.py`（`devin -p` / `pi -p`，在临时空目录中运行，结果缓存在 `.cache/`，缓存键不含 `NO_TOOLS` 后缀）。devin 调用一律带 `--config` 隔离配置（`config/devin_isolation.json`：PreToolUse hook 一律 `exit 2` 并记日志），缓存键含隔离配置摘要和 devin CLI 版本。改动隔离配置或升级 devin 后，先运行 `python3 scripts/verify_isolation.py`（默认免费的 swe-2-medium）。
- 第一阶段（独立初译）的冻结输入见 `books/1clement/decisions.md`；初译开始后不要改 `prompts/translate.md`、风格指南、术语表和原文。`tw.py translate` 会跳过已有 `draft_x.txt` + `draft_x.meta.json` 的章节。
- 角色与模型在 `config/pipeline.toml`；评审不得与译者、裁决者、润色者同一模型家族。
- 段落文件格式：每行 `[章.节] 文本`（`scripts/segments.py`）。模型输出必须与原文段落编号完全一致。
- 初译提示词不得包含英译或任何现有中译本；英译只用于裁决和审阅单。不得使用有版权的中译本或现代校勘本。
- `books/<书>/final/` 可能含用户的手工修改，不要覆盖（`polish` 需 `--force` 才覆盖）。
- 术语表只有 `status=locked` 的条目会进入提示词；不要替用户把条目改为 locked。
- 原始资料在 `apostolic_fathers/`（PDF 不进 git）。注意：`english_ccel/` 的 PDF 实为希腊文；`open_apostolic_fathers/LICENSE` 下载失败（内容为 404）。
- 模型耗时参考（第 5 章，约 820 个希腊字符）：初译 20–40 秒，裁决约 40 秒，评审 10–40 秒。devin / gemini-4-carbon-medium 每次约 6 分钟，已改用 low 档（约 15 秒）。
- 流程设计和译文问题的讨论记录在 `docs/discussions/`，约定见其中的 README.md（一话题一文件、只追加、署名 agent / 模型、每节一次提交）。改代码时在提交说明里写“依据讨论 NNN”。
- 日志与报告中标注模型时一律带上 agent（如 `devin / swe-2-high`、`pi[aliyun-token-plan] / qwen3.8-max`），因为同名模型可能在两个 agent 下都有。
