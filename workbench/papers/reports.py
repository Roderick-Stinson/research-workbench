"""周报 / 月报 + 热点榜（ADR-0027，P6）。

reports/hot.json               近 72 小时关注度榜（HF 点赞、HN 讨论、独立来源数）
reports/weekly/<YYYY-Www>.json 周报：本周精选、高分论文、地图变化、通过的 IDEA、本周走向（批量模型写的一段话）
reports/monthly/<YYYY-MM>.json 月报：同上，按月
"""
from __future__ import annotations

import datetime as _dt
import json
import math

from .. import config
from ..util import now_iso, read_json, write_json
from . import fieldmap, llm, select, store
from .pipeline import load_issue


def hot(day: str, hours: int = 72) -> dict:
    d = _dt.date.fromisoformat(day)
    since = (d - _dt.timedelta(days=max(1, hours // 24))).isoformat()
    rows = []
    for m in store.all_papers():
        if m.get("intake", {}).get("state") != "relevant":
            continue
        if (m.get("discovered_day") or "") < since and (m.get("published") or "") < since:
            continue
        att = m.get("attention") or {}
        heat = 40 * min(1, math.log1p(att.get("hf_upvotes") or 0) / math.log1p(80)) + \
            40 * min(1, math.log1p(att.get("hn_points") or 0) / math.log1p(300)) + \
            20 * min(1, (att.get("sources_n") or 1) / 4)
        if heat <= 5:
            continue
        qr = m.get("_qr") or {}
        rows.append({"pid": m["id"], "title": qr.get("title_zh") or m.get("title"), "heat": round(heat),
                     "hf_upvotes": att.get("hf_upvotes") or 0, "hn_points": att.get("hn_points") or 0,
                     "hn_comments": att.get("hn_comments") or 0, "hn_links": att.get("hn_links") or [],
                     "sources_n": att.get("sources_n") or 1, "one_liner": qr.get("one_liner")})
    rows.sort(key=lambda r: -r["heat"])
    from . import taste

    rows = rows[:30]
    for r in rows:
        r["key"], r["kind"], r["text"] = r["pid"], "论文", r.get("one_liner") or ""
    rows = taste.keep(rows, log=lambda *a: None, label="热点")
    for r in rows:
        for k in ("key", "kind", "text"):
            r.pop(k, None)
    out = {"day": day, "window_hours": hours, "generated_at": now_iso(), "items": rows[:20]}
    write_json(config.home() / "reports" / "hot.json", out)
    return out


def _days(start: _dt.date, end: _dt.date):
    d = start
    while d <= end:
        yield d.isoformat()
        d += _dt.timedelta(days=1)


NARR_SYS = """你在为「多智能体架构优化」方向写{kind}的"本期走向"：根据给出的论文（标题、一句话、维度标签）概括这段时间领域在往哪走。
3–6 句中文，具体到方法与现象，提到论文时用 [序号]。只依据给出的内容，不编造。输出 JSON：{{"narrative": "..."}}"""


def build(period: str, key: str | None = None) -> dict:
    today = _dt.date.fromisoformat(store.today())
    if period == "weekly":
        if not key:
            last = today - _dt.timedelta(days=today.weekday() + 7)  # 上周一
            y, w, _ = last.isocalendar()
            key = f"{y}-W{w:02d}"
        y, w = key.split("-W")
        start = _dt.date.fromisocalendar(int(y), int(w), 1)
        end = start + _dt.timedelta(days=6)
    else:
        if not key:
            first = today.replace(day=1) - _dt.timedelta(days=1)
            key = first.strftime("%Y-%m")
        start = _dt.date.fromisoformat(key + "-01")
        end = (start.replace(day=28) + _dt.timedelta(days=4)).replace(day=1) - _dt.timedelta(days=1)
    picks, new, ideas, prog = [], set(), [], []
    for day in _days(start, end):
        p = config.home() / "reports" / "daily" / f"{day}.json"
        if not p.exists():
            continue
        iss = load_issue(day)
        picks += [dict(x, day=day) for x in iss.get("picks") or []]
        new |= set(iss.get("new") or [])
        ideas += iss.get("ideas") or []
        prog += iss.get("progress") or []
    scored = []
    for pid in new | {x["pid"] for x in picks}:
        m = store.load(pid)
        qr = store.load_quickread(pid) or {}
        if m and qr:
            scored.append({"pid": pid, "title": qr.get("title_zh") or m.get("title"), "one_liner": qr.get("one_liner"),
                           "facets": qr.get("facets"), "score": select.score(m, qr)["total"]})
    scored.sort(key=lambda x: -x["score"])
    maps = [v for v in fieldmap.versions() if v.get("created_at") and start.isoformat() <= v["created_at"][:10] <= end.isoformat()]
    rep = {"period": period, "key": key, "start": start.isoformat(), "end": end.isoformat(), "generated_at": now_iso(),
           "n_new": len(new), "n_picks": len(picks), "picks": picks, "top": scored[:15], "ideas": ideas,
           "progress": prog[:50], "map_versions": maps}
    if scored and llm.configured():
        items = [{"i": i + 1, "title": x["title"], "one_liner": x["one_liner"], "facets": x["facets"]}
                 for i, x in enumerate(scored[:40])]
        try:
            res = llm.chat(NARR_SYS.format(kind="周报" if period == "weekly" else "月报"),
                           json.dumps(items, ensure_ascii=False), task=f"report-{period}")
            rep["narrative"] = res.get("narrative")
            rep["narrative_refs"] = [x["pid"] for x in scored[:40]]
        except Exception as e:  # noqa: BLE001
            rep["narrative_error"] = str(e)[:200]
    write_json(config.home() / "reports" / period / f"{key}.json", rep)
    return rep


def list_reports(period: str) -> list:
    d = config.home() / "reports" / period
    return sorted((p.stem for p in d.glob("*.json")), reverse=True) if d.exists() else []


def load_report(period: str, key: str) -> dict | None:
    return read_json(config.home() / "reports" / period / f"{key}.json")
