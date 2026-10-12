# 维护这个工作台

GitHub 仓库是唯一来源；服务器上的部署目录是它的一个 clone。日常改动由 codex / Claude 直接在服务器上完成，人只做两件事：**提需求**和**合并 PR**。

## 三个地方，各管一件事

| 位置 | 是什么 | 进不进仓库 |
|---|---|---|
| 仓库（服务器上的部署目录，如 `~/research-workbench`） | 代码、面板、agent skill、设计文档 | 是 |
| `$WB_HOME`（数据目录） | Run、论文库、地图、IDEA、报告，以及**你自己的配置**：`config.json`、`registry/field.json`、`registry/seeds.json` | 否 |
| `$WB_HOME/secrets/`（权限 600） | 批量模型 key、额度管理端 key | 否 |
| `panel/.env` | 面板容器的 uid、路径、端口 | 否（仓库里只有 `panel/.env.example`） |

**任何个人信息（路径、IP、域名、账号、项目名）都只能写在数据目录或 `.env` 里**。CI 会扫描密钥形态的字符串。

## 改一个东西的流程

```
提需求（对 agent 说） → agent 开分支 → 改代码 + 测试 + （有新决策就写 ADR）→ 开 PR → CI 通过 → 你合并 → 部署
```

agent 在服务器上的具体步骤：

```bash
cd ~/research-workbench
git checkout main && git pull --ff-only
git checkout -b <简短描述>                      # 例如 fix/arxiv-429
# 修改……
python3 -m unittest discover -s tests/unit      # 单元测试
WB_E2E_DIR=$(mktemp -d) bash tests/e2e.sh       # 端到端（假模型，不碰真实数据）
git commit -am "<中文说明改了什么、为什么>"
git push -u origin HEAD && gh pr create --fill
```

合并后部署（在服务器上）：

```bash
export WB_HOME=<数据目录>
bash scripts/deploy.sh          # 拉 main → 单元测试 → 更新 cron 与 agent 规则 → 重启守护进程和面板 → wb doctor
```

部署前有未提交的改动会直接拒绝，避免"服务器上偷偷改过的代码"和仓库不一致。

## 设计决策

- 新的取舍写一份 `docs/adr/NNNN-<主题>.md`（背景 / 决策 / 后果），编号递增；被取代的旧 ADR 在状态里写"已被 ADR-xxxx 取代"，**不改写历史**。
- 术语有变化同步改 `CONTEXT.md`。

## 发版

- 版本号：`MAJOR.MINOR.PATCH`。数据格式（run.json、claim.json、论文目录结构）不兼容地改了才升 MAJOR，并在 CHANGELOG 写迁移方法。
- 发版步骤：更新 `CHANGELOG.md` 与 `workbench/__init__.py` 的版本 → PR 合并 → `git tag vX.Y.Z && git push --tags` → `gh release create vX.Y.Z --notes-file <CHANGELOG 对应段>`。
- 服务器上部署某个版本：`bash scripts/deploy.sh vX.Y.Z`。

## 新机器从零安装

```bash
git clone https://github.com/Roderick-Stinson/research-workbench && cd research-workbench
export WB_HOME=~/workbench-data
python3 -m workbench install --watch-root /path/to/experiments --bin-dir ~/.local/bin \
        --panel-url http://<服务器>:8765 --telegram-env /path/to/telegram.env --baseline-dir /path/to/baselines
wb field init mas-arch-opt            # 或照 examples/fields/ 写自己的研究方向
wb install-agents                     # agent 规则与 skill
cp panel/.env.example panel/.env      # 按实际修改后：
(cd panel && docker compose up -d --build)
```

然后把批量模型的 key 写进 `$WB_HOME/secrets/llm.env`，额度管理端（可选）写进 `secrets/quota.env`，`wb doctor` 自检。

## 出问题先看哪里

| 现象 | 看这里 |
|---|---|
| 实验没被抓到 | `wb doctor`；`$WB_HOME/daemon/daemon.log` |
| 每日推送没来 / 内容不全 | `wb daily status`（含额度与暂停原因）；`$WB_HOME/reports/daily/<日期>.log` |
| 额度 | `wb daily status` 第二行；`reports/llm_block.json`、`codex_block.json`；`reports/llm_usage.jsonl` |
| 面板打不开 | `docker logs workbench-panel` |
