# ADR-0003：每个 Run 都必须可还原

- 状态：已接受（产物保留策略见 ADR-0004）
- 日期：2026-10-11

## 背景
本人反馈：经常想回滚到某次实验的版本，却回滚失败。探索阶段由 cc / codex 驱动，代码多处于未提交状态，
agent 接着改下去以后，旧现场就丢了。

## 决策
- 探索 Run 和正式 Run **同样完整记录**，没有"轻量记录"这一档。
- 验收标准：任意一条记录，事后都能用一条命令 Restore 出当时的现场。
- 代码快照的初步方案：登记时用临时 index（`GIT_INDEX_FILE`）`add -A` → `write-tree` → `commit-tree`，
  把整个工作区（含未提交和 untracked 文件）存成隐藏 ref `refs/wb/<RUN-ID>`。
  **不切分支，也不改工作区和 stash**，对 agent 和人都无感。
- 还原：`git worktree add <dir> refs/wb/<RUN-ID>`，不碰当前工作区。

## 现场范围（Q4）
| 内容 | 怎么存 |
|---|---|
| 代码（含未提交和 untracked 文件） | 隐藏 git ref，全量 |
| 参数 / config / 完整命令行 / 相关环境变量 / 种子 | 存进 Run 记录（环境变量里的 key 要脱敏） |
| 环境 | 每个 Run 自动导出 `requirements.txt`（pip freeze）+ Python / CUDA / 驱动 / GPU 型号 |
| 基座模型、原始数据集（输入，不会变） | 只记路径 + 哈希（HF 模型记 revision） |
| **Run 产出的 checkpoint / LoRA**（产物，会被覆盖） | 归属于 Run，只读保护、永久保留，见 ADR-0004 |

