"""wb：工作台唯一的写入口（ADR-0011）。人和 agent 都用它；面板只读。

常用：
  wb inbox                         待认领的 Run
  wb show <RUN>                    看一个 Run 的现场
  wb context <RUN>                 启动它的那段对话（用来起草"目的"）
  wb adopt <RUN>... -v <配置组> -p "<目的>"     认领
  wb summary <配置组|项目>          均值 ± 标准差 + 一致性警告
  wb restore <RUN> [目录]           还原现场
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, config, sessions, snapshot, stats, store
from .util import human_bytes, human_duration, now_iso, parse_iso, read_json

# ---------------- 输出小工具 ----------------


def out_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def table(rows: list, headers: list):
    if not rows:
        print("（空）")
        return
    w = [len(str(h)) for h in headers]

    def dw(s):
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(s))

    w = [max(dw(h), *(dw(r[i]) for r in rows)) for i, h in enumerate(headers)]
    line = "  ".join(str(h) + " " * (w[i] - dw(h)) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * x for x in w))
    for r in rows:
        print("  ".join(str(c) + " " * (w[i] - dw(c)) for i, c in enumerate(r)))


def short(s, n=60):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def run_status(run: dict) -> str:
    c = run.get("claim") or {}
    if c.get("ignored"):
        return "已忽略"
    if c.get("variant"):
        return "已认领"
    return "待认领"


def is_inbox(run: dict) -> bool:
    c = run.get("claim") or {}
    return not c.get("variant") and not c.get("ignored")


# ---------------- 状态 ----------------


def cmd_status(a):
    hb = read_json(store.daemon_dir() / "heartbeat.json")
    alive = hb and time.time() - hb.get("epoch", 0) < 60
    runs = store.all_runs()
    live = [r for r in runs if r.get("state") == "running"]
    inbox = [r for r in runs if is_inbox(r) and "service" not in r.get("tags", [])]
    disk = read_json(store.daemon_dir() / "disk.json") or {}
    if a.json:
        return out_json({"daemon_alive": bool(alive), "heartbeat": hb, "runs": len(runs), "running": len(live),
                         "inbox": len(inbox), "disk": disk, "home": str(store.home())})
    print(f"workbench {__version__}  数据目录 {store.home()}")
    if alive:
        print(f"守护进程：运行中（pid {hb['pid']}，{hb['ts']}，跟踪 {hb['running']} 个 Run）")
    else:
        print("守护进程：⚠ 没有在运行" + (f"（最后心跳 {hb['ts']}）" if hb else "") + "  → wb install 会配好 cron 自动拉起")
    print(f"Run：共 {len(runs)}，运行中 {len(live)}，待认领 {len(inbox)}")
    for v in disk.get("volumes", []):
        print(f"磁盘 {v['path']}：剩余 {human_bytes(v['free'])} / {human_bytes(v['total'])}")
    if disk.get("workbench_bytes") is not None:
        print(f"工作台自身占用：{human_bytes(disk['workbench_bytes'])}")


def run_row(r: dict):
    c = r.get("claim") or {}
    st = r.get("state")
    state = {"running": "运行中", "ended": "结束", "candidate": "候选"}.get(st, st)
    if st == "ended":
        g = (r.get("exit") or {}).get("guess")
        state += {"ok": "✓", "ok?": "✓?", "error": "✗", "error?": "✗?", "killed": "☠", "killed?": "☠?", "interrupted": "⏹"}.get(g, "")
    return [r["id"], state, human_duration(r.get("duration_s") or (time.time() - r["start_ts"] if st == "running" else None)),
            run_status(r), c.get("variant") or "", short(Path(r.get("workdir") or r.get("cwd", "")).name, 16),
            short(r.get("cmdline"), 70)]


HEAD = ["RUN", "状态", "时长", "认领", "配置组", "目录", "命令"]


def cmd_ls(a):
    runs = store.all_runs(include_candidates=False)
    if a.inbox:
        runs = [r for r in runs if is_inbox(r)]
        if not a.all:
            runs = [r for r in runs if "service" not in r.get("tags", [])]
    if a.running:
        runs = [r for r in runs if r.get("state") == "running"]
    if a.variant:
        runs = [r for r in runs if (r.get("claim") or {}).get("variant") == a.variant]
    if a.dir:
        runs = [r for r in runs if a.dir in (r.get("workdir") or "") or a.dir in r.get("cwd", "")]
    if a.grep:
        runs = [r for r in runs if a.grep in r.get("cmdline", "")]
    if a.since:
        t = time.time() - _parse_since(a.since)
        runs = [r for r in runs if r.get("start_ts", 0) >= t]
    runs = runs[: a.limit]
    if a.json:
        return out_json([_brief(r) for r in runs])
    table([run_row(r) for r in runs], HEAD)


def _parse_since(s: str) -> float:
    n, u = float(s[:-1]), s[-1]
    return n * {"m": 60, "h": 3600, "d": 86400}.get(u, 3600)


def _brief(r: dict) -> dict:
    c = r.get("claim") or {}
    return {"id": r["id"], "state": r.get("state"), "started_at": r.get("started_at"), "duration_s": r.get("duration_s"),
            "cwd": r.get("cwd"), "cmdline": r.get("cmdline"), "tags": r.get("tags"), "exit": r.get("exit"),
            "parent_run": r.get("parent_run"), "variant": c.get("variant"), "purpose": c.get("purpose"),
            "session": {k: (r.get("session") or {}).get(k) for k in ("agent", "session_id", "confidence")}}


def cmd_inbox(a):
    a.inbox, a.running, a.variant, a.dir, a.grep, a.since = True, False, None, None, None, a.since
    cmd_ls(a)


def cmd_show(a):
    rid = store.resolve_run_id(a.run)
    r = store.load_run(rid)
    if a.json:
        return out_json(r)
    c = r.get("claim") or {}
    print(f"{rid}  [{run_status(r)}]  {r.get('state')}  标签：{', '.join(r.get('tags', [])) or '-'}")
    print(f"命令    {r.get('cmdline')}")
    print(f"cwd     {r.get('cwd')}")
    print(f"时间    {r.get('started_at')} → {r.get('ended_at') or '运行中'}（{human_duration(r.get('duration_s'))}，抓取延迟 {r.get('capture_delay_s')}s）")
    ex = r.get("exit") or {}
    if r.get("state") == "ended":
        print(f"退出    code={ex.get('code')} 推断={ex.get('guess')}")
    if r.get("seeds"):
        print(f"种子    {r['seeds']}")
    if r.get("parent_run"):
        print(f"父 Run  {r['parent_run']}")
    for s in r.get("snapshots", []):
        if s.get("error"):
            print(f"快照    ✗ {s['root']}：{s['error']}")
            continue
        g = s.get("git") or {}
        extra = f"，HEAD {g.get('head', '')[:10]}（{g.get('branch')}）{'，有未提交改动' if g.get('dirty_tracked') else ''}" if g else ""
        print(f"快照    {s['root']}：{s.get('files')} 个文件，commit {s.get('commit', '')[:10]}{extra}，跳过大文件 {s.get('skipped_count', 0)} 个")
    sysi = r.get("system") or {}
    if sysi:
        print(f"环境    python {sysi.get('python')} @ {sysi.get('prefix')}，{sysi.get('packages')} 个包；"
              + "，".join(f"{k} {v}" for k, v in (sysi.get("key_packages") or {}).items()))
    if r.get("gpu"):
        print(f"GPU     卡 {r['gpu'].get('devices')}，峰值显存 {r['gpu'].get('peak_mem_mb')} MB")
    for name, it in (r.get("stdio") or {}).items():
        print(f"{name:7} {it.get('tee') or it.get('target')} {it.get('note', '')}")
    s = r.get("session") or {}
    if s:
        print(f"对话    {s.get('agent')} {s.get('session_id') or ''}（置信度 {s.get('confidence')}）{s.get('file') or ''}")
    os_ = r.get("outputs_summary") or {}
    if os_:
        print(f"产物    {os_.get('files')} 个文件，{human_bytes(os_.get('bytes'))}（{os_.get('mode')}）")
        for o in r.get("outputs", [])[:10]:
            print(f"        {o['path']}  {human_bytes(o['size'])}")
    if r.get("metrics_auto"):
        print("候选指标（从输出 json 自动提取，认领时可选用）：")
        for f, kv in list(r["metrics_auto"].items())[:5]:
            print(f"  {f}")
            print("    " + "，".join(f"{k}={v}" for k, v in list(kv.items())[:12]))
    m = stats.run_metrics(r)
    if m:
        print("指标    " + "，".join(f"{k}={v}" for k, v in m.items()))
    if c:
        print(f"认领    配置组 {c.get('variant')}，状态 {c.get('status')}，种子 {c.get('seed')}")
        print(f"目的    {c.get('purpose')}")
        print(f"结论    {c.get('conclusion')}")
    if r.get("log_tail"):
        print("日志尾部：")
        for line in r["log_tail"][-12:]:
            print("  " + line)


def cmd_context(a):
    rid = store.resolve_run_id(a.run)
    r = store.load_run(rid)
    li = r.get("session") or {}
    if not li.get("file"):
        # 守护进程当时没找到（或还没来得及），现在再找一次
        li = sessions.link(r["start_ts"], r.get("argv", []), r.get("cwd", ""), r.get("ancestors", [])) or {}
    if not li.get("file"):
        print(f"{rid}：没有找到启动它的 codex / claude 对话（可能是手动启动的）。请直接问本人实验目的。")
        return
    evs = sessions.context(li, before=a.before, after=a.after)
    if a.json:
        return out_json({"link": li, "events": evs})
    print(f"# {rid} 的启动对话：{li.get('agent')} {li.get('session_id')}（置信度 {li.get('confidence')}）")
    print(f"# 文件：{li.get('file')}")
    for e in evs:
        t = now_iso(e["ts"]) if e.get("ts") else "?"
        tag = {"user": "👤 用户", "assistant": "🤖 agent", "tool": "🔧 启动命令"}.get(e["role"], e["role"])
        if e.get("launch"):
            tag = "🚀 启动命令"
        text = e.get("text", "")
        if e["role"] == "tool":
            text = short(text, 600)
        else:
            text = short(text, a.width)
        print(f"\n[{t}] {tag}\n{text}")


# ---------------- 还原 / diff ----------------


def cmd_restore(a):
    from .restore import restore

    rid = store.resolve_run_id(a.run)
    rep = restore(rid, a.dest, use_git_history=not a.no_git)
    if a.json:
        return out_json(rep)
    print(f"已还原 {rid} → {rep['dest']}")
    for r in rep["roots"]:
        print(f"  {r['root']} → {r['dest']}：{r['files']} 个文件（{r['how']}）")
    print(f"说明：{rep['dest']}/RESTORE.md")
    print(f"重跑：bash {rep['rerun']}")


def _primary_commit(run: dict, root: str | None = None):
    for s in run.get("snapshots", []):
        if s.get("commit") and (root is None or s["root"] == root):
            return s
    return None


def cmd_diff(a):
    rid = store.resolve_run_id(a.run)
    r = store.load_run(rid)
    s1 = _primary_commit(r)
    if not s1:
        raise SystemExit("这个 Run 没有代码快照")
    if a.other:
        r2 = store.load_run(store.resolve_run_id(a.other))
        s2 = _primary_commit(r2, s1["root"]) or _primary_commit(r2)
        if not s2:
            raise SystemExit("另一个 Run 没有代码快照")
        print(snapshot.diff(s1["commit"], s2["commit"], stat_only=a.stat) or "（代码完全一致）")
        if not a.stat:
            ck = stats.config_key(r, stats.seed_fields_for(None))
            ck2 = stats.config_key(r2, stats.seed_fields_for(None))
            if ck != ck2:
                print(f"\n命令差异：\n- {r.get('cmdline')}\n+ {r2.get('cmdline')}")
    else:
        print(snapshot.diff_against_live(s1["commit"], s1["root"], stat_only=a.stat) or "（和现在的工作区完全一致）")


# ---------------- 注册表 ----------------


def cmd_project(a):
    if a.action == "ls":
        ps = store.all_projects()
        if a.json:
            return out_json(ps)
        table([[p["id"], short(p.get("title"), 40), p.get("venue") or "", len(p.get("experiments", [])),
                p.get("status", "active")] for p in ps], ["项目", "标题", "投稿", "实验数", "状态"])
    elif a.action == "add":
        pid = store.check_slug(a.id, "项目")
        if store.load_project(pid):
            raise SystemExit(f"项目已存在：{pid}")
        p = {"id": pid, "title": a.title or pid, "venue": a.venue, "deadline": a.deadline, "status": "active",
             "seed_fields": [x for x in (a.seed_fields or "").split(",") if x], "notes": a.notes,
             "workdirs": [x for x in (a.workdirs or "").split(",") if x],
             "experiments": [], "created": now_iso()}
        store.save_project(p)
        store.journal("project.add", project=pid, title=p["title"])
        print(f"已创建项目 {pid}")
    elif a.action == "set":
        p = _need_project(a.id)
        for kv in a.kv:
            k, v = kv.split("=", 1)
            if k in ("seed_fields", "workdirs"):
                v = [x for x in v.split(",") if x]
            p[k] = v
        store.save_project(p)
        store.journal("project.set", project=p["id"], fields=a.kv)
        print(f"已更新项目 {p['id']}")
    elif a.action == "show":
        p = _need_project(a.id)
        if a.json:
            return out_json(p)
        print(f"{p['id']}：{p.get('title')}  {p.get('venue') or ''} {p.get('deadline') or ''}")
        for e in p.get("experiments", []):
            print(f"\n[{e['id']}] {e.get('claim')}  （{e.get('status', 'open')}）")
            for vid in e.get("variants", []):
                _print_variant_summary(vid, indent="    ", metrics=e.get("metrics"))


def _need_project(pid):
    p = store.load_project(pid)
    if not p:
        raise SystemExit(f"没有这个项目：{pid}（wb project ls 看看）")
    return p


def _split_exp(s: str):
    if "/" not in s:
        raise SystemExit("实验要写成 <项目>/<实验ID>，例如 mypaper/C1")
    pid, eid = s.split("/", 1)
    return _need_project(pid), eid


def cmd_exp(a):
    p, eid = _split_exp(a.exp)
    e = store.find_experiment(p, eid)
    if a.action == "add":
        store.check_slug(eid, "实验")
        if e:
            raise SystemExit(f"实验已存在：{a.exp}")
        e = {"id": eid, "claim": a.claim, "metrics": [x for x in (a.metrics or "").split(",") if x],
             "variants": [], "status": "open", "notes": a.notes, "created": now_iso()}
        p.setdefault("experiments", []).append(e)
        store.save_project(p)
        store.journal("exp.add", project=p["id"], exp=eid, claim=a.claim)
        print(f"已创建实验 {a.exp}：{a.claim}")
        return
    if not e:
        raise SystemExit(f"没有这个实验：{a.exp}")
    if a.action == "link":
        for vid in a.variants:
            if not store.load_variant(vid):
                raise SystemExit(f"没有这个配置组：{vid}（先 wb variant add）")
            if vid not in e["variants"]:
                e["variants"].append(vid)
        store.save_project(p)
        store.journal("exp.link", project=p["id"], exp=eid, variants=a.variants)
        print(f"{a.exp} 现在包含配置组：{', '.join(e['variants'])}")
    elif a.action == "unlink":
        e["variants"] = [v for v in e["variants"] if v not in a.variants]
        store.save_project(p)
        store.journal("exp.unlink", project=p["id"], exp=eid, variants=a.variants)
        print(f"{a.exp} 现在包含配置组：{', '.join(e['variants']) or '（无）'}")
    elif a.action == "set":
        for kv in a.kv:
            k, v = kv.split("=", 1)
            e[k] = [x for x in v.split(",") if x] if k == "metrics" else v
        store.save_project(p)
        store.journal("exp.set", project=p["id"], exp=eid, fields=a.kv)
        print(f"已更新 {a.exp}")


def cmd_variant(a):
    if a.action == "ls":
        vs = store.all_variants()
        runs = store.all_runs()
        if a.json:
            return out_json(vs)
        rows = []
        for v in vs:
            n = len(stats.variant_runs(v["id"], runs))
            rows.append([v["id"], short(v.get("title"), 30), v.get("method") or "", v.get("task") or "", n])
        table(rows, ["配置组", "标题", "方法", "任务", "Run 数"])
    elif a.action == "add":
        vid = store.check_slug(a.id, "配置组")
        if store.load_variant(vid):
            raise SystemExit(f"配置组已存在：{vid}")
        v = {"id": vid, "title": a.title or vid, "method": a.method, "task": a.task, "baseline": bool(a.baseline),
             "params": a.params, "notes": a.notes, "seed_fields": [x for x in (a.seed_fields or "").split(",") if x],
             "created": now_iso()}
        store.save_variant(v)
        store.journal("variant.add", variant=vid)
        print(f"已创建配置组 {vid}")
        if a.exp:
            for ex in a.exp:
                p, eid = _split_exp(ex)
                e = store.find_experiment(p, eid)
                if not e:
                    raise SystemExit(f"没有这个实验：{ex}")
                if vid not in e["variants"]:
                    e["variants"].append(vid)
                store.save_project(p)
                store.journal("exp.link", project=p["id"], exp=eid, variants=[vid])
                print(f"  已挂到 {ex}")
    elif a.action == "set":
        v = store.load_variant(a.id)
        if not v:
            raise SystemExit(f"没有这个配置组：{a.id}")
        for kv in a.kv:
            k, val = kv.split("=", 1)
            v[k] = [x for x in val.split(",") if x] if k == "seed_fields" else val
        store.save_variant(v)
        store.journal("variant.set", variant=a.id, fields=a.kv)
        print(f"已更新配置组 {a.id}")
    elif a.action == "show":
        _print_variant_summary(a.id, verbose=True)


def _print_variant_summary(vid: str, indent: str = "", metrics=None, verbose: bool = False):
    v = store.load_variant(vid) or {"id": vid}
    runs = stats.variant_runs(vid)
    agg = stats.aggregate(runs)
    fields = stats.seed_fields_for(v, store.all_projects())
    warns = stats.consistency(runs, fields)
    keys = metrics or sorted(agg)
    ms = "；".join(f"{k} {stats.fmt_ms(agg[k]['mean'], agg[k]['std'])} (n={agg[k]['n']})" for k in keys if k in agg)
    print(f"{indent}{vid}{'（baseline）' if v.get('baseline') else ''}：{len(runs)} 个 Run  {ms or '暂无指标'}")
    for w in warns:
        print(f"{indent}  ⚠ {w['msg']}")
        if verbose and w.get("groups"):
            for k, ids in w["groups"].items():
                print(f"{indent}     {short(k, 80)}: {', '.join(ids)}")
    if verbose:
        for r in runs:
            c = r.get("claim") or {}
            m = stats.run_metrics(r)
            print(f"{indent}  - {r['id']} 种子 {c.get('seed')} [{c.get('status')}] "
                  + "，".join(f"{k}={m[k]}" for k in keys if k in m))


def cmd_summary(a):
    target = a.target
    if "/" in target or store.load_project(target):
        if "/" in target:
            p, eid = _split_exp(target)
            exps = [e for e in p.get("experiments", []) if e["id"] == eid]
        else:
            p = _need_project(target)
            exps = p.get("experiments", [])
        if a.json:
            res = {}
            for e in exps:
                res[e["id"]] = {"claim": e.get("claim"), "variants": {}}
                for vid in e.get("variants", []):
                    runs = stats.variant_runs(vid)
                    v = store.load_variant(vid) or {}
                    res[e["id"]]["variants"][vid] = {
                        "aggregate": stats.aggregate(runs),
                        "warnings": stats.consistency(runs, stats.seed_fields_for(v, [p]))}
            return out_json(res)
        for e in exps:
            print(f"[{p['id']}/{e['id']}] {e.get('claim')}")
            for vid in e.get("variants", []):
                _print_variant_summary(vid, indent="  ", metrics=e.get("metrics") or None, verbose=a.verbose)
            print()
    else:
        if not store.load_variant(target):
            raise SystemExit(f"既不是项目也不是配置组：{target}")
        if a.json:
            runs = stats.variant_runs(target)
            return out_json({"aggregate": stats.aggregate(runs),
                             "warnings": stats.consistency(runs, stats.seed_fields_for(store.load_variant(target)))})
        _print_variant_summary(target, verbose=True)


# ---------------- 认领 ----------------


def _parse_metrics(items: list) -> dict:
    out = {}
    for kv in items or []:
        if "=" not in kv:
            raise SystemExit(f"指标要写成 name=value：{kv}")
        k, v = kv.split("=", 1)
        try:
            out[k] = float(v)
        except ValueError:
            out[k] = v
    return out


def _metrics_from_file(path: str, keys: list | None) -> dict:
    data = json.loads(Path(path).read_text())
    flat = {}

    def walk(x, prefix):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            flat[prefix] = x
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))

    walk(data, "")
    if keys:
        out = {}
        for k in keys:
            name, _, src = k.partition(":")  # 支持 acc:results.accuracy 改名
            src = src or name
            if src not in flat:
                raise SystemExit(f"{path} 里没有 {src}（有：{', '.join(list(flat)[:30])}）")
            out[name] = flat[src]
        return out
    return flat


def _apply_claim(rid: str, a, new: bool) -> dict:
    run = store.load_run(rid)
    if run is None:
        raise SystemExit(f"找不到 Run {rid}")
    c = run.get("claim") or {}
    before = dict(c)
    if new:
        if c.get("variant") and c["variant"] != a.variant and not a.force:
            raise SystemExit(f"{rid} 已经认领到 {c['variant']}，要改请加 --force")
        c["variant"] = a.variant
        c.setdefault("claimed_at", now_iso())
        c.pop("ignored", None)
    if getattr(a, "purpose", None):
        c["purpose"] = a.purpose
        c["purpose_confirmed"] = not getattr(a, "draft", False)
    if getattr(a, "conclusion", None):
        c["conclusion"] = a.conclusion
    if getattr(a, "status", None):
        c["status"] = a.status
    c.setdefault("status", "ok")
    if getattr(a, "seed", None) is not None:
        c["seed"] = a.seed
    elif "seed" not in c:
        s = run.get("seeds") or {}
        c["seed"] = next(iter(s.values()), None) if len(s) == 1 else (",".join(f"{k}={v}" for k, v in s.items()) or None)
    m = dict(c.get("metrics") or {})
    if getattr(a, "metrics_from", None):
        m.update(_metrics_from_file(a.metrics_from, [k for k in (a.keys or "").split(",") if k] or None))
        c["metrics_source"] = a.metrics_from
    m.update(_parse_metrics(getattr(a, "metric", None)))
    if m:
        c["metrics"] = m
    if getattr(a, "notes", None):
        c["notes"] = a.notes
    c["run"] = rid
    c["updated_at"] = now_iso()
    hist = c.setdefault("history", [])
    hist.append({"ts": now_iso(), "by": store._actor(),
                 "changed": sorted(k for k in c if k not in ("history", "updated_at") and c.get(k) != before.get(k))})
    store.save_claim(rid, c)
    return c


def cmd_adopt(a):
    if not store.load_variant(a.variant):
        raise SystemExit(f"没有这个配置组：{a.variant}（先 wb variant add {a.variant} ...）")
    ids = [store.resolve_run_id(x) for x in a.runs]
    if a.with_children:
        allr = store.all_runs()
        for rid in list(ids):
            ids += [r["id"] for r in allr if r.get("parent_run") == rid and r["id"] not in ids]
    for rid in ids:
        c = _apply_claim(rid, a, new=True)
        store.journal("adopt", run=rid, variant=a.variant, purpose=c.get("purpose"))
        print(f"已认领 {rid} → {a.variant}（种子 {c.get('seed')}，指标 {c.get('metrics') or '-'}）")
    v = store.load_variant(a.variant)
    linked = [f"{p['id']}/{e['id']}" for p in store.all_projects() for e in p.get("experiments", [])
              if a.variant in e.get("variants", [])]
    if not linked:
        print(f"提示：配置组 {a.variant} 还没挂到任何实验（claim）上：wb exp link <项目>/<实验> {a.variant}")
    runs = stats.variant_runs(a.variant)
    warns = stats.consistency(runs, stats.seed_fields_for(v, store.all_projects()))
    for w in warns:
        print(f"⚠ {w['msg']}  （wb variant show {a.variant} 看详情）")


def cmd_note(a):
    for x in a.runs:
        rid = store.resolve_run_id(x)
        c = _apply_claim(rid, a, new=False)
        store.journal("note", run=rid, fields=c.get("history", [{}])[-1].get("changed"))
        print(f"已更新 {rid}")


def cmd_ignore(a):
    for x in a.runs:
        rid = store.resolve_run_id(x)
        c = store.load_claim(rid) or {}
        if a.undo:
            c.pop("ignored", None)
            c.pop("ignored_reason", None)
        else:
            if c.get("variant") and not a.force:
                print(f"跳过 {rid}：已认领到 {c['variant']}（要忽略请加 --force）")
                continue
            c["ignored"] = True
            c["ignored_reason"] = a.reason
            c["ignored_at"] = now_iso()
        c["run"] = rid
        store.save_claim(rid, c)
        store.journal("unignore" if a.undo else "ignore", run=rid, reason=a.reason)
        print(("已取消忽略 " if a.undo else "已忽略 ") + rid + "（记录和产物都保留）")


def cmd_rm_artifacts(a):
    """ADR-0004：只有本人明确说"不要了"才删；删的是产物副本，Run 记录保留。"""
    ids = [store.resolve_run_id(x) for x in a.runs]
    total = 0
    for rid in ids:
        od = store.run_dir(rid) / "outputs"
        if od.exists():
            for dp, _, fns in os.walk(od):
                for fn in fns:
                    try:
                        total += os.lstat(os.path.join(dp, fn)).st_size
                    except OSError:
                        pass
    if not a.yes:
        print(f"将删除 {len(ids)} 个 Run 的产物副本，共 {human_bytes(total)}：{', '.join(ids)}")
        print("Run 记录、代码快照、日志都会保留。确认是本人要求后，加 --yes 再执行一次。")
        return
    if not a.reason:
        raise SystemExit("请用 --reason 写明是谁、为什么要删（例如 --reason '本人：某项目早期探索不要了'）")
    for rid in ids:
        od = store.run_dir(rid) / "outputs"
        if od.exists():
            for dp, dns, fns in os.walk(od):
                os.chmod(dp, stat.S_IRWXU)
            shutil.rmtree(od)
        run = store.load_run(rid)
        run.pop("claim", None)
        run["artifacts_deleted"] = {"at": now_iso(), "by": store._actor(), "reason": a.reason}
        store.save_run(run)
        store.journal("rm-artifacts", run=rid, reason=a.reason)
        print(f"已删除 {rid} 的产物副本")


# ---------------- 安装 / 守护进程 ----------------

CRON_TAG = "# workbench-daemon"


def cmd_daemon(a):
    from .daemon import main

    main(["--once"] if a.once else [])


def cmd_install(a):
    home = store.home()
    for d in ("runs", "registry/projects", "registry/variants", "daemon", "spool"):
        (home / d).mkdir(parents=True, exist_ok=True)
    snapshot.ensure_repo()
    cfg_file = home / "config.json"
    cur_cfg = json.loads(cfg_file.read_text()) if cfg_file.exists() else {}
    for k, v in (("watch_roots", a.watch_root), ("panel_url", a.panel_url), ("telegram_env", a.telegram_env),
                 ("baseline_dir", a.baseline_dir)):
        if v:
            cur_cfg[k] = v
    if not cur_cfg.get("watch_roots"):
        raise SystemExit("请指定要监控的实验目录：wb install --watch-root /path/to/your/experiments")
    cfg_file.write_text(json.dumps(cur_cfg, indent=2, ensure_ascii=False) + "\n")
    config.load(reload=True)
    pkg_root = Path(__file__).resolve().parent.parent
    py = sys.executable
    line = (f"* * * * * cd {pkg_root} && WB_HOME={home} /usr/bin/flock -n {home}/daemon/lock "
            f"{py} -m workbench daemon >> {home}/daemon/cron.log 2>&1 {CRON_TAG}")
    cfg_p = home / "config.json"
    cfg_now = json.loads(cfg_p.read_text())
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    changed = False
    if proxy and not cfg_now.get("proxy"):
        cfg_now["proxy"] = proxy
        changed = True
    codex_bin = shutil.which("codex")
    if codex_bin and not (cfg_now.get("codex") or {}).get("bin"):
        cfg_now.setdefault("codex", {})["bin"] = codex_bin
        changed = True
    if changed:
        cfg_p.write_text(json.dumps(cfg_now, indent=2) + "\n")
    env = f"cd {pkg_root} && WB_HOME={home}"
    off = int(config.load().get("cron_utc_offset_hours", 0))

    def bj(h):  # 北京时间的小时 → cron 守护进程时区的小时
        return (h - 8 + off) % 24

    daily = (f"0 {bj(1)} * * * {env} /usr/bin/flock -w 21600 {home}/daemon/daily.lock {py} -m workbench daily run "
             f">> {home}/daemon/daily-cron.log 2>&1 {CRON_TAG}-daily")
    push = (f"30 {bj(8)} * * * {env} {py} -m workbench daily push >> {home}/daemon/daily-cron.log 2>&1 {CRON_TAG}-push")
    resume = (f"*/30 * * * * {env} /usr/bin/flock -n {home}/daemon/daily.lock {py} -m workbench daily resume "
              f">> {home}/daemon/resume-cron.log 2>&1 {CRON_TAG}-resume")
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines = [l for l in (cur.stdout.splitlines() if cur.returncode == 0 else []) if CRON_TAG not in l]
    if not a.no_cron:
        lines += [line, daily, push, resume]
        subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
        print("cron 已配置：守护进程每分钟检查一次；每日论文流程 01:00 运行、08:30 推送；每 30 分钟补跑暂停的步骤 / 续跑回填")
    if a.bin_dir:
        b = Path(a.bin_dir) / "wb"
        b.write_text(f"#!/bin/sh\nWB_HOME=\"${{WB_HOME:-{home}}}\" PYTHONPATH={pkg_root}${{PYTHONPATH:+:$PYTHONPATH}} exec {py} -m workbench \"$@\"\n")
        os.chmod(b, 0o755)
        print(f"已安装命令：{b}")
    print(f"数据目录：{home}")


BEGIN, END = "<!-- workbench:begin -->", "<!-- workbench:end -->"


def _upsert_block(path: Path, block: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text() if path.exists() else ""
    new_block = f"{BEGIN}\n{block.strip()}\n{END}"
    if BEGIN in old and END in old:
        pre, rest = old.split(BEGIN, 1)
        post = rest.split(END, 1)[1]
        text = pre + new_block + post
    else:
        text = (old.rstrip() + "\n\n" if old.strip() else "") + new_block + "\n"
    if text != old:
        path.write_text(text)
        return "已更新"
    return "无变化"


def cmd_install_agents(a):
    """把规则写进 agent 的全局说明（只改标记块之间的内容），并链接 record-experiment skill。"""
    repo = Path(__file__).resolve().parent.parent
    cfg = config.load()
    rules = (repo / "agents" / "rules.md").read_text().replace("{WB_HOME}", str(store.home()))
    rules = rules.replace("{PANEL_URL}", cfg.get("panel_url") or "（未配置面板地址：wb install --panel-url）")
    rules = rules.replace("{BASELINE_DIR}", cfg.get("baseline_dir") or "（未配置：wb install --baseline-dir）")
    skills = sorted(p for p in (repo / "agents" / "skills").iterdir() if (p / "SKILL.md").exists())
    home = Path.home()
    targets = [(home / ".claude" / "CLAUDE.md", home / ".claude" / "skills"),
               (home / ".codex" / "AGENTS.md", home / ".codex" / "skills")]
    for md, skills_dir in targets:
        if not md.parent.exists():
            continue
        if a.remove:
            if md.exists() and BEGIN in md.read_text():
                pre, rest = md.read_text().split(BEGIN, 1)
                md.write_text((pre.rstrip() + "\n" + rest.split(END, 1)[1].lstrip()).strip() + "\n")
            for sk in skills:
                link = skills_dir / sk.name
                if link.is_symlink():
                    link.unlink()
            print(f"已移除 {md} 中的工作台规则和 skill 链接")
            continue
        print(f"{md}：{_upsert_block(md, rules)}")
        skills_dir.mkdir(parents=True, exist_ok=True)
        for sk in skills:
            link = skills_dir / sk.name
            if link.is_symlink() or not link.exists():
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(sk)
                print(f"{link} → {sk}")
            else:
                print(f"跳过 {link}：已存在同名目录（不是链接），请手动处理")
    store.journal("install-agents", remove=bool(a.remove))


def cmd_uninstall(a):
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines = [l for l in (cur.stdout.splitlines() if cur.returncode == 0 else []) if CRON_TAG not in l]
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
    hb = read_json(store.daemon_dir() / "heartbeat.json")
    if hb:
        try:
            os.kill(hb["pid"], 15)
        except OSError:
            pass
    print("已移除 cron 并停止守护进程（数据全部保留）")


def cmd_doctor(a):
    ok = True

    def chk(name, cond, hint=""):
        nonlocal ok
        print(("✓ " if cond else "✗ ") + name + ("" if cond else f"  → {hint}"))
        ok = ok and cond

    home = store.home()
    chk(f"数据目录 {home}", home.exists(), "wb install")
    chk("快照仓库", (home / "snapshots.git" / "HEAD").exists(), "wb install")
    hb = read_json(store.daemon_dir() / "heartbeat.json")
    chk("守护进程心跳 < 60s", bool(hb and time.time() - hb.get("epoch", 0) < 60), "wb install；看 daemon/daemon.log")
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    chk("cron 已配置", CRON_TAG in cur.stdout, "wb install")
    chk("git 可用", shutil.which("git") is not None, "安装 git")
    chk("nvidia-smi 可用", shutil.which("nvidia-smi") is not None, "没有 GPU 也能用，只是不采样 GPU")
    for w in config.load()["watch_roots"]:
        chk(f"监控目录 {w}", os.path.isdir(w), "修改 config.json 的 watch_roots")
    sys.exit(0 if ok else 1)


# ---------------- 入口 ----------------


def build_parser():
    ap = argparse.ArgumentParser(prog="wb", description="实验工作台：自动抓现场 + 说'记录'时认领")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_, description=help_)
        p.set_defaults(fn=fn)
        p.add_argument("--json", action="store_true", help="输出 JSON（给 agent 用）")
        return p

    add("status", cmd_status, "守护进程和总体状态")
    for name, fn, h in (("ls", cmd_ls, "列出 Run"), ("inbox", cmd_inbox, "待认领的 Run")):
        p = add(name, fn, h)
        p.add_argument("--limit", type=int, default=50)
        p.add_argument("--since", help="只看最近的，如 6h / 2d")
        p.add_argument("--all", action="store_true", help="包括推理服务等（默认折叠）")
        if name == "ls":
            p.add_argument("--inbox", action="store_true")
            p.add_argument("--running", action="store_true")
            p.add_argument("--variant")
            p.add_argument("--dir", help="按工作目录过滤")
            p.add_argument("--grep", help="按命令过滤")
    p = add("show", cmd_show, "看一个 Run 的现场")
    p.add_argument("run")
    p = add("context", cmd_context, "启动这个 Run 的对话片段（起草'目的'用）")
    p.add_argument("run")
    p.add_argument("--before", type=int, default=12)
    p.add_argument("--after", type=int, default=4)
    p.add_argument("--width", type=int, default=1200)
    p = add("restore", cmd_restore, "还原现场到一个新目录（不碰原工作区）")
    p.add_argument("run")
    p.add_argument("dest", nargs="?")
    p.add_argument("--no-git", action="store_true", help="不 clone 原仓库历史，只解出快照")
    p = add("diff", cmd_diff, "代码 diff：两个 Run 之间，或 Run 和现在的工作区")
    p.add_argument("run")
    p.add_argument("other", nargs="?")
    p.add_argument("--stat", action="store_true")

    p = add("project", cmd_project, "项目（一篇你自己的论文）")
    p.add_argument("action", choices=["ls", "add", "show", "set"])
    p.add_argument("id", nargs="?")
    p.add_argument("kv", nargs="*", help="set 用：key=value")
    p.add_argument("--title")
    p.add_argument("--venue")
    p.add_argument("--deadline")
    p.add_argument("--seed-fields", help="逗号分隔，例如 seed,data_seed")
    p.add_argument("--workdirs", help="逗号分隔，仅作参考")
    p.add_argument("--notes")

    p = add("exp", cmd_exp, "实验 = 论文里的一个 claim（<项目>/<实验ID>）")
    p.add_argument("action", choices=["add", "link", "unlink", "set"])
    p.add_argument("exp")
    p.add_argument("variants", nargs="*", help="link/unlink：配置组；set：key=value")
    p.add_argument("--claim", help="add：claim 描述")
    p.add_argument("--metrics", help="这个 claim 关心的指标，逗号分隔")
    p.add_argument("--notes")

    p = add("variant", cmd_variant, "配置组（方法 × 参数 × 任务，只差种子）")
    p.add_argument("action", choices=["ls", "add", "show", "set"])
    p.add_argument("id", nargs="?")
    p.add_argument("kv", nargs="*")
    p.add_argument("--title")
    p.add_argument("--method")
    p.add_argument("--task")
    p.add_argument("--params", help="关键参数的文字描述")
    p.add_argument("--baseline", action="store_true", help="这是别人的方法（baseline）")
    p.add_argument("--seed-fields")
    p.add_argument("--notes")
    p.add_argument("--exp", action="append", help="同时挂到实验 <项目>/<实验ID>，可多次")

    p = add("adopt", cmd_adopt, "认领：把 Run 归入配置组，并补目的 / 结论 / 指标")
    p.add_argument("runs", nargs="+")
    p.add_argument("-v", "--variant", required=True)
    p.add_argument("-p", "--purpose")
    p.add_argument("--draft", action="store_true", help="目的是 agent 起草、本人还没确认")
    p.add_argument("-c", "--conclusion")
    p.add_argument("--status", choices=["ok", "failed", "discard"])
    p.add_argument("--seed")
    p.add_argument("-m", "--metric", action="append", help="name=value，可多次")
    p.add_argument("--metrics-from", help="从 json 文件读指标")
    p.add_argument("--keys", help="配合 --metrics-from：要哪些键，逗号分隔；可写 新名:原路径")
    p.add_argument("--notes")
    p.add_argument("--with-children", action="store_true", help="连同子 Run 一起认领")
    p.add_argument("--force", action="store_true")

    p = add("note", cmd_note, "补充 / 修改已有 Run 的目的、结论、指标、状态")
    p.add_argument("runs", nargs="+")
    p.add_argument("-p", "--purpose")
    p.add_argument("--draft", action="store_true")
    p.add_argument("-c", "--conclusion")
    p.add_argument("--status", choices=["ok", "failed", "discard"])
    p.add_argument("--seed")
    p.add_argument("-m", "--metric", action="append")
    p.add_argument("--metrics-from")
    p.add_argument("--keys")
    p.add_argument("--notes")

    p = add("ignore", cmd_ignore, "忽略（不是实验）；记录和产物都保留")
    p.add_argument("runs", nargs="+")
    p.add_argument("--reason")
    p.add_argument("--undo", action="store_true")
    p.add_argument("--force", action="store_true")

    p = add("summary", cmd_summary, "均值 ± 标准差（项目 / 项目/实验 / 配置组）")
    p.add_argument("target")
    p.add_argument("--verbose", action="store_true")

    p = add("rm-artifacts", cmd_rm_artifacts, "删除产物副本（只在本人明确要求时用）")
    p.add_argument("runs", nargs="+")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--reason")

    p = add("daemon", cmd_daemon, "运行守护进程（通常由 cron 拉起）")
    p.add_argument("--once", action="store_true")
    p = add("install", cmd_install, "初始化数据目录 + 配置 cron + 安装 wb 命令")
    p.add_argument("--bin-dir", help="把 wb 装到这个目录（应在 PATH 里）")
    p.add_argument("--no-cron", action="store_true")
    p.add_argument("--watch-root", action="append", help="要监控的实验目录（可多次）；首次安装必填")
    p.add_argument("--panel-url", help="面板地址，推送和 agent 规则里的链接用")
    p.add_argument("--telegram-env", help="含 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 的文件")
    p.add_argument("--baseline-dir", help="复现时官方代码放在哪个目录")
    add("uninstall", cmd_uninstall, "移除 cron、停止守护进程（数据保留）")
    p = add("install-agents", cmd_install_agents, "把规则写进 ~/.claude/CLAUDE.md 和 ~/.codex/AGENTS.md，并链接 skill")
    p.add_argument("--remove", action="store_true")
    add("doctor", cmd_doctor, "自检")
    from . import repro
    from .papers import cli_papers

    cli_papers.register(add)
    repro.register(add)
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except BrokenPipeError:
        pass
