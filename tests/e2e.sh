#!/usr/bin/env bash
# 端到端测试：在隔离目录里起一个守护进程，跑假实验，验证 抓现场 → 认领 → 统计 → 还原。
# 用法：bash tests/e2e.sh   （不碰真实数据目录）
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
T="${WB_E2E_DIR:-$(mktemp -d -t wb_e2e.XXXXXX)}"
chmod -R u+w "$T" 2>/dev/null || true; rm -rf "$T"; mkdir -p "$T/home" "$T/work"
export WB_HOME="$T/home" PYTHONPATH="$REPO"
cat > "$WB_HOME/config.json" <<EOF
{"watch_roots": ["$T/work"], "promote_after_seconds": 4, "tick_seconds": 1,
 "codex_sessions": "$T/codex", "claude_projects": "$T/none"}
EOF
wb() { python3 -m workbench "$@"; }

# ---- 一个 git 仓库形式的实验项目，带未提交改动 ----
P="$T/work/proj"; mkdir -p "$P/configs"
cd "$P"; git init -q; git config user.email t@t; git config user.name t
cat > train.py <<'EOF'
import argparse, json, os, random, time, sys
sys.path.insert(0, os.environ["WB_REPO"])
import workbench as wb
ap = argparse.ArgumentParser(); ap.add_argument("--seed", type=int); ap.add_argument("--config")
ap.add_argument("--output_dir"); a = ap.parse_args()
random.seed(a.seed); lr = 0.1  # VERSION-A
os.makedirs(a.output_dir, exist_ok=True)
for step in range(6):
    wb.log(step=step, loss=1.0 / (step + 1))
    time.sleep(1)
acc = 0.8 + random.random() * 0.05
json.dump({"accuracy": acc, "nested": {"f1": acc - 0.1}}, open(os.path.join(a.output_dir, "results.json"), "w"))
open(os.path.join(a.output_dir, "model.ckpt"), "wb").write(os.urandom(4096))
print("done acc", acc)
EOF
echo "lr: 0.1" > configs/base.yaml
git add -A; git commit -qm init
echo "# uncommitted change" >> train.py         # 未提交改动
echo "scratch" > notes_untracked.txt              # untracked 文件

