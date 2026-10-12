# ADR-0026：论文侧技术架构

- 状态：已接受（2026-10-11 本人确认开工；实现细节见 ADR-0028）
- 日期：2026-10-11
- 依据：ADR-0014 ～ ADR-0025、ADR-0027（检索与推荐逻辑参考 AI Safety HOT）

## 一条链
```
来源（arXiv / HF Daily Papers / 引用种子的新论文 / 顶会·OpenReview / GitHub）
  + 关注度信号（HF 点赞、Reddit、Hacker News、X：72 小时内独立来源数、讨论数、24h 走势）
  → 去重合并（arXiv ID > DOI > 归一化标题）
  → 收录：只判是否属于研究方向（规则 + 引文信号粗筛，再由便宜模型批量判），不评好坏
      每篇：速读（问题 / 方法 / 实验与结果 / 局限 / 实验设置）+ 维度标签 + 结果抽取（任务·backbone·指标·数值·出处）
  → 精选：质量 + 关注度打分，0～10 篇
      每篇：short summary + paper-explainer 8 步精读 HTML（codex，按篇并行）
  → 跨论文结果表（方法 × benchmark，每个数字可点回论文与表号）
  → 领域地图：新论文归类（每天）/ 重组（每周最多一次，留版本）
  → 构思：提议者想多个 IDEA（含问题 + 真实证据）→ 独立审稿人（两票否决 + 均分 ≥7）
  → 通过的：问题 HTML + IDEA 6 步 HTML
  → 当日一期：08:30 第一版推 Telegram（精选 + 通过的 IDEA）；当天的新进展更新同一期；任何一步失败也推
  → 周报 / 月报 + 热点榜
本人对 agent 说：已读 / 不相关 / 收藏 / 复现 / 删 IDEA / 升级为 claim …… → wb
复现：官方代码 → 原文设定（对数字）→ 自己的设定 → 进论文 claim（ADR-0023）
```

## 组件（全部在实验室服务器，同一个仓库 / 数据目录 / 面板）
| 组件 | 作用 | 形态 |
|---|---|---|
| `wb daily` | 每日流程，按步骤记状态，可断点续跑、单步重跑；失败推 Telegram | cron 每天 01:00 拉起（与守护进程同样的 cron + flock） |
| `workbench/papers/sources.py` | 四路抓取，合并去重（arXiv ID > DOI > 归一化标题） | 标准库 HTTP；Semantic Scholar / OpenReview / GitHub 公共 API |
| codex 执行器 | 精筛、精读、地图、构思、审稿：每个任务一次 `codex exec`，工作目录限定在该任务的输出目录 | 命令模板 + 模型 + provider 写在 config.json（ADR-0020） |
| skills | `paper-explainer`（以作者本地 skill 库的最新版为准，随仓库部署）、`idea-explainer`（前 6 步）、`problem-doc`（真实例子作图）、`idea-reviewer` | 与 record-experiment 同样链接到 ~/.codex/skills、~/.claude/skills |
| `wb paper / seed / idea / problem / map` | 唯一写入口（agent 用），写操作进 journal | CLI |
| 只读 MCP | 论文库、领域地图、IDEA、结果表、日 / 周 / 月报的查询，供任意机器上的 Claude / codex 使用 | 与面板同一进程（Streamable HTTP，只读） |
| 面板 | 新增：今日一期、论文库（维度筛选 + 计数，"已整理优先 / 最新发表"）、结果表、领域地图、热点榜、周 / 月报、问题与 IDEA；精读 / IDEA / 问题 HTML 内嵌查看 | 现有 FastAPI 面板，只读 |
| Telegram | 每日推送 + 失败告警 | 读 `baseline/.secrets/telegram.env` |

## 存储（$WB_HOME 下，文件即数据库）
```
papers/<paper-id>/        meta.json（来源、关注度证据、打分、是否精选）、quickread.json、results.json（抽取的数字 + 出处）、
                          summary.md、explainer.html（仅精选）、paper.pdf、status.json（已读/不相关/收藏…）
reports/                  daily/<date>.json|md、weekly/<YYYY-Www>、monthly/<YYYY-MM>、hot.json
seeds.json                种子：baseline 自动加入 + 本人指定
map/                      v<N>.json + map.html（主分类 Related Work 文体 + 维度矩阵），current → 最新版本
problems/<id>/            problem.json、problem.html、figs/（真实数据作图的脚本和数据都留着，可复核）
ideas/<id>/               idea.json（状态、来源问题、关联 claim）、idea.html、review.json；hidden/ 存没通过评估的
daily/<date>/             每一步的状态、日志、推送内容
```

## 默认值（非本人决策，可改）
- 每日流程 01:00 开始，**08:30 推送**（流程没跑完也按时推送已完成部分 + 告警）。
- 初始种子：`baseline/methods/` 下 5 个 baseline（AFlow、ADAS、MaAS、GDesigner、ARG-Designer）；Obsidian 种子库里只有 MaAS，已包含。
- 精读并行度 3；单篇失败重试 1 次。

## 里程碑（每一步都能单独用）
1. **P1 收录 + 每日一期**：五路来源 + 关注度信号 + 去重 + 收录（速读 + 标签）+ 精选打分 + Telegram + 面板"今日一期 / 论文库"。
2. **P2 精读**：精选每篇 paper-explainer 8 步 HTML，面板内嵌。
3. **P3 领域地图 + 结果表**：多维度 + 主分类（每日归类、每周重组、版本）；结果抽取与"方法 × benchmark"表。
4. **P4 构思**：问题 + IDEA 生成、独立审稿、通过的推送；`wb idea` 删除 / 放弃 / 升级为 claim。
5. **P5 复现**：配置组加 论文 / 报告数字（取自结果表）/ 复现状态，面板"论文报告 vs 我们复现"。
6. **P6 周 / 月报、热点榜、只读 MCP**。

## 风险
- codex 订阅额度：每天最多 10 篇精读 + 构思 + 审稿，可能触到额度上限 → 执行器记录每次耗时与失败原因，额度不够时按优先级降级（先保推送和 summary）。
- 外部 API 限速（Semantic Scholar 匿名很紧）→ 本地缓存，按天增量。
- X（Twitter）没有免费 API：关注度先用 HF Daily Papers / Reddit / Hacker News；X 等有可用渠道再接。
- 实验室服务器访问外网要走代理（<代理端口>），而代理对部分站点返回 502（构建面板时遇到过）→ P1 先逐个实测各来源的可达性。
