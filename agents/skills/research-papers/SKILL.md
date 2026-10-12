---
name: research-papers
description: 实验工作台的论文侧：用户说"这篇读过了 / 收藏 / 不相关 / 我想复现这篇 / 今天有什么论文 / 这个 idea 删掉 / 放弃 / 升级成 claim / 加个种子论文 / 看看某 benchmark 上的结果 / 领域地图"时使用。所有写操作都通过 wb 完成（面板只读）。
---

# 论文侧（wb）

面板（只读，地址见 agent 规则或 `wb status`）：今日论文、论文库、地图、结果表、IDEA、热点、报告。
数据：`wb status` 第一行的目录；**不要手动改**，只用 `wb`。

## 论文反馈（用户一句话 → 一条命令）
论文可以用 arXiv 号、库 id（arxiv-2502.04180）或标题片段指定：
```bash
wb paper mark <论文> --read            # 读过了
wb paper mark <论文> --fav             # 收藏
wb paper mark <论文> --irrelevant      # 不相关：这篇不再进精选，并作为反例影响以后的相关性判定（可加 --note 写原因）
wb paper mark <论文> --reproduce       # 想复现（再按下面的复现流程走）
wb paper mark <论文> --read --undo     # 撤销
wb paper show <论文> [--json]          # 看速读、抽取的结果、质量评估
wb paper ls --picked --day 2026-10-12  # 某天精选；--q 关键词；--facet optimizes=通信拓扑
```
"今天有什么论文"：`wb daily status`、`wb paper ls --day <今天>`，或直接给面板链接 /issue。

## 本人提供论文（"把这篇加进来"）
```bash
wb paper add 2502.04180                       # arXiv 号；也可以是链接、"标题"、服务器上的 PDF 路径，可一次给多个
wb paper add "<标题>" --seed --why "和我的 claim 2 直接相关"   # 同时设为种子（需要有 arXiv 号）
wb paper add /path/to/paper.pdf --explain     # 顺带做 8 步精读
```
直接进库（不再判相关性），立刻做速读；本人提供的论文不会再出现在每日精选里。

## 评价论文（"这篇太烂了" / "这篇很好" / "评价论文 xxx：2 分，理由：…"）
```bash
wb paper rate <论文> <1-5> --note "理由（用本人原话）"   # 1 很差 · 2 差 · 3 一般 · 4 好 · 5 很好
wb paper rate <论文> --undo                             # 撤销
```
- 用户没给分数时按语气换算（"狗屎 / 太烂了" → 1，"不太行" → 2，"还行" → 3，"不错" → 4，"很好 / 必读" → 5），
  回复里说清打了几分，让用户可以纠正；理由尽量保留用户原话。用户只说了标题时先 `wb paper ls --q "<标题关键词>"` 找到论文。
- 评价会直接影响以后的每日精选（按评价重排候选）；新评价攒够 3 条，每日流程会起草画像修改（含口味），推送里提示。
- 和"不相关"不同：不相关 = 不属于方向；评价 = 属于方向，但好不好、合不合口味。

## 口味与重新精选（"我更关注 X，不喜欢 Y" / "帮我重新推荐"）
```bash
wb field set 'taste={"likes": ["…"], "dislikes": ["…"]}'   # 本人明确说出的口味，直接写（旧版本自动留底）
wb daily repick --reason "一句话原因" --force                 # 按最新口味 / 评价重新精选本期并推送更新
```
口味要写成能判断一篇新论文的句子（看论文的主要贡献），尽量保留本人原话；写之前先 `wb field show` 看现有口味，合并而不是覆盖。

精选的硬门槛（ADR-0037）：方法论文必须和"领域常用方法"比较过才进精选。名单默认从论文库统计；本人想改时：
```bash
wb field set 'selection.standard_baselines=["AFlow", "ADAS", "MaAS", ...]'   # 指定名单（覆盖统计）
wb field set selection.min_accept=6                                        # 中稿评估门槛（1–10，默认 5）
```

## 更新研究画像（"根据这些论文更新我的方向"）
```bash
wb field suggest     # 根据种子、本人提供的论文、标为不相关的论文、本人的评价，起草新的描述 / 子主题 / 检索词 / 口味（不写入）
wb field apply       # 本人确认后才执行；旧版本自动留底在 registry/field.history/
```
先把 `wb field suggest` 的输出（改了什么、为什么、检索词增删）原样给用户看，**用户明确同意后**再 `wb field apply`。

## 研究方向与种子
```bash
wb seed ls | wb seed add <arXiv号> --why "原因" | wb seed rm <arXiv号>
wb field show | wb field set selection.max_per_day=8
```
baseline 一律是种子（ADR-0015）：新建 baseline 配置组时用 `wb variant set <vid> paper=arxiv-xxxx baseline=true`，会自动进入种子。

## IDEA（ADR-0024：告诉过用户的 IDEA，只有用户明确说删除才删）
```bash
wb idea ls [--hidden]                   # --hidden 包括没通过评审的
wb idea show <IDEA>
wb idea verify <IDEA>                   # 开始验证（之后的探索 Run 认领时在目的里写上 IDEA id）
wb idea abandon <IDEA> --reason "..."   # 放弃要写原因
wb idea promote <IDEA> --claim <项目>/<C>   # 升级为论文 claim（先 wb exp add 建好 claim）
wb idea delete <IDEA> --reason "用户原话"   # 仅在用户明确要求删除时
wb problem                              # 列出问题
```

## 复现（ADR-0023：先原文设定、对上数字，再上自己的设定）
1. 官方代码拉到 baseline 目录（agent 规则里写的 baseline 目录，即 `config.json` 的 `baseline_dir`）下的 `<论文名>/`，环境放该目录自己的 `.venv`；
2. 先跑**原文设定**（同任务 / backbone / 种子），实验照常启动（日志重定向），守护进程自动抓现场；
3. 建配置组并记录论文报告的数字：
   ```bash
   wb variant add maas-gsm8k-paper --title "MaAS × GSM8K（原文设定）" --method MaAS --task GSM8K --baseline
   wb repro ref maas-gsm8k-paper --from-paper arxiv-2502.04180 --task GSM8K --paper-metric accuracy --as acc
   wb adopt <RUN>... -v maas-gsm8k-paper -p "复现 MaAS 原文设定" --metrics-from <结果json> --keys acc
   wb repro check maas-gsm8k-paper        # 容差内 = 已复现；否则"复现差距"，把原因写进 wb variant set ... notes=
   ```
4. 之后才建"自己的设定"配置组，挂进论文的 claim。

## 其它
```bash
wb results [数据集]           # 跨论文结果表（数字都来自原文，带出处、评测规模、代码是否开源）
wb results refresh            # 归类数据集名、补评测规模、核对方法名、核实代码、重建索引（每日流程会自动做）
wb map show                   # 领域地图（每类没考虑到什么 = 找问题的入口）
wb report show --period weekly | wb report hot
wb daily run --steps harvest,judge,read   # 手动补跑某些步骤（一般不需要，cron 每天 01:00 跑、08:30 推送）
```
