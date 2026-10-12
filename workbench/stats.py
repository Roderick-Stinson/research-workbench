"""聚合与一致性校验（ADR-0005 / ADR-0010）。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path

from . import config, store


def seed_fields_for(variant: dict | None, projects: list | None = None) -> list:
    fields = list(config.load()["seed_fields"])
    for p in projects or []:
        for f in p.get("seed_fields", []):
            if f not in fields:
                fields.append(f)
    if variant:
        for f in variant.get("seed_fields", []):
            if f not in fields:
                fields.append(f)
    return fields


def _metric_lines(run: dict) -> list:
    """metrics.jsonl（结束后）+ spool（运行中，wb.log() 还没被并入）。"""
    lines = []
    p = store.run_dir(run["id"]) / "metrics.jsonl"
    if p.exists():
        lines += p.read_text(errors="replace").splitlines()
    if run.get("state") == "running":
        for m in run.get("members", []):
            f = store.home() / "spool" / (m.replace(":", "-") + ".jsonl")
            if f.exists():
                lines += f.read_text(errors="replace").splitlines()
    return lines


def run_metrics(run: dict) -> dict:
    """一个 Run 的最终指标：metrics.jsonl 里不带 step 的记录 < claim.json 里显式填的。"""
    out = {}
    for line in _metric_lines(run):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "step" in rec:
            continue
        for k, v in rec.items():
            if k != "t" and isinstance(v, (int, float)):
                out[k] = v
    claim = run.get("claim") or {}
    for k, v in (claim.get("metrics") or {}).items():
        out[k] = v
    return out


def run_curves(run: dict) -> dict:
    """{metric: [(step, value)]}，来自 metrics.jsonl 带 step 的记录。"""
    out = {}
    for line in _metric_lines(run):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "step" not in rec:
            continue
        for k, v in rec.items():
            if k in ("t", "step") or not isinstance(v, (int, float)):
                continue
            out.setdefault(k, []).append((rec["step"], v))
    return out


def mean_std(xs: list):
    n = len(xs)
    if n == 0:
        return None, None
    m = sum(xs) / n
    if n == 1:
        return m, None
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var)


def variant_runs(vid: str, runs: list | None = None) -> list:
    runs = runs if runs is not None else store.all_runs()
    return [r for r in runs if (r.get("claim") or {}).get("variant") == vid and not (r.get("claim") or {}).get("ignored")]


def aggregate(runs: list) -> dict:
    """{metric: {n, mean, std, values:[{run, seed, value}]}}，只统计 status=ok 的 Run。"""
    out = {}
    for r in runs:
        c = r.get("claim") or {}
        if c.get("status", "ok") != "ok":
            continue
        for k, v in run_metrics(r).items():
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            out.setdefault(k, {"values": []})["values"].append(
                {"run": r["id"], "seed": c.get("seed", _seed_str(r)), "value": v})
    for k, d in out.items():
        m, s = mean_std([x["value"] for x in d["values"]])
        d.update({"n": len(d["values"]), "mean": m, "std": s})
    return out


def _seed_str(run: dict) -> str | None:
    s = run.get("seeds") or {}
    if not s:
        return None
    return ",".join(f"{k}={v}" for k, v in s.items())


OUT_FLAG = re.compile(r"^--?[\w\-]*(out|save|ckpt|checkpoint|log|result|run_name|exp_name|name|tag|wandb)[\w\-]*$", re.I)


def config_key(run: dict, seed_fields: list) -> str:
    """去掉种子字段和输出路径类参数之后的"配置指纹"。"""
    argv = list(run.get("argv", []))
    if argv:
        argv[0] = Path(argv[0]).name
    names = "|".join(re.escape(f) for f in seed_fields)
    rx_eq = re.compile(rf"^-{{0,2}}(?:[\w.]*\.)?({names})=", re.I)
    rx_flag = re.compile(rf"^--?({names})$", re.I)
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if rx_eq.match(a):
            continue
        if rx_flag.match(a):
            skip = True
            continue
        m = re.match(r"^(--?[\w\-]+)=", a)
        if m and OUT_FLAG.match(m.group(1)):
            continue
        if OUT_FLAG.match(a):
            skip = True
            continue
        out.append(a)
    cfg_hash = []
    for c in (run.get("entry") or {}).get("config_files", []):
        cfg_hash.append(Path(c).name)
    return hashlib.sha1(json.dumps([out, cfg_hash]).encode()).hexdigest()[:12] + " " + " ".join(out)[:300]


_NOISE_RX = re.compile(r"(^|/)(nohup\.out|[^/]*\.(log|out|err))$|(^|/)[^/]*log[^/]*\.txt$", re.I)
_tree_cache: dict = {}


def _is_noise(rel: str, outputs_rel: set) -> bool:
    """快照里不算"代码"的东西：其它 Run 的产物、日志、结果目录。"""
    if rel in outputs_rel or _NOISE_RX.search(rel):
        return True
    prune = set(config.load()["snapshot_prune_dirs"])
    parts = rel.split("/")[:-1]
    return any(p in prune or p.startswith(("out", "result", "ckpt", "checkpoint")) for p in parts)


def code_key(run: dict, outputs_rel: set | None = None) -> str | None:
    """代码指纹：快照里的代码文件（去掉产物 / 日志）的哈希。并发或顺序跑的种子之间才可比。"""
    from . import snapshot

    snaps = [s for s in (run.get("snapshots") or []) if s.get("commit")]
    if not snaps:
        return None
    h = hashlib.sha1()
    for s in snaps:
        c = s["commit"]
        if c not in _tree_cache:
            try:
                _tree_cache[c] = snapshot.ls_tree(c)
            except Exception:
                _tree_cache[c] = []
        out_rel = {os.path.relpath(p, s["root"]) for p in (outputs_rel or set())}
        for e in _tree_cache[c]:
            if not _is_noise(e["path"], out_rel):
                h.update(f"{s['root']}\0{e['path']}\0{e.get('sha')}\n".encode())
    return h.hexdigest()[:12]


def code_diff_paths(a: dict, b: dict, outputs_rel: set) -> list:
    """两个 Run 的代码差异（只看代码文件）。"""
    from . import snapshot

    def files(run):
        out = {}
        for s in run.get("snapshots") or []:
            if not s.get("commit"):
                continue
            out_rel = {os.path.relpath(p, s["root"]) for p in outputs_rel}
            for e in _tree_cache.get(s["commit"]) or snapshot.ls_tree(s["commit"]):
                if not _is_noise(e["path"], out_rel):
                    out[os.path.join(s["root"], e["path"])] = e.get("sha")
        return out

    fa, fb = files(a), files(b)
    return sorted(p for p in set(fa) | set(fb) if fa.get(p) != fb.get(p))


def consistency(runs: list, seed_fields: list) -> list:
    """组内不一致的警告（只警告，不拒绝，不拆组）。"""
    warns = []
    ok = [r for r in runs if (r.get("claim") or {}).get("status", "ok") == "ok"]
    if len(ok) < 2:
        return warns
    outputs = set()
    for r in runs:
        outputs |= {o["path"] for o in r.get("outputs", [])}
        for it in (r.get("stdio") or {}).values():
            for k in ("target", "tee"):
                if it.get(k, "").startswith("/"):
                    outputs.add(it[k])
    by_code = {}
    first = {}
    for r in ok:
        k = code_key(r, outputs)
        by_code.setdefault(k, []).append(r["id"])
        first.setdefault(k, r)
    if len(by_code) > 1:
        ks = list(by_code)
        files = code_diff_paths(first[ks[0]], first[ks[1]], outputs) if ks[0] and ks[1] else []
        warns.append({"kind": "code", "msg": f"组内代码不一致（{len(by_code)} 种）"
                      + (f"：{', '.join(os.path.basename(f) for f in files[:5])}" if files else ""),
                      "groups": {k or "无快照": v for k, v in by_code.items()}, "files": files[:50]})
    by_cfg = {}
    for r in ok:
        by_cfg.setdefault(config_key(r, seed_fields), []).append(r["id"])
    if len(by_cfg) > 1:
        warns.append({"kind": "config", "msg": f"组内命令/参数不一致（去掉种子后仍有 {len(by_cfg)} 种）",
                      "groups": by_cfg})
    seeds = [(r.get("claim") or {}).get("seed") or _seed_str(r) for r in ok]
    dup = {s for s in seeds if s is not None and seeds.count(s) > 1}
    if dup:
        warns.append({"kind": "seed", "msg": f"有重复的种子：{', '.join(map(str, dup))}"})
    return warns


def fmt_ms(m, s, digits: int = 4) -> str:
    if m is None:
        return "-"
    if s is None:
        return f"{m:.{digits}g}"
    return f"{m:.{digits}g} ± {s:.2g}"
