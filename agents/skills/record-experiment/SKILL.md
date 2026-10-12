---
name: record-experiment
description: 用户说"记录"、"记一下这个实验"、"把这次跑的记下来"、"认领"、"record this run" 时使用。把实验工作台（wb）自动抓到的 Run 认领到 论文 → claim → 配置组，并补上目的、指标、结论。也用于"看看实验结果 / 均值方差"、"还原到某次实验的版本"、"这次实验没用，忽略掉"。
---

# 记录实验（实验工作台 wb）

背景：服务器上的守护进程已经在实验**启动那一刻**自动保存了现场（代码含未提交改动、命令、环境、日志、产物）。
你要做的只是**认领**：告诉工作台这个 Run 属于哪篇论文的哪个 claim 的哪个配置组，并补上人的判断。

术语（别混）：
- **项目** = 用户自己要写的一篇论文。下载的别人论文代码只是 baseline，**不是项目**。
- **实验 / claim** = 论文里要证明的一个论点，写作 `<项目>/<实验ID>`，如 `mypaper/C1`。
- **配置组 variant** = 方法 × 参数 × 任务固定、只差种子的一组 Run；均值 ± 标准差在这层算。一个配置组可以挂在多个 claim 下。
- **认领 adopt** = 把 Run 归入配置组。和论文的 claim 是两回事。

## 流程

1. **找到是哪个 Run**
   ```bash
   wb inbox --since 2d          # 待认领
   wb ls --running              # 正在跑的
   wb ls --grep train.py --since 1d
   ```
   根据对话里刚讨论 / 刚启动的命令、cwd、时间对上号。多个种子就是多个 Run，一起认领。拿不准就把候选列给用户选。

2. **看现场、起草目的**
   ```bash
   wb show <RUN>
   wb context <RUN>             # 启动它的那段 codex / claude 对话
   ```
   根据对话起草一句话的**目的**（想验证什么 / 和什么对比）。不要编造对话里没有的意图。

3. **确定归属**：`wb project ls`、`wb project show <项目>`、`wb variant ls`。
   向用户**一次性确认**：目的草稿、归到哪个 `项目/claim`、哪个配置组（已有的，还是新建，新建时给出 ID 建议，如 `ours-hotpotqa`、`maas-hotpotqa`）、指标从哪里取。
   用户已经说得很明确时不用再问。

4. **缺什么建什么**（只在用户确认后）：
   ```bash
   wb project add <id> --title "<论文标题>" --venue <会议> --seed-fields seed,data_seed
   wb exp add <项目>/<C1> --claim "<要证明的论点>" --metrics acc,f1
   wb variant add <vid> --title "<方法 × 任务>" --method <方法> --task <任务> [--baseline] --exp <项目>/<C1>
   wb exp link <项目>/<C2> <vid>          # 同一个配置组支撑另一个 claim，不用重跑
   ```

5. **认领**
   ```bash
   wb adopt <RUN> [<RUN> ...] -v <vid> -p "<目的>" \
       --metrics-from <结果json> --keys acc,f1:metrics.f1    # 或 -m acc=0.83 -m f1=0.71
   ```
   - `wb show` 里的"候选指标"是从输出 json 自动提取的，选对的键。
   - 用户还没确认目的时加 `--draft`。
   - 还在跑的 Run 也可以先认领，跑完再 `wb note <RUN> --metrics-from ...`。
   - 跑挂了的：`--status failed`；明确作废的：`--status discard`（都不进均值）。

6. **汇报**：`wb summary <vid>` 或 `wb summary <项目>`，把均值 ± 标准差和 ⚠ 一致性警告告诉用户。
   有"组内代码不一致"警告时，用 `wb variant show <vid>` 和 `wb diff <RUN_A> <RUN_B>` 说清楚差在哪，由用户决定。

7. 用户给出结论时：`wb note <RUN>... -c "<结论>"`；claim 有结论时 `wb exp set <项目>/<C1> status=supported conclusion="..."`。

## 其它常用

- 不是实验（下载脚本、调试）：`wb ignore <RUN> --reason "..."`（记录保留，只是不再出现在待认领里）。
- 还原某次实验的代码：`wb restore <RUN> [目录]`，然后看目录里的 `RESTORE.md`、`rerun.sh`。**不会**改动原工作区。
- 比较代码：`wb diff <RUN>`（和现在的工作区比）、`wb diff <RUN_A> <RUN_B>`。
- 所有命令加 `--json` 得到结构化输出。

## 规则

- 只用 `wb` 命令写入，**不要手动改**工作台数据目录（`wb status` 第一行）下的任何文件。
- **绝不**在用户没有明确说"不要了 / 删掉"时执行 `wb rm-artifacts`；执行时 `--reason` 写清楚是用户的哪句话。
- 目的、结论是用户的判断：起草可以，定稿要用户确认。
