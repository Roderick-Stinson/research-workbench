## 实验工作台（wb）

这台机器上有实验工作台：后台守护进程会在实验**启动那一刻**自动保存现场（代码含未提交改动、命令、环境、日志、产物），之后可以一键还原。

- **启动实验时**（训练、评测、跑 benchmark 等会跑一阵子的命令），一律把输出重定向到日志文件，例如
  `nohup python train.py --seed 0 > logs/train_seed0.log 2>&1 &`；不要只输出到终端，否则日志无法被记录。
  多个种子用不同的日志文件。
- 用户说**"记录"**（记一下 / 认领 / record this）时，按 `record-experiment` skill 用 `wb` 认领对应的 Run。
  skill 不可用时：`wb inbox` 找 Run → `wb context <RUN>` 起草目的 → 和用户确认归属 → `wb adopt <RUN> -v <配置组> -p "<目的>"`。
- 想回到某次实验的代码：`wb restore <RUN>`（生成新目录，不动当前工作区）。
- **复现别人的论文**：不建项目；`wb variant add <vid> --baseline …` + `wb repro ref <vid> --paper <论文库 id> --set 指标=论文数字`，跑完认领到这个配置组（面板首页"复现"栏、论文页会显示对比）。
- **你在另一台电脑上、通过 ssh 在这台服务器上启动实验时**：认领后把你自己的对话记录传上来，Run 才能关联到对话：`ssh <服务器> 'wb session attach <RUN>...' < <你的会话 .jsonl>`（Claude Code：`~/.claude/projects/<目录>/<会话 id>.jsonl`；codex：`~/.codex/sessions/年/月/日/rollout-*.jsonl`）。
- 只通过 `wb` 写工作台数据；不要手动改 `{WB_HOME}/`；没有用户明确要求绝不执行 `wb rm-artifacts`。
- **论文侧**：每天 08:30 Telegram 推送当日论文与通过评审的 IDEA，面板 {PANEL_URL} 只读。
  用户说"读过了 / 收藏 / 不相关 / 想复现 / 删掉这个 idea / 加种子"等，按 `research-papers` skill 用 `wb paper / idea / seed / repro` 完成。
  告诉过用户的 IDEA 只有用户明确说删除才能 `wb idea delete`。
- 复现论文时官方代码放在 `{BASELINE_DIR}/<论文名>/`（ADR-0023）。
