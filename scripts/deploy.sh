#!/usr/bin/env bash
# 在服务器上部署 main 分支的最新版本（MAINTAINING.md）。
#   bash scripts/deploy.sh            # 拉取 → 测试 → 更新 cron / agent 规则 → 重启面板 → 自检
#   bash scripts/deploy.sh v0.2.0     # 部署某个发布版本
set -euo pipefail
cd "$(dirname "$0")/.."
REF="${1:-main}"
: "${WB_HOME:?先 export WB_HOME=<数据目录>}"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "工作区有未提交的改动，先提交或丢弃再部署"; git status --short; exit 1
fi
before=$(git rev-parse HEAD)
git fetch --tags origin
if [ "$REF" = "main" ]; then git checkout -q main && git pull --ff-only -q origin main; else git checkout -q "$REF"; fi
after=$(git rev-parse HEAD)
echo "部署 $REF：${before:0:8} → ${after:0:8}"

python3 -m unittest discover -s tests/unit -q
python3 -m workbench install --bin-dir "${WB_BIN_DIR:-$HOME/.local/bin}" >/dev/null   # cron 与 wb 命令
python3 -m workbench install-agents >/dev/null                                        # agent 规则与 skill 链接

# 守护进程：下一分钟由 cron 用新代码拉起
pid=$(python3 -c "import json,os;print(json.load(open(os.path.join(os.environ['WB_HOME'],'daemon','heartbeat.json'))).get('pid',''))" 2>/dev/null || true)
[ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true

# 面板：依赖或 Dockerfile 变了才重建镜像，否则只重启（代码是只读挂载的）
if git diff --name-only "$before" "$after" | grep -qE '^panel/(Dockerfile|requirements\.txt)$'; then
  (cd panel && docker compose up -d --build)
else
  (cd panel && docker compose up -d && docker compose restart)
fi
sleep 70
python3 -m workbench doctor