# ---- 一段假的 codex 对话：用户提需求 → agent 启动 3 个种子（用来测"关联对话"）----
D="$T/codex/2026/10/11"; mkdir -p "$D"
python3 - "$D/rollout-test.jsonl" "$P" <<'PY'
import json, sys, datetime
now = datetime.datetime.now(datetime.timezone.utc)
ts = lambda dt: (now + datetime.timedelta(seconds=dt)).isoformat().replace("+00:00", "Z")
cmd = "cd %s && for s in 0 1 2; do nohup python3 train.py --seed $s --config configs/base.yaml --output_dir out/seed$s > log_seed$s.txt 2>&1 & done" % sys.argv[2]
rows = [
  {"timestamp": ts(-30), "type": "session_meta", "payload": {"id": "sess-e2e", "cwd": sys.argv[2]}},
  {"timestamp": ts(-20), "type": "event_msg", "payload": {"type": "user_message", "message": "用 3 个种子跑一下 toy 上的 ours，看 lr=0.1 的 accuracy 稳不稳"}},
  {"timestamp": ts(-10), "type": "event_msg", "payload": {"type": "agent_message", "message": "好，我并发启动 seed 0/1/2，日志写到 log_seed*.txt。"}},
  {"timestamp": ts(-1), "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "const r = await tools.exec_command(" + json.dumps({"cmd": cmd}) + ");"}},
]
open(sys.argv[1], "w").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
PY

# ---- 起守护进程 ----
python3 -m workbench daemon > "$T/daemon.out" 2>&1 &
DPID=$!
trap 'kill $DPID 2>/dev/null || true' EXIT
sleep 2

# 三个种子，日志重定向到文件（agent 约定的方式）
PIDS=""
for s in 0 1 2; do
  WB_REPO="$REPO" python3 train.py --seed $s --config configs/base.yaml --output_dir out/seed$s > "log_seed$s.txt" 2>&1 &
  PIDS="$PIDS $!"
done
# 一个短命脚本：不应成为 Run
WB_REPO="$REPO" python3 -c "print(1)"
sleep 3
# 实验跑起来以后改代码：快照必须是启动那一刻的
sed -i 's/VERSION-A/VERSION-B/' train.py
for p in $PIDS; do wait $p || true; done
# 顺序再跑两个种子（VERSION-B）：后一个的快照里会混进前一个的产物 / 日志，校验时不能误报
for s in 3 4; do
  WB_REPO="$REPO" python3 train.py --seed $s --config configs/base.yaml --output_dir out/seed$s > "log_seed$s.txt" 2>&1
done
sleep 4
kill $DPID; wait $DPID 2>/dev/null || true
trap - EXIT

echo "== ls"; wb ls
N=$(wb ls --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
[ "$N" = 5 ] || { echo "FAIL: 期望 5 个 Run，实际 $N"; cat "$WB_HOME/daemon/daemon.log"; exit 1; }
R0=$(wb ls --json | python3 -c 'import json,sys; r=[x for x in json.load(sys.stdin) if "--seed 0" in x["cmdline"]]; print(r[0]["id"])')
echo "== show $R0"; wb show "$R0"
wb show "$R0" --json | python3 -c 'import json,sys; s=json.load(sys.stdin)["session"]; assert s and s.get("session_id")=="sess-e2e" and s["confidence"]=="high", s; print("关联对话 OK", s["confidence"], s["score"])'
wb context "$R0" | grep -q "看 lr=0.1 的 accuracy 稳不稳" || { echo "FAIL: wb context 没取到用户需求"; exit 1; }

# ---- 认领 ----
wb project add demo --title "Demo paper" --seed-fields seed
wb exp add demo/C1 --claim "ours beats baseline on toy" --metrics accuracy
wb variant add ours-toy --title "Ours on toy" --method ours --task toy --exp demo/C1
wb variant add ours-toy-b --title "Ours on toy (B)" --method ours --task toy --exp demo/C1
ids_of() { wb ls --json | python3 -c "import json,sys; print(' '.join(x['id'] for x in json.load(sys.stdin) if any('--seed '+s in x['cmdline'] for s in '$1'.split(','))))"; }
adopt() { v=$1; shift; for id in "$@"; do
  f=$(python3 -c "import json,sys; r=json.load(open('$WB_HOME/runs/$id/run.json')); print([o['path'] for o in r['outputs'] if o['path'].endswith('results.json')][0])")
  wb adopt "$id" -v $v -p "e2e 测试" --metrics-from "$f" --keys accuracy,f1:nested.f1 $EXTRA
done; }
EXTRA="" adopt ours-toy $(ids_of 0,1,2)
EXTRA="" adopt ours-toy-b $(ids_of 3,4)
echo "== summary"; wb summary demo --verbose
wb summary ours-toy --json | python3 -c 'import json,sys; d=json.load(sys.stdin); a=d["aggregate"]["accuracy"]; assert a["n"]==3 and a["std"] is not None, a; print("聚合 OK", a["mean"], a["std"])'

wb summary ours-toy-b --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert not d["warnings"], d["warnings"]; print("顺序种子无误报 OK")'
wb summary ours-toy --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert not d["warnings"], d["warnings"]; print("并发种子无误报 OK")'
EXTRA="--force" adopt ours-toy $(ids_of 3)
wb summary ours-toy --json | python3 -c 'import json,sys; d=json.load(sys.stdin); w=[x for x in d["warnings"] if x["kind"]=="code"]; assert w and any("train.py" in f for f in w[0]["files"]), d["warnings"]; print("代码改动被发现 OK:", w[0]["msg"])'
EXTRA="--force" adopt ours-toy-b $(ids_of 3)

# ---- 还原：必须是 VERSION-A + 未提交改动 + untracked 文件 ----
wb restore "$R0" "$T/restored"
grep -q "VERSION-A" "$T/restored/train.py" || { echo "FAIL: 还原的不是启动时的代码"; exit 1; }
grep -q "# uncommitted change" "$T/restored/train.py" || { echo "FAIL: 丢了未提交改动"; exit 1; }
test -f "$T/restored/notes_untracked.txt" || { echo "FAIL: 丢了 untracked 文件"; exit 1; }
(cd "$T/restored" && git status --short | head)
test -f "$T/restored/requirements.txt" || { echo "FAIL: 没有 requirements.txt"; exit 1; }
echo "== diff vs live"; wb diff "$R0" --stat
# 产物只读
test ! -w "$(find "$WB_HOME/runs/$R0/outputs" -name model.ckpt)" || { echo "FAIL: 产物不是只读"; exit 1; }
# 曲线
test -s "$WB_HOME/runs/$R0/metrics.jsonl" || { echo "FAIL: 没有 wb.log 指标"; exit 1; }
# 日志
grep -q "done acc" "$WB_HOME/runs/$R0/stdout.log" || { echo "FAIL: 没抓到日志"; exit 1; }
echo
echo "E2E 全部通过 ✓"
# 清理（产物副本是只读的，先加回写权限）；WB_E2E_KEEP=1 时保留现场方便排查
if [ -z "${WB_E2E_KEEP:-}" ]; then chmod -R u+w "$T" 2>/dev/null || true; rm -rf "$T"; fi
