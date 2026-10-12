# workbench · 研究工作台

> 平时照常跑实验；想记录的时候对 codex / Claude 说一声"记录"。
> 每天早上收到本方向的新论文（每篇都有速读 + 8 步精读）和通过独立评审的 IDEA。

研究里最常见的两个坑：**想回滚到某次实验的版本却回不去**（代码已经被 agent 改过了），以及**实验跑完了没记下来**。
这个工具不要求你改启动方式、也不要求在代码里加任何东西：

1. **自动抓现场**：后台守护进程（`wbd`）发现新启动的实验，在**启动那一刻**保存：
   代码（含未提交改动和 untracked 文件）、完整命令、环境变量（脱敏）、`requirements.txt`、GPU 使用、日志、产物（只读副本）。
2. **说"记录"时认领**：agent 找到对应的 Run，从**启动它的那段对话**里起草"目的"，你确认后归入
   **论文 → claim → 配置组 → Run**。同一配置组里不同种子自动算 **均值 ± 标准差**，组内代码 / 参数不一致时给出警告。
3. **一键还原**：`wb restore <RUN>` 生成一个独立目录，代码、依赖清单、重跑脚本齐全，不碰当前工作区。
4. **只读面板**：首页只放"我的论文"和"待认领"，细节在项目页、配置组页、Run 页。

设计讨论和每个决策的理由见 [`CONTEXT.md`](CONTEXT.md)（术语）和 [`docs/adr/`](docs/adr/)。

## 论文侧（ADR-0014 起）

一条链：**论文 → 问题 → IDEA → 验证 Run → claim → 论文项目**。

- **收录**：每天从 arXiv（逐个关键词检索）、HF Daily Papers、引用了种子论文的新论文（Semantic Scholar）、新 GitHub 仓库抓候选，
  去重（arXiv ID > DOI > 标题）后由批量模型只判"是否属于研究方向"；属于的每篇读全文做**速读**
  （问题 / 方法 / 实验与结果 / 局限 / 实验设置 + 维度标签 + 原文数字抽取 + 质量分）。
- **精选**：质量 + 关注度（HF 点赞、Hacker News）+ 与种子的引用关系 + 代码 / 录用，按质量门槛每天最多 10 篇；
  每篇由 codex 按 `paper-explainer` 写 **8 步精读 HTML**。
- **领域地图**：多维度 + 一个主分类，Related Work 文体（每类核心思路、代表工作、**这一类没考虑到什么**）；每天归类，每周最多重组一次，版本全留。
- **跨论文结果表**：方法 × benchmark，每个数字都能点回论文与表号；也是复现的参照数字。
- **构思**：每天由 codex 根据论文库、地图、实验与 bad case 想多个 IDEA（每个都针对一个有真实证据的问题），
  **独立审稿人**（另一个 codex 会话，联网查新）两票否决（新颖性 / 问题真实性）+ 均分 ≥ 7 才告诉你；
  通过的写成问题文档（真实数据作图）+ IDEA 文档（paper-explainer 前 6 步）。告诉过你的只有你说删才删。
- **复现**：`wb repro ref / check`，配置组记录论文报告的数字，与均值 ± 标准差比，容差内即"已复现"。
- **推送**：每天 01:00 跑流程，08:30 Telegram 推送当期；任何一步失败也推告警。还有周报 / 月报、热点榜、只读 MCP。

需要的配置：
- `$WB_HOME/secrets/llm.env`：批量任务模型（OpenAI 兼容或 Gemini 原生，`WB_LLM_PROTOCOL=openai|gemini`）；
- `config.json` 的 `telegram_env`：指向含 `TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID` 的文件；
- codex 已登录（精读、地图、构思、审稿都由 `codex exec` 执行，模型 / provider 可在 `config.json` 的 `codex` 里改）；
- `registry/field.json`：研究方向（描述、检索词、维度），`wb field show / set`；种子 `wb seed`。

只读 MCP（任意机器上的 Claude / codex）：

```bash
claude mcp add --transport http --scope user workbench http://<host>:8765/mcp
```

```bash
codex mcp add workbench --url http://<host>:8765/mcp
```

## 层级

```
项目 Project = 你自己要写的一篇论文（手动创建；别人论文的代码只是 baseline）
└─ 实验 = 论文里的一个 claim
   └─ 配置组 Variant = 方法 × 参数 × 任务，只差种子；均值 ± 标准差在这层算；可被多个 claim 共用
      └─ Run = 一次运行（守护进程自动抓到，认领前在"待认领"里）
```

## 安装（Linux，Python ≥ 3.9，git ≥ 2.25）

```bash
git clone https://github.com/Roderick-Stinson/research-workbench && cd research-workbench
export WB_HOME=~/workbench-data                     # 数据目录（你的配置、Run、论文库都在这里，不进仓库）
python3 -m workbench install --watch-root /path/to/experiments --bin-dir ~/.local/bin \
        --panel-url http://<服务器>:8765            # 建数据目录、配 cron、装 wb 命令
wb field init mas-arch-opt                          # 研究方向与种子论文（示例在 examples/fields/，可照着写自己的）
wb install-agents                                   # 规则写进 ~/.claude/CLAUDE.md、~/.codex/AGENTS.md，并链接 skill
```

