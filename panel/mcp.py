"""只读 MCP（ADR-0027，P6）：Streamable HTTP（无状态 JSON 响应），挂在面板的 /mcp。

接入：
  codex mcp add workbench --url http://<面板地址>/mcp
  claude mcp add --transport http --scope user workbench http://<面板地址>/mcp
只提供查询，不提供任何写操作（写操作只能通过服务器上的 wb，ADR-0011 / 0022）。
"""
from __future__ import annotations

import json
import re

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from workbench import __version__, stats, store as wbstore
from workbench.papers import fieldmap, ideate, reports, results, store
from workbench.papers.pipeline import load_issue

PROTOCOL = "2025-06-18"


def _paper_brief(m, qr):
    return {"id": m["id"], "title": m.get("title"), "title_zh": qr.get("title_zh"), "one_liner": qr.get("one_liner"),
            "published": m.get("published"), "venue": m.get("venue"), "arxiv": m.get("arxiv"),
            "code": m.get("code_urls"), "facets": qr.get("facets"), "picked": m.get("picked")}


def t_search_papers(q: str = "", facet: str = "", picked_only: bool = False, limit: int = 20):
    out = []
    ql = q.lower()
    for m in store.all_papers():
        if m.get("intake", {}).get("state") != "relevant":
            continue
        qr = m.get("_qr") or {}
        if picked_only and not m.get("picked"):
            continue
        if ql and ql not in (m.get("title") or "").lower() and ql not in json.dumps(qr, ensure_ascii=False).lower() \
                and ql not in (m.get("abstract") or "").lower():
            continue
        if facet and "=" in facet:
            k, v = facet.split("=", 1)
            if not any(v in x for x in (qr.get("facets") or {}).get(k) or []):
                continue
        out.append(_paper_brief(m, qr))
    out.sort(key=lambda x: x.get("published") or "", reverse=True)
    return {"total": len(out), "items": out[: max(1, min(int(limit), 100))]}


def t_get_paper(id: str, depth: str = "summary"):
    m = store.load(id)
    if not m:
        return {"error": "not_found"}
    qr = store.load_quickread(id) or {}
    out = {"meta": {k: v for k, v in m.items() if k not in ("abstract",)}, "abstract": m.get("abstract"), "quickread": qr}
    exp = store.pdir(id) / "explainer.html"
    if depth == "full" and exp.exists():
        text = re.sub(r"<script.*?</script>|<style.*?</style>", "", exp.read_text(errors="replace"), flags=re.S)
        out["explainer_text"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:30000]
    return out


def t_get_issue(day: str = ""):
    iss = load_issue(day or store.today())
    return {k: v for k, v in iss.items() if k not in ("candidates",)}


def t_get_map(version: int = 0):
    return fieldmap.load(int(version) or None) or {"error": "还没有领域地图"}


def t_get_results(task: str = "", category: str = "", backbone: str = ""):
    rows = results.all_rows()
    if task:
        return results.table(results.norm_task(task), rows)
    if category:
        bbs = results.backbones(rows, category)
        bk = results.bb_key(backbone) if backbone else (bbs[0]["key"] if bbs else "")
        t = results.matrix(rows, category, bk)
        return {"backbones": bbs[:20], "cols": [{k: c[k] for k in ("dataset", "metric", "n_papers", "full_size", "mixed", "scales")} for c in t["cols"]],
                "rows": [{"method": mt["name"], "own": mt["own"], "cells": {t["cols"][i]["dataset"] + " · " + t["cols"][i]["metric"]:
                          {"value": c["value"], "scale": c["r"]["scale"], "paper": c["r"]["pid"], "where": c["r"]["where"]}
                          for i, c in mt["cells"].items()}} for mt in t["rows"]]}
    return {"categories": results.categories(rows), "benchmarks": results.benchmarks(rows)[:200]}


def t_list_ideas():
    return [{k: v for k, v in i.items() if not k.startswith("_")} for i in ideate.all_ideas()]


def t_get_idea(id: str):
    from workbench.util import read_json
    d = ideate.ideas_dir() / id
    if not d.exists():
        return {"error": "not_found"}
    return {"idea": read_json(d / "idea.json"), "candidate": read_json(d / "candidate.json"),
            "review": read_json(d / "review.json")}


