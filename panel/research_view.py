"""面板：领域地图 / 结果表 / IDEA 与问题 / 热点 / 周月报（只读）。"""
from __future__ import annotations

from urllib.parse import urlencode

from markupsafe import Markup

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from workbench import config
from workbench.papers import field as fieldmod
from workbench.papers import codecheck, datasets, fieldmap, ideate, mapview, reports, results, store, topvenue
from workbench.util import read_json


def _titles(pids):
    out = {}
    for p in pids:
        m = store.load(p)
        if m:
            qr = store.load_quickread(p) or {}
            out[p] = {"title": qr.get("title_zh") or m.get("title"), "orig": m.get("title"), "arxiv": m.get("arxiv"),
                      "published": m.get("published")}
    return out


def _spark(series, w=150, h=34) -> Markup:
    """占比的小折线（内联 SVG）。样本少（n < 30）的时间段画成空心点。"""
    pts = [(i, p["share"], p.get("n") or 0) for i, p in enumerate(series) if p.get("share") is not None]
    if len(pts) < 2:
        return Markup("")
    n = len(series)
    hi = max(v for _, v, _ in pts) or 1
    xy = [(4 + (w - 8) * i / max(1, n - 1), h - 4 - (h - 8) * v / hi, k) for i, v, k in pts]
    path = " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{y:.1f}" for j, (x, y, _) in enumerate(xy))
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" class="{"lo" if k < 30 else "hi"}"/>' for x, y, k in xy)
    return Markup(f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}"><path d="{path}"/>{dots}</svg>')


def _qs(base: dict, **kw) -> str:
    """结果表页面的链接：在当前选择上改几项。"""
    d = dict(base)
    d.update(kw)
    keep = {k: v for k, v in d.items() if v not in (None, 0, "") and not (k == "kind" and v == "quality")}
    return "/results?" + urlencode(keep)