可调项都在 `$WB_HOME/config.json`（默认值见 `workbench/config.py`）。论文推送还需要：
`--telegram-env <含 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 的文件>`、`$WB_HOME/secrets/llm.env`（批量模型），
可选 `$WB_HOME/secrets/quota.env`（GPT-Load 管理端，读真实额度）。

面板（所有挂载都是只读的）：

```bash
cp panel/.env.example panel/.env    # 按实际填 uid、数据目录、端口
cd panel && docker compose up -d --build
```

维护流程（分支 → PR → CI → 合并 → `scripts/deploy.sh`）、发版与排障见 [MAINTAINING.md](MAINTAINING.md)。

## 日常用法

大多数时候你不用敲命令，对 agent 说就行：

| 你说 | agent 做 |
|---|---|
| "用 3 个种子跑一下 X"（照常） | 启动实验，输出重定向到日志文件（规则里要求的） |
| "记录" | `wb inbox` → `wb context <RUN>` 起草目的 → 跟你确认 → `wb adopt` |
| "这几个结果怎么样" | `wb summary <配置组/项目>` |
| "回到上周那次实验的代码" | `wb restore <RUN>` |
| "这个不是实验" | `wb ignore <RUN>` |

命令一览（都支持 `--json`）：

```text
wb status | doctor                    状态 / 自检
wb inbox [--since 2d]                 待认领
wb ls [--running] [--grep x] [--dir d]
wb show <RUN>                         现场
wb context <RUN>                      启动它的对话片段
wb restore <RUN> [目录]               还原（git 仓库会带上历史，git status 就是当时的未提交改动）
wb diff <RUN> [<RUN2>] [--stat]       代码差异（不给 RUN2 就和现在的工作区比）
wb project add|ls|show|set            论文
wb exp add|link|unlink|set <项目>/<C1>  claim
wb variant add|ls|show|set            配置组
wb adopt <RUN>... -v <配置组> -p "目的" [--metrics-from f.json --keys acc,f1:metrics.f1] [-m acc=0.8]
wb note <RUN>... [-c 结论] [-m k=v] [--status ok|failed|discard]
wb summary <项目|项目/C1|配置组> [--verbose]
wb ignore <RUN>... [--undo]
wb rm-artifacts <RUN>... --yes --reason "..."   只在你明确说"不要了"时用

# 论文侧
wb daily run|push|status               每日流程（cron 自动跑）
wb backfill [--months 12]              首次回填
wb paper ls|show|mark <论文> [--read|--fav|--irrelevant|--reproduce] [--undo]
wb seed ls|add|rm                      种子论文
wb field show|set k=v                  研究方向
wb map show|versions|rebuild           领域地图
wb results [benchmark]                 跨论文结果表
wb idea ls|show|verify|abandon|promote|delete    IDEA（delete 只在你明确要求时）
wb problem                             问题
wb repro ref|check <配置组>            复现对比
wb report build|show|hot               周报 / 月报 / 热点
```

可选：在代码里记训练曲线（不调用也完全不影响抓现场）

```python
import workbench as wb
wb.log(step=step, loss=loss)   # 曲线
wb.log(acc=0.83)               # 最终指标
```

## 它是怎么工作的

- `wbd` 每 2 秒扫一次当前用户的进程：python 进程且 cwd / 脚本在 `watch_roots` 下，或任何用 GPU 的进程，都是候选。
  候选活过一个 tick 就拍代码快照（`python -c` 之类的短命进程不浪费快照）；用了 GPU 或活过 60 秒就成为 Run。
- 代码快照存在一个**集中的** bare 仓库 `$WB_HOME/snapshots.git` 里：`GIT_WORK_TREE=<你的目录>` + 每个目录一个持久 index，
  不碰你自己的 `.git`、不切分支、不动 stash；git 自动去重。
- 数据全是文件：`runs/<ID>/run.json`（守护进程写的事实）、`claim.json`（认领时人写的判断）、`registry/`（论文 / claim / 配置组）、
  `journal.jsonl`（所有写操作的流水账）。面板和 CLI 读的是同一份。

## 已知局限

- 直接输出到终端（tmux 里不重定向）的日志无法事后捕获。
- 退出码通常拿不到（守护进程不是父进程），按日志推断。
- 轮询有约 2 秒延迟：进程启动后 2 秒内代码就被改，会漏掉（面板上"迟到抓取"会标出来）。
- 产物默认复制一份只读副本；超大（>50GB）或磁盘紧张时只做硬链接，挡不住"原地覆盖写"。

## 测试

```bash
bash tests/e2e.sh     # 隔离目录里起守护进程、跑假实验，验证 抓现场 → 关联对话 → 认领 → 统计 → 一致性警告 → 还原
```

## 许可证

MIT，见 [LICENSE](LICENSE)。