def t_get_report(period: str = "weekly", key: str = ""):
    if period == "hot":
        from workbench.util import read_json
        from workbench import config
        return read_json(config.home() / "reports" / "hot.json") or {}
    if not key:
        keys = reports.list_reports(period)
        if not keys:
            return {"error": "还没有报告"}
        key = keys[0]
    return reports.load_report(period, key) or {"error": "not_found"}


def t_get_project(id: str = ""):
    ps = wbstore.all_projects()
    if not id:
        return [{"id": p["id"], "title": p.get("title")} for p in ps]
    p = wbstore.load_project(id)
    if not p:
        return {"error": "not_found"}
    out = {"project": p, "variants": {}}
    for e in p.get("experiments", []):
        for vid in e.get("variants", []):
            runs = stats.variant_runs(vid)
            out["variants"][vid] = {"aggregate": stats.aggregate(runs),
                                    "warnings": stats.consistency(runs, stats.seed_fields_for(wbstore.load_variant(vid), [p]))}
    return out


TOOLS = {
    "search_papers": (t_search_papers, "搜索论文库（属于研究方向的论文）。可按关键词、维度（facet=key=value）、是否精选过筛选。",
                      {"q": "string", "facet": "string", "picked_only": "boolean", "limit": "integer"}),
    "get_paper": (t_get_paper, "读一篇论文：元数据、速读、抽取的结果；depth=full 时附精读文本。", {"id": "string", "depth": "string"}),
    "get_issue": (t_get_issue, "读某一天的一期（精选、新收录、进展、通过的 IDEA），day 省略为今天。", {"day": "string"}),
    "get_map": (t_get_map, "读领域地图（主分类、每类的代表工作与没考虑到的地方）；version 省略为最新。", {"version": "integer"}),
    "get_results": (t_get_results, "跨论文结果表：不给参数列出分类与数据集；给 category（math/code/qa/knowledge/agent/other）"
                    "和可选 backbone 返回论文式对比表（行 = 方法，列 = 数据集 · 指标，带评测规模）；给 task 返回该数据集的全部报告。",
                    {"task": "string", "category": "string", "backbone": "string"}),
    "list_ideas": (t_list_ideas, "列出告诉过本人的 IDEA。", {}),
    "get_idea": (t_get_idea, "读一个 IDEA：候选内容、审稿意见。", {"id": "string"}),
    "get_report": (t_get_report, "读报告：period=weekly|monthly|hot；key 省略为最新一期。", {"period": "string", "key": "string"}),
    "get_project": (t_get_project, "读本人的论文项目：claim、配置组均值±标准差与一致性警告；id 省略列出全部。", {"id": "string"}),
}


def _schema(props):
    return {"type": "object", "properties": {k: {"type": t} for k, t in props.items()}}


def register(app):
    @app.post("/mcp")
    async def mcp(request: Request):
        try:
            msg = await request.json()
        except Exception:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
        if isinstance(msg, list):
            return JSONResponse([r for r in (_handle(m) for m in msg) if r is not None])
        r = _handle(msg)
        return Response(status_code=202) if r is None else JSONResponse(r)

    @app.get("/mcp")
    def mcp_get():
        return Response(status_code=405)


def _handle(msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notification
        return None
    if method == "initialize":
        res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
               "serverInfo": {"name": "workbench", "version": __version__},
               "instructions": "实验工作台（只读）：论文库、每日一期、领域地图、跨论文结果表、IDEA、周 / 月报、热点、论文项目。写操作请在服务器上用 wb。"}
    elif method == "ping":
        res = {}
    elif method == "tools/list":
        res = {"tools": [{"name": n, "description": d, "inputSchema": _schema(p)} for n, (_, d, p) in TOOLS.items()]}
    elif method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            data = TOOLS[name][0](**(params.get("arguments") or {}))
            res = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)[:200000]}]}
        except TypeError as e:
            res = {"content": [{"type": "text", "text": f"参数错误：{e}"}], "isError": True}
        except Exception as e:  # noqa: BLE001
            res = {"content": [{"type": "text", "text": f"出错：{e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}
