# 基督教公版文献中译工作流

用多个不同来源的 AI 模型，把公有领域的基督教文献从原文（希腊文/拉丁文）译成现代中文，并用一套普通人也能看懂的方法评估译文质量。

- 只需要 Python 3.11+（不需要安装任何依赖），以及已登录的 `devin` 和 `pi` 命令行工具。
- 所有命令都在仓库根目录运行：`python3 scripts/tw.py <命令> <书名> [选项]`
- 每次模型调用的结果都会缓存在 `.cache/`：中途中断或额度用完，重跑同一命令会从断点继续，不会重复花钱。

## 一、流程总览

```
希腊文原文 ─┬─> ① 译者甲 ─┐
            └─> ① 译者乙 ─┴─> ② 裁决（对照原文，英译作旁证）─> ③ 润色（只看中文）
                                                                    │
                              ④ 复查：改了意思的段落自动回退 <──────┘
                                         │
                ⑤ 脚本检查 + ⑥ 两位评审挑错 ─> ⑦ 审阅单 ─> 你通读、修改、批准 ─> 发布
```

| 步骤    | 做什么                                | 产出（`books/<书>/work/chNN/`）               |
| ----- | ---------------------------------- | ---------------------------------------- |
| ① 初译  | 两个不同家族的模型只看原文，各自独立翻译，并写"疑难说明"      | `draft_a.txt` `draft_b.txt` `notes_*.md` |
| ② 裁决  | 第三个模型对照原文逐段比较两稿，合成一稿；记录实质分歧和理由（中文） | `adjudicated.txt` `disagreements.json`   |
| ③ 润色  | 中文母语模型只改表达，不改意思                    | `polished.txt`                           |
| ④ 复查  | 对照原文检查润色改动过的段落，改了意思的回退             | `recheck.json` → `final/chNN.txt`        |
| ⑤ 检查  | 脚本检查漏段、长度异常、残留外文、标点、术语表            | `check__final.json`                      |
| ⑥ 评审  | 两个与上面都不同家族的模型对照原文挑错并打分             | `eval/chNN/mqm__*.json`                  |
| ⑦ 审阅单 | 逐段列出译文、原文、英译和所有问题，标出【需看】           | `review.md`                              |

各角色用哪个模型写在 `config/pipeline.toml`。**原则：评审不能与译者、裁决者、润色者同一家族**（否则会偏袒自己）。

## 二、开始一本新书

```bash
python3 scripts/tw.py init <书名>          # 建目录骨架，然后填写 books/<书名>/book.toml
```

1. **底本**：每段一行、格式 `[章.节] 原文`，按章放进 `books/<书名>/source/chNN.txt`。如果原文是"`章.节 原文`"一行一节的格式，可以用：
   `python3 scripts/import_segmented.py <文件> books/<书名>`
2. **英译（可选，仅作旁证）**：同样格式放进 `books/<书名>/reference/<名称>/chNN.txt`。**初译模型看不到英译**，只有裁决和审阅单会用到。
3. **体例**：`book.toml` 里的 `profile` 决定译名传统，对应 `profiles/<体例>/` 下的风格指南和基础术语表。目前有 `protestant-cuv`（和合本体系）；`catholic-sigao`、`neutral-academic` 为预留。

## 三、译前准备（每本书一次）

```bash
python3 scripts/tw.py glossary 1clement      # 模型抽取术语候选 → books/1clement/glossary.csv
python3 scripts/tw.py scripture 1clement     # 模型标出经文引用 → books/1clement/scripture_index.csv
```

**术语表是最值得你花时间的地方。** 用 Excel/Numbers 打开 `profiles/protestant-cuv/glossary_base.csv` 和 `books/1clement/glossary.csv`：

- 同意的条目：把 `status` 改为 `locked`
- 不同意：先改 `target`（译法）、`avoid`（要避免的译法，用 `|` 分隔），再改为 `locked`
- 不需要的行：直接删掉

**只有 `locked` 的条目才会交给模型使用。** 经文引用索引也请抽查：它会进入发布稿的脚注。

## 四、翻译一章

```bash
python3 scripts/tw.py run 1clement --ch 5      # 一条龙：①–⑦
```

也可以分步运行：`translate`、`adjudicate`、`polish`、`check`、`review`。章节写法：`5`、`1-10`、`1,4,7`、`bench`（基准集）、`all`。

然后：

1. 打开 `books/1clement/work/ch05/review.md`，通读每一段；标【需看】的段落请对照下面列出的问题说明决定是否修改。
2. 要修改就直接编辑 `books/1clement/final/ch05.txt`，并在 `books/1clement/decisions.md` 里记一句理由（新定的译名请同时写进术语表）。
3. 批准：`python3 scripts/tw.py approve 1clement --ch 5`（有漏段等错误时会拒绝）。

`final/` 已存在时，重跑 `polish`/`run` **不会覆盖你的修改**，除非加 `--force`。

## 五、发布

```bash
python3 scripts/tw.py build 1clement           # → dist/1clement.md，只包含已批准的章节
```

发布稿自动带翻译说明、底本来源、经文出处脚注；`books/<书>/notes.csv`（列：`seg,note`）里的内容会作为译注脚注。

## 六、评估：怎么相对客观地判断译文好坏

| 方法          | 命令                                               | 看什么数字                       |
| ----------- | ------------------------------------------------ | --------------------------- |
| 错误标注评分（MQM） | `eval 1clement mqm`                              | 每千字扣分，越低越好（轻错 1、重错 5、致命 25） |
| 回译核对        | `eval 1clement back`                             | 把中文直译回英文，再对照原文找意思偏离，越少越好    |
| 问答测试        | `eval 1clement qa`                               | 根据原文出题，另一个模型只读中文作答，答对率越高越好  |
| 盲评对比        | `eval 1clement pairwise --version A --against B` | 隐去来源、随机顺序，分别比"忠实"和"可读"的胜负   |
| 评审可信度（埋错校准） | `eval 1clement seed`                             | 在译文里故意植入错误，看评审能抓出几成（检出率）    |
| 你本人打分       | `eval 1clement human [--against B]`              | 生成评分表 → 填写 → 再运行一次计分        |

- `eval 1clement all` = MQM + 回译 + 问答。
- `--version` 可以是 `final`、`draft_a`、`draft_b`、`adjudicated`、`polished`，或者 `books/<书>/baseline/<名称>/chNN.txt` 里的任何版本（例如把你以前的译稿放进 `baseline/old/`，就能用 `--version old` 对比）。
- 所有结果汇总在 `books/<书>/eval/report.md`（成绩单）。

**建议用法**：先在基准集（`--ch bench`，见 `book.toml`）上跑全套评估，用数据决定模型分配、提示词是否合适；正式翻译时 `run` 只跑 MQM 这一项轻量评估。每次改提示词或换模型后，再在基准集上重跑对比。

**这些分数怎么看**：评审模型会漏判，也会误判。两位评审都指出的问题最可信；埋错校准的检出率告诉你评审本身有多可靠。神学上敏感的段落，仍建议请懂原文的人抽看。

## 七、目录

```
config/pipeline.toml    各角色用哪个模型
profiles/<体例>/         风格指南、基础术语表
prompts/                每个步骤的提示词（可直接修改）
scripts/                tw.py 主命令；import_*.py 导入工具
books/<书>/              book.toml、source/、reference/、glossary.csv、scripture_index.csv、
                        notes.csv、work/、final/、eval/、decisions.md
dist/                   发布稿
apostolic_fathers/      原始资料（PDF 不进 git）
```
