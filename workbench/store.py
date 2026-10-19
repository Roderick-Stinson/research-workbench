"""数据存取。文件系统就是数据库（ADR-0012）：

$WB_HOME/
├── runs/<RUN-ID>/
│   ├── run.json          守护进程写：抓到的现场（事实）
│   ├── claim.json        wb CLI 写：认领信息（人的判断：配置组、目的、结论、指标）
│   ├── requirements.txt  环境
│   ├── stdout.log …      日志副本
│   ├── gpu.csv           GPU 采样
│   ├── metrics.jsonl     wb.log() 写的指标（可选）
│   └── outputs/          产物副本（只读）
├── registry/
│   ├── projects/<id>.json   项目（论文）+ 它的实验（claim）
│   └── variants/<id>.json   配置组
├── snapshots.git         代码快照
├── daemon/               守护进程状态、心跳、日志
└── journal.jsonl         所有写操作的流水账（谁、何时、做了什么）

run.json 和 claim.json 分开，守护进程和 CLI 永远不写同一个文件。
"""
from __future__ import annotations

import getpass
import json
import os
import re
from pathlib import Path

from . import config
from .util import now_iso, read_json, write_json


def home() -> Path:
    return config.home()


def runs_dir() -> Path:
    return home() / "runs"


def run_dir(run_id: str) -> Path:
    return runs_dir() / run_id


def registry_dir() -> Path:
    return home() / "registry"


def daemon_dir() -> Path:
    return home() / "daemon"


# ---------------- Run ----------------


def load_run(run_id: str) -> dict | None:
    d = run_dir(run_id)
    run = read_json(d / "run.json")
    if run is None:
        return None
    run["claim"] = read_json(d / "claim.json", None)
    att = (run["claim"] or {}).get("session")
    if att and att.get("file") and not (run.get("session") or {}).get("file"):
        run["session"] = att  # wb session attach 补关联的（对话在别的机器上，守护进程找不到）
    return run


def save_run(run: dict) -> None:
    r = {k: v for k, v in run.items() if k != "claim"}
    write_json(run_dir(run["id"]) / "run.json", r)


def load_claim(run_id: str) -> dict | None:
    return read_json(run_dir(run_id) / "claim.json", None)


def save_claim(run_id: str, claim: dict) -> None:
    write_json(run_dir(run_id) / "claim.json", claim)


def all_run_ids() -> list:
    d = runs_dir()
    if not d.exists():
        return []
    return sorted((p.name for p in d.iterdir() if p.is_dir() and (p / "run.json").exists()), reverse=True)


_run_cache: dict = {}


def all_runs(include_candidates: bool = False) -> list:
    """读全部 Run（按 run.json / claim.json 的 mtime 缓存，面板反复调用也不慢）。"""
    out = []
    for rid in all_run_ids():
        d = run_dir(rid)
        try:
            m1 = (d / "run.json").stat().st_mtime_ns
        except OSError:
            continue
        try:
            m2 = (d / "claim.json").stat().st_mtime_ns
        except OSError:
            m2 = 0
        key = (m1, m2)
        c = _run_cache.get(rid)
        if c and c[0] == key:
            run = c[1]
        else:
            run = load_run(rid)
            if run is None:
                continue
            _run_cache[rid] = (key, run)
        if not include_candidates and run.get("state") == "candidate":
            continue
        out.append(run)
    return out


def resolve_run_id(s: str) -> str:
    """允许只写 ID 的一部分（唯一匹配即可）。"""
    s = s.strip()
    if (run_dir(s) / "run.json").exists():
        return s
    ids = [i for i in all_run_ids() if s in i]
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise SystemExit(f"找不到 Run：{s}")
    raise SystemExit(f"'{s}' 匹配到多个 Run：{', '.join(ids[:10])}")


# ---------------- 注册表：项目 / 实验(claim) / 配置组 ----------------

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


def check_slug(s: str, what: str) -> str:
    if not SLUG_RE.match(s):
        raise SystemExit(f"{what} ID 只能用字母、数字、'-'、'_'、'.'（≤64 字符）：{s!r}")
    return s


def project_path(pid: str) -> Path:
    return registry_dir() / "projects" / f"{pid}.json"


def variant_path(vid: str) -> Path:
    return registry_dir() / "variants" / f"{vid}.json"


def load_project(pid: str) -> dict | None:
    return read_json(project_path(pid))


def save_project(p: dict) -> None:
    write_json(project_path(p["id"]), p)


def all_projects() -> list:
    d = registry_dir() / "projects"
    if not d.exists():
        return []
    out = [read_json(p) for p in sorted(d.glob("*.json"))]
    return [p for p in out if p]


def load_variant(vid: str) -> dict | None:
    return read_json(variant_path(vid))


def save_variant(v: dict) -> None:
    write_json(variant_path(v["id"]), v)


def all_variants() -> list:
    d = registry_dir() / "variants"
    if not d.exists():
        return []
    out = [read_json(p) for p in sorted(d.glob("*.json"))]
    return [v for v in out if v]


def find_experiment(project: dict, eid: str) -> dict | None:
    for e in project.get("experiments", []):
        if e["id"] == eid:
            return e
    return None


# ---------------- 流水账 ----------------


def journal(action: str, **fields) -> None:
    rec = {"ts": now_iso(), "by": os.environ.get("WB_ACTOR") or _actor(), "action": action, **fields}
    p = home() / "journal.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _actor() -> str:
    """谁在写：codex / claude / 人。从父进程环境猜。"""
    env = os.environ
    if env.get("CODEX_SANDBOX") or env.get("CODEX_HOME") or env.get("CODEX_THREAD_ID") or env.get("CODEX_MANAGED_BY_NPM"):
        return "codex"
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def read_journal(limit: int = 200) -> list:
    p = home() / "journal.jsonl"
    if not p.exists():
        return []
    lines = p.read_text(errors="replace").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out
