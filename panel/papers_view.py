"""面板的论文侧页面（只读，ADR-0022）：今日一期 / 论文库 / 论文详情。"""
from __future__ import annotations

import datetime as _dt
import json
from collections import Counter

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from workbench import config
from workbench.papers import field as fieldmod
from workbench.papers import codecheck, select, store
from workbench.papers.pipeline import load_issue


def _decorate(m: dict) -> dict:
    """给模板用的一篇论文：meta + 速读 + 标记 + 分数。"""
    qr = m.get("_qr") or store.load_quickread(m["id"]) or {}
    st = m.get("_status") if "_status" in m else store.load_status(m["id"])
    return {"m": m, "qr": qr, "st": st or {}, "score": select.score(m, qr) if qr else None,
            "has_explainer": (store.pdir(m["id"]) / "explainer.html").exists()}


def _facet_counts(papers: list, f: dict) -> dict:
    out = {}
    for key, d in f["facets"].items():
        c = Counter()
        for p in papers:
            for v in (p["qr"].get("facets") or {}).get(key) or []:
                c[v.split(":")[0].strip()] += 1
        out[key] = {"label": d["label"], "counts": c.most_common(20)}
    return out


def register(app, render):
    @app.get("/issue", response_class=HTMLResponse)
    def issue_today(request: Request):
        d = config.home() / "reports" / "daily"
        days = sorted((p.stem for p in d.glob("*.json")), reverse=True) if d.exists() else []
        return RedirectResponse(f"/issue/{days[0] if days else store.today()}")

    @app.get("/issue/{day}", response_class=HTMLResponse)
    def issue_page(request: Request, day: str):
        try:
            _dt.date.fromisoformat(day)
        except ValueError:
            raise HTTPException(404)
        iss = load_issue(day)
        picks = []
        for p in iss.get("picks") or []:
            m = store.load(p["pid"])
            if m:
                x = _decorate(m)
                x["pick"] = p
                picks.append(x)
        pick_ids = {p["pid"] for p in iss.get("picks") or []}
        new = [_decorate(m) for m in (store.load(pid) for pid in iss.get("new") or []) if m and m["id"] not in pick_ids]
        new.sort(key=lambda x: -((x["score"] or {}).get("total") or 0))
        d = config.home() / "reports" / "daily"
        days = sorted((p.stem for p in d.glob("*.json")), reverse=True)[:30] if d.exists() else []
        progress = []
        for g in iss.get("progress") or []:
            m = store.load(g["pid"])
            if m:
                progress.append({"g": g, "m": m, "qr": store.load_quickread(g["pid"]) or {}})
        return render(request, "issue.html", iss=iss, picks=picks, new=new, days=days, progress=progress,
                      field=fieldmod.field())

    @app.get("/papers", response_class=HTMLResponse)
    def papers_page(request: Request, q: str = "", sort: str = "curated", picked: int = 0,
                    offset: int = 0, limit: int = 50, rated: str = ""):
        f = fieldmod.field()
        allp = [_decorate(m) for m in store.all_papers() if m.get("intake", {}).get("state") == "relevant"]
        sel = {}
        for fv in request.query_params.getlist("f"):
            if "=" in fv:
                k, v = fv.split("=", 1)
                sel.setdefault(k, set()).add(v)
        items = allp
        if q:
            ql = q.lower()
            items = [x for x in items if ql in (x["m"].get("title") or "").lower()
                     or ql in (x["m"].get("abstract") or "").lower()
                     or ql in json.dumps(x["qr"], ensure_ascii=False).lower()]
        if picked:
            items = [x for x in items if x["m"].get("picked")]
        rf = {"any": lambda r: r is not None, "high": lambda r: r is not None and r["score"] >= 4,
              "low": lambda r: r is not None and r["score"] <= 2, "none": lambda r: r is None}.get(rated)
        if rf:
            items = [x for x in items if rf((x["st"] or {}).get("rating"))]
        for k, vs in sel.items():
            items = [x for x in items if vs & {v.split(":")[0].strip() for v in (x["qr"].get("facets") or {}).get(k) or []}]
        counts = _facet_counts(items, f)
        if sort == "latest":
            items.sort(key=lambda x: x["m"].get("published") or "", reverse=True)
        elif sort == "rated":  # 最近评价的在前
            items.sort(key=lambda x: ((x["st"] or {}).get("rating") or {}).get("at") or "", reverse=True)
        else:  # 已整理优先：有精读 / 速读的在前，组内按发表日期
            items.sort(key=lambda x: (x["has_explainer"], bool(x["qr"]), x["m"].get("published") or ""), reverse=True)
        total = len(items)
        return render(request, "papers.html", items=items[offset:offset + limit], total=total, all_total=len(allp),
                      counts=counts, sel={k: sorted(v) for k, v in sel.items()}, q=q, sort=sort, picked=picked,
                      offset=offset, limit=limit, field=f, rated=rated)

    @app.get("/paper/{pid}", response_class=HTMLResponse)
    def paper_page(request: Request, pid: str):
        m = store.load(pid)
        if not m:
            raise HTTPException(404, "没有这篇论文")
        x = _decorate(m)
        seeds = {s["arxiv"]: s.get("title") for s in fieldmod.seeds()}
        return render(request, "paper.html", x=x, seeds=seeds, field=fieldmod.field(), code_check=codecheck.load(pid),
                      code_names=codecheck.STATUS)

    @app.get("/paper/{pid}/explainer")
    def paper_explainer(pid: str):
        p = store.pdir(pid) / "explainer.html"
        if "/" in pid or ".." in pid or not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="text/html")
