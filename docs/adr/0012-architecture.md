# ADR-0012：技术架构

- 状态：已接受（2026-10-11 本人审阅通过；实现中的取舍见 ADR-0013）
- 日期：2026-10-11

## 组件（全部在实验室服务器，ADR-0006）
| 组件 | 作用 | 形态 |
|---|---|---|
| `wbd` 守护进程 | 抓现场（ADR-0009）：轮询 /proc + nvidia-smi，发现候选进程立即拍快照，关联 codex / cc 会话，进程结束后补记 | 常驻（cron + flock，见 ADR-0013） |
| `wb` CLI | 唯一写入口：project / claim / variant 管理、认领、写结论、restore、删产物、inbox | Python，尽量只用标准库 |
| agent 集成 | `AGENTS.md`（codex）+ `CLAUDE.md` + "记录" skill：说"记录"时调用 `wb` 完成认领；约定启动实验时一律重定向日志 | 文本规则 + skill |
| 面板 | 只读网页（ADR-0011） | FastAPI + 服务端渲染，docker compose，参照 paper-cockpit |

## 存储
```
<repo>/          # 工具本身的 git 仓库（含本 docs/）
$WB_HOME/
├── runs/<RUN-ID>/             # 一个 Run 的一切：run.json、requirements.txt、stdout/err、gpu.csv、metrics、产物（结束后只读）
├── snapshots.git              # 集中的代码快照仓库：用 --work-tree=<workdir> + 临时 index 存工作区，
│                              #   不碰 workdir 自己的 .git；workdir 不是 git 仓库也能用；git 自动去重
├── registry/                  # 项目 / claim / 配置组定义（人工数据，纯文本，可 diff）
└── index.sqlite               # 派生索引，随时可以从 runs/ + registry/ 全量重建
```
- 代码快照：遵守 workdir 的 `.gitignore`；超过阈值（如 50MB）的文件、模型和数据目录只记路径 + 哈希（ADR-0003）。

## 迭代方式（"可持续迭代"）
- 本目录的 `CONTEXT.md` + `docs/adr/` 随代码一起进仓库；每个新决策新增一条 ADR，旧的标记为"被取代"，不改写。
- 里程碑（每一步都能单独交付使用）：
  1. **M1 抓现场 + 还原**：`wbd` + `wb restore`。没有面板也能解决"回滚失败"。
  2. **M2 认领**：registry + `wb` 认领命令 + agent 规则和 skill，"说记录就记录"可用。
  3. **M3 只读面板**：首页 + 项目页 + Run 详情。
  4. **M4 增强**：论文表格导出（数字可反查到 Run）、逐样本对比、外网入口。