def register(app, render):
    @app.get("/map", response_class=HTMLResponse)
    def map_page(request: Request, v: int = 0):
        m = fieldmap.load(v or None)
        vers = fieldmap.versions()
        titles = {}
        if m:
            titles = _titles({x for c in m["primary"]["categories"] for x in (c.get("representative") or [])})
        ov = mapview.load()
        ids_ = {x["id"] for x in ov.get("landmarks") or []}
        for e in ov.get("insights") or []:
            ids_ |= {x for x in e.get("evidence") or [] if isinstance(x, str) and x.startswith(("arxiv-", "doi-", "t-"))}
        titles.update(_titles(ids_ - set(titles)))
        return render(request, "map.html", m=m, versions=vers, titles=titles, field=fieldmod.field(), ov=ov, spark=_spark,
                      lm={x["id"]: x for x in ov.get("landmarks") or []})

    @app.get("/results", response_class=HTMLResponse)
    def results_page(request: Request, cat: str = "", kind: str = "quality", full: int = 0, abl: int = 0,
                     ds: str = "", task: str = ""):
        rows = results.all_rows()
        cats = results.categories(rows)
        ds = ds or (results.norm_task(task) if task else "")
        kind = kind if kind in ("quality", "cost") else "quality"
        pids = {r["pid"] for r in rows}
        cover = {"papers": len(pids), "checked": len({r["pid"] for r in rows if r.get("has_eval")}),
                 "code_checked": len({r["pid"] for r in rows if r["code"]["status"] != "unchecked"}),
                 "renamed": len({(r["pid"], r["method_raw"]) for r in rows if r["name_status"] == "renamed"}),
                 "pending_names": len({r["task_raw"] for r in rows if r.get("pending")})}
        if ds:
            t = results.dataset_table(rows, ds, kind, full_only=bool(full), ablations=bool(abl), max_metrics=6)
            opts = {"ds": ds, "kind": kind, "full": full, "abl": abl}
            return render(request, "results.html", cats=cats, view="dataset", t=t, cover=cover, opts=dict(opts, cat=t["category"]),
                          cat_names=dict(datasets.CATEGORIES), qs=_qs, code_names=codecheck.STATUS)
        cat = cat or (cats[0]["key"] if cats else "")
        b = results.board(rows, cat, kind, full_only=bool(full), ablations=bool(abl)) if cat else None
        return render(request, "results.html", cats=cats, view="board", b=b, cover=cover,
                      opts={"cat": cat, "kind": kind, "full": full, "abl": abl}, cat_names=dict(datasets.CATEGORIES), qs=_qs,
                      code_names=codecheck.STATUS)

    @app.get("/ideas", response_class=HTMLResponse)
    def ideas_page(request: Request):
        its = ideate.all_ideas()
        probs = {p["id"]: p for p in ideate.all_problems()}
        # 构思记录：每天想了几个、通过几个；没通过的里最接近的（不推送，只在这里折叠显示）
        rejected = []
        for i in ideate.all_ideas(include_hidden=True):
            if i.get("informed"):
                continue
            rv = read_json(ideate.ideas_dir() / ".hidden" / i["id"] / "review.json") or {}
            veto = i.get("veto") or ("新颖性" if rv.get("novelty_veto") else ("问题真实性" if rv.get("problem_veto") else None))
            rejected.append(dict(i, veto=veto, review_summary=i.get("review_summary") or rv.get("summary"),
                                 scores=rv.get("scores"), error=rv.get("error")))
        by_day = {}
        for i in rejected + [dict(x, passed=True) for x in its]:
            d = by_day.setdefault(i.get("day") or "?", {"day": i.get("day") or "?", "total": 0, "passed": 0, "veto": 0, "error": 0})
            d["total"] += 1
            d["passed"] += bool(i.get("passed"))
            d["veto"] += bool(i.get("veto"))
            d["error"] += i.get("verdict") == "error"
        near = sorted([r for r in rejected if not r.get("veto") and r.get("verdict") != "error" and (r.get("score") or 0) >= 6],
                      key=lambda r: -(r.get("score") or 0))
        return render(request, "ideas.html", ideas=its, problems=probs, days=sorted(by_day.values(), key=lambda d: d["day"], reverse=True)[:14],
                      near=near, n_rejected=len(rejected))

    @app.get("/idea/{iid}", response_class=HTMLResponse)
    def idea_page(request: Request, iid: str):
        d = ideate.ideas_dir() / iid
        if "/" in iid or ".." in iid or not (d / "idea.json").exists():
            raise HTTPException(404)
        it = read_json(d / "idea.json")
        prob = read_json(ideate.problems_dir() / (it.get("problem_id") or "_") / "problem.json")
        return render(request, "idea.html", it=it, cand=read_json(d / "candidate.json") or {},
                      review=read_json(d / "review.json") or {}, prob=prob, has_doc=(d / "idea.html").exists())

    @app.get("/idea/{iid}/doc")
    def idea_doc(iid: str):
        p = ideate.ideas_dir() / iid / "idea.html"
        if "/" in iid or ".." in iid or not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="text/html")

    @app.get("/problem/{pid}/doc")
    def problem_doc(pid: str):
        p = ideate.problems_dir() / pid / "problem.html"
        if "/" in pid or ".." in pid or not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="text/html")

    @app.get("/must-read", response_class=HTMLResponse)
    def must_read_page(request: Request, tab: str = "must", order: str = "new", tier: str = ""):
        d = topvenue.load()
        items = d.get("worth" if tab == "worth" else "must") or []
        tiers = sorted({x["venue"]["tier"] for x in (d.get("must") or []) + (d.get("worth") or [])})
        if tier:
            items = [x for x in items if x["venue"]["tier"] == tier]
        items = sorted(items, key=lambda x: x.get("date") or "", reverse=(order != "old"))
        codes = {x["pid"]: codecheck.load(x["pid"]) or {} for x in items}
        sts = {x["pid"]: store.load_status(x["pid"]) for x in items}
        return render(request, "must_read.html", d=d, items=items, tab=tab, order=order, tier=tier, tiers=tiers,
                      codes=codes, sts=sts, code_names=codecheck.STATUS)

    @app.get("/hot", response_class=HTMLResponse)
    def hot_page(request: Request):
        h = read_json(config.home() / "reports" / "hot.json") or {}
        return render(request, "hot.html", h=h, items=h.get("items") or [])

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request, period: str = "weekly", key: str = ""):
        if period not in ("weekly", "monthly"):
            raise HTTPException(404)
        keys = reports.list_reports(period)
        rep = reports.load_report(period, key or (keys[0] if keys else "")) if keys else None
        refs = _titles(rep.get("narrative_refs") or []) if rep else {}
        return render(request, "reports.html", period=period, keys=keys, rep=rep, refs=refs,
                      other=reports.list_reports("monthly" if period == "weekly" else "weekly"))
