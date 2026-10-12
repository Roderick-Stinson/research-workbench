"""只读面板（ADR-0011）。所有写操作走 agent / wb CLI；这里只读文件系统。

首页：我的项目（论文）+ 待认领收件箱 + 一行磁盘。细节都在项目页 / 配置组页 / Run 页。
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workbench import __version__, config, sessions, snapshot, stats, store  # noqa: E402
from workbench.util import human_bytes, human_duration, now_iso, read_json, redact_value  # noqa: E402

HERE = Path(__file__).resolve().parent
app = FastAPI(title="实验工作台", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
tpl = Jinja2Templates(directory=HERE / "templates")


# ---------------- 模板工具 ----------------


def ago(ts):
    if ts is None:
        return "-"
    d = time.time() - ts
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return f"{int(d // 86400)} 天前"


def fmt_time(iso):
    if not iso:
        return "-"
    return iso.replace("T", " ")[5:16]


def fmt_num(v, digits=4):
    if isinstance(v, bool) or v is None:
        return "-" if v is None else str(v)
    if isinstance(v, (int, float)):
        return f"{v:.{digits}g}"
    return str(v)


from markupsafe import Markup, escape  # noqa: E402


def linkify_ids(text):
    """把正文里的 [arxiv-2502.04180] 之类的论文 id 变成链接。"""
    t = str(escape(text or ""))
    return Markup(re.sub(r"\[((?:arxiv-\d{4}\.\d{4,5})|(?:doi-[\w.\-]+)|(?:t-[0-9a-f]{16}))\]",
                         lambda m: f'<a href="/paper/{m.group(1)}">[{m.group(1)}]</a>', t))


tpl.env.filters["ids"] = linkify_ids
tpl.env.globals.update(
    human_bytes=human_bytes, human_duration=human_duration, ago=ago, fmt_time=fmt_time, fmt_num=fmt_num,
    fmt_ms=stats.fmt_ms, version=__version__, basename=os.path.basename,
)


def exit_badge(run):
    st = run.get("state")
    if st == "running":
        return ("运行中", "run")
    g = (run.get("exit") or {}).get("guess")
    return {
        "ok": ("成功", "ok"), "ok?": ("结束", "ok"),  # ok? = 日志里没有报错 "error": ("报错", "bad"), "error?": ("可能报错", "bad"),
        "killed": ("被杀", "bad"), "killed?": ("可能被杀", "bad"), "interrupted": ("中断", "warn"),
    }.get(g, ("结束", "muted"))


tpl.env.globals["exit_badge"] = exit_badge


def daemon_info():
    hb = read_json(store.daemon_dir() / "heartbeat.json") or {}
    alive = bool(hb) and time.time() - hb.get("epoch", 0) < 90
    disk = read_json(store.daemon_dir() / "disk.json") or {}
    vols = disk.get("volumes", [])
    warn_gb = config.load()["disk_warn_free_gb"]
    low = [v for v in vols if v["free"] < warn_gb * (1 << 30)]
    return {"alive": alive, "hb": hb, "disk": disk, "vols": vols, "low": low}


def is_inbox(r):
    c = r.get("claim") or {}
    return not c.get("variant") and not c.get("ignored")


def render(request, name, **kw):
    kw.setdefault("daemon", daemon_info())
    kw["request"] = request
    return tpl.TemplateResponse(request, name, kw)


# ---------------- 数据组装 ----------------


def runs_by_variant(runs):
    out = {}
    for r in runs:
        c = r.get("claim") or {}
        if c.get("variant") and not c.get("ignored"):
            out.setdefault(c["variant"], []).append(r)
    return out


def variant_row(vid, vruns, projects, metrics=None):
    v = store.load_variant(vid) or {"id": vid, "title": vid}
    agg = stats.aggregate(vruns)
    warns = stats.consistency(vruns, stats.seed_fields_for(v, projects))
    return {"v": v, "runs": vruns, "agg": agg, "warns": warns,
            "metrics": metrics or sorted(agg), "running": sum(1 for r in vruns if r.get("state") == "running")}


def project_overview(p, rbv, projects):
    vids = []
    for e in p.get("experiments", []):
        for vid in e.get("variants", []):
            if vid not in vids:
                vids.append(vid)
    runs = [r for vid in vids for r in rbv.get(vid, [])]
    last = max((r.get("start_ts", 0) for r in runs), default=None)
    size = sum((r.get("outputs_summary") or {}).get("bytes", 0) for r in runs)
    nwarn = sum(1 for vid in vids if stats.consistency(rbv.get(vid, []), stats.seed_fields_for(store.load_variant(vid), projects)))
    return {"p": p, "n_exp": len(p.get("experiments", [])), "n_var": len(vids), "n_runs": len(runs),
            "running": sum(1 for r in runs if r.get("state") == "running"), "last": last, "size": size,
            "nwarn": nwarn}


# ---------------- 页面 ----------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    runs = store.all_runs()
    projects = store.all_projects()
    rbv = runs_by_variant(runs)
    inbox = [r for r in runs if is_inbox(r)]
    main = [r for r in inbox if "service" not in r.get("tags", []) and not r.get("parent_run")]
    child_count = {}
    for r in inbox:
        if r.get("parent_run"):
            child_count[r["parent_run"]] = child_count.get(r["parent_run"], 0) + 1
    services = [r for r in inbox if "service" in r.get("tags", [])]
    total_out = sum((r.get("outputs_summary") or {}).get("bytes", 0) for r in runs)
    return render(request, "index.html",
                  projects=[project_overview(p, rbv, projects) for p in projects if p.get("status") != "archived"],
                  inbox=main[:60], inbox_total=len(main), child_count=child_count, services=services,
                  running=sum(1 for r in runs if r.get("state") == "running"), total_runs=len(runs),
                  total_out=total_out)


@app.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request, show: str = "main", q: str = ""):
    runs = store.all_runs()
    if show == "ignored":
        items = [r for r in runs if (r.get("claim") or {}).get("ignored")]
    elif show == "all":
        items = [r for r in runs if is_inbox(r)]
    else:
        items = [r for r in runs if is_inbox(r) and "service" not in r.get("tags", [])]
    if q:
        items = [r for r in items if q in r.get("cmdline", "") or q in r.get("cwd", "") or q in r["id"]]
    return render(request, "inbox.html", items=items[:500], show=show, q=q)


@app.get("/p/{pid}", response_class=HTMLResponse)
def project_page(request: Request, pid: str):
    p = store.load_project(pid)
    if not p:
        raise HTTPException(404, "没有这个项目")
    runs = store.all_runs()
    projects = store.all_projects()
    rbv = runs_by_variant(runs)
    exps = []
    for e in p.get("experiments", []):
        rows = [variant_row(vid, rbv.get(vid, []), projects) for vid in e.get("variants", [])]
        metrics = e.get("metrics") or sorted({m for r in rows for m in r["agg"]})
        # 每个指标的最好值（高亮用；默认越大越好）
        best = {}
        for m in metrics:
            vals = [r["agg"][m]["mean"] for r in rows if m in r["agg"] and r["agg"][m]["mean"] is not None]
            if vals:
                best[m] = max(vals)
        exps.append({"e": e, "rows": rows, "metrics": metrics, "best": best})
    used = {vid for e in p.get("experiments", []) for vid in e.get("variants", [])}
    timeline = sorted([r for vid in used for r in rbv.get(vid, [])], key=lambda r: -r.get("start_ts", 0))[:30]
    return render(request, "project.html", p=p, exps=exps, timeline=timeline,
                  ov=project_overview(p, rbv, projects))


@app.get("/v/{vid}", response_class=HTMLResponse)
def variant_page(request: Request, vid: str):
    v = store.load_variant(vid)
    if not v:
        raise HTTPException(404, "没有这个配置组")
    runs = stats.variant_runs(vid)
    projects = store.all_projects()
    row = variant_row(vid, runs, projects)
    used_by = [(p, e) for p in projects for e in p.get("experiments", []) if vid in e.get("variants", [])]
    runs_sorted = sorted(runs, key=lambda r: r.get("start_ts", 0))
    per_run = [(r, stats.run_metrics(r)) for r in runs_sorted]
    curves = {}
    for r in runs_sorted[:12]:
        for k, pts in stats.run_curves(r).items():
            curves.setdefault(k, []).append((r["id"], pts))
    charts = {k: line_chart(series) for k, series in list(curves.items())[:6]}
    from workbench import repro
    rep = repro.compare(v, runs) if v.get("reported") else []
    return render(request, "variant.html", v=v, row=row, used_by=used_by, per_run=per_run, charts=charts,
                  repro_rows=rep, repro_status=repro.status_of(v) if v.get("reported") else None)


@app.get("/r/{rid}", response_class=HTMLResponse)
def run_page(request: Request, rid: str, vs: str | None = None, tab: str = "overview"):
    r = store.load_run(rid)
    if not r:
        raise HTTPException(404, "没有这个 Run")
    rd = store.run_dir(rid)
    c = r.get("claim") or {}
    vid = c.get("variant")
    siblings = sorted(stats.variant_runs(vid), key=lambda x: x.get("start_ts", 0)) if vid else []
    # 代码对比对象：显式指定的，否则同组里上一个 Run
    base = None
    if vs:
        base = store.load_run(vs)
    elif siblings:
        prev = [s for s in siblings if s.get("start_ts", 0) < r.get("start_ts", 0)]
        base = prev[-1] if prev else None
    data = {"r": r, "c": c, "siblings": siblings, "base": base, "tab": tab,
            "metrics": stats.run_metrics(r), "tags": r.get("tags", []),
            "used_by": [(p, e) for p in store.all_projects() for e in p.get("experiments", [])
                        if vid and vid in e.get("variants", [])]}
    if tab == "overview":
        data["curves"] = {k: line_chart([(rid, pts)]) for k, pts in list(stats.run_curves(r).items())[:6]}
        data["gpu_chart"] = gpu_chart(rd / "gpu.csv")
    elif tab == "code":
        s1 = next((s for s in r.get("snapshots", []) if s.get("commit")), None)
        data["patches"] = []
        for i, s in enumerate(r.get("snapshots", [])):
            pf = rd / f"uncommitted-{i}.patch"
            if pf.exists():
                data["patches"].append((s["root"], pf.read_text(errors="replace")[:300_000]))
        if base and s1:
            s2 = next((s for s in base.get("snapshots", []) if s.get("commit") and s["root"] == s1["root"]), None)
            if s2:
                names = snapshot.diff_names(s2["commit"], s1["commit"])
                outs = {os.path.relpath(o["path"], s1["root"]) for x in (r, base) for o in x.get("outputs", [])}
                code = [n for n in names if not stats._is_noise(n, outs)]
                data["noise_files"] = [n for n in names if n not in code]
                if code:
                    data["diff_stat"] = snapshot.diff(s2["commit"], s1["commit"], stat_only=True, paths=code)
                    data["diff"] = snapshot.diff(s2["commit"], s1["commit"], paths=code)[:300_000]
        data["files"] = {s["root"]: snapshot.ls_tree(s["commit"]) for s in r.get("snapshots", []) if s.get("commit")}
    elif tab == "logs":
        logs = []
        for name in ("stdout", "stderr"):
            f = rd / f"{name}.log"
            src = ((r.get("stdio") or {}).get(name) or {})
            if f.exists():
                logs.append((name, f"{name}.log（副本）", tail_text(f, 400_000)))
            elif r.get("state") == "running":
                live = src.get("tee") or src.get("target")
                if live and live.startswith("/") and os.path.isfile(live):
                    logs.append((name, f"{live}（实时）", tail_text(Path(live), 400_000, offset=src.get("offset", 0))))
        data["logs"] = logs
    elif tab == "env":
        req = rd / "requirements.txt"
        data["requirements"] = req.read_text(errors="replace") if req.exists() else None
    elif tab == "talk":
        li = r.get("session") or {}
        evs = sessions.context(li, before=16, after=6) if li.get("file") else []
        evs = [dict(e, text=redact_value(e.get("text", ""))[:6000], ts_iso=now_iso(e["ts"]) if e.get("ts") else None)
               for e in evs]
        data["talk"] = evs
        data["link"] = li
    return render(request, "run.html", **data)


@app.get("/r/{rid}/file", response_class=PlainTextResponse)
def run_file(rid: str, root: str, path: str):
    r = store.load_run(rid)
    if not r:
        raise HTTPException(404)
    s = next((s for s in r.get("snapshots", []) if s.get("root") == root and s.get("commit")), None)
    if not s or ".." in path.split("/"):
        raise HTTPException(404)
    b = snapshot.show_file(s["commit"], path)
    if b is None:
        raise HTTPException(404)
    return PlainTextResponse(redact_value(b[:2_000_000].decode("utf-8", "replace")))


import mcp as mcp_server  # noqa: E402
import papers_view  # noqa: E402
import research_view  # noqa: E402

papers_view.register(app, lambda request, name, **kw: render(request, name, **kw))
research_view.register(app, lambda request, name, **kw: render(request, name, **kw))
mcp_server.register(app)


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__, "home": str(store.home())}


# ---------------- 图 ----------------


def tail_text(path: Path, limit: int, offset: int = 0) -> str:
    try:
        size = path.stat().st_size
        start = max(offset, size - limit)
        with open(path, "rb") as f:
            f.seek(start)
            text = f.read().decode("utf-8", "replace")
        if start > offset:
            text = f"…… 只显示最后 {human_bytes(limit)} ……\n" + text
        # 进度条（\r）只保留最后一帧
        text = "\n".join(line.split("\r")[-1] for line in text.split("\n"))
        return redact_value(text)
    except OSError:
        return ""


PALETTE = ["#3b6ea8", "#c0573e", "#3f8f5a", "#8a5cb8", "#b8862f", "#2f8a8a", "#a8456e", "#6b6b6b"]


def line_chart(series, w=560, h=170):
    """series: [(label, [(x, y)])] → 内联 SVG。"""
    pts = [p for _, s in series for p in s if isinstance(p[1], (int, float))]
    if len(pts) < 2:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    if y1 == y0:
        y1 = y0 + (abs(y0) or 1) * 0.01
    pl, pr, pt, pb = 46, 10, 10, 22

    def sx(x):
        return pl + (x - x0) / (x1 - x0) * (w - pl - pr)

    def sy(y):
        return pt + (1 - (y - y0) / (y1 - y0)) * (h - pt - pb)

    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img">']
    for frac in (0, 0.5, 1):
        yv = y0 + frac * (y1 - y0)
        yy = sy(yv)
        parts.append(f'<line x1="{pl}" x2="{w - pr}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>')
        parts.append(f'<text x="{pl - 6}" y="{yy + 4:.1f}" class="tick" text-anchor="end">{fmt_num(yv, 3)}</text>')
    parts.append(f'<text x="{pl}" y="{h - 4}" class="tick">{fmt_num(x0, 4)}</text>')
    parts.append(f'<text x="{w - pr}" y="{h - 4}" class="tick" text-anchor="end">{fmt_num(x1, 4)}</text>')
    for i, (label, s) in enumerate(series):
        s = [p for p in s if isinstance(p[1], (int, float))]
        if not s:
            continue
        d = " ".join(f"{'M' if j == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for j, (x, y) in enumerate(s))
        color = PALETTE[i % len(PALETTE)]
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6"><title>{html.escape(str(label))}</title></path>')
    parts.append("</svg>")
    return "".join(parts)


def gpu_chart(path: Path):
    if not path.exists():
        return None
    pts = []
    util = []
    try:
        lines = path.read_text().splitlines()[1:]
    except OSError:
        return None
    t0 = None
    for line in lines[-2000:]:
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            from workbench.util import parse_iso
            t = parse_iso(parts[0])
        except Exception:
            continue
        t0 = t0 or t
        try:
            pts.append(((t - t0) / 60, float(parts[2]) / 1024))
            if parts[3]:
                util.append(((t - t0) / 60, float(parts[3])))
        except ValueError:
            continue
    return {"mem": line_chart([("显存 GB", pts)]), "util": line_chart([("利用率 %", util)])}
