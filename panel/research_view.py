"""面板：领域地图 / 结果表 / IDEA 与问题 / 热点 / 周月报（只读）。"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from workbench import config
from workbench.papers import field as fieldmod
from workbench.papers import fieldmap, ideate, reports, results, store
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


def register(app, render):
    @app.get("/map", response_class=HTMLResponse)
    def map_page(request: Request, v: int = 0):
        m = fieldmap.load(v or None)
        vers = fieldmap.versions()
        titles = {}
        if m:
            titles = _titles({x for c in m["primary"]["categories"] for x in (c.get("members") or []) + (c.get("representative") or [])})
        return render(request, "map.html", m=m, versions=vers, titles=titles, field=fieldmod.field())

    @app.get("/results", response_class=HTMLResponse)
    def results_page(request: Request, task: str = ""):
        rows = results.all_rows()
        bs = results.benchmarks(rows)
        t = results.table(results.norm_task(task), rows) if task else (results.table(bs[0][0], rows) if bs else None)
        return render(request, "results.html", benchmarks=bs, t=t)

    @app.get("/ideas", response_class=HTMLResponse)
    def ideas_page(request: Request):
        its = ideate.all_ideas()
        probs = {p["id"]: p for p in ideate.all_problems()}
        return render(request, "ideas.html", ideas=its, problems=probs)

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
