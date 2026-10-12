"""精选（ADR-0020 / ADR-0027）：分数 = 质量 + 关注度 + 与种子的关系 + 代码 / 录用，按质量门槛最多 10 篇。"""
from __future__ import annotations

import datetime as _dt
import math

from . import field as fieldmod
from . import store

W = {"quality": 0.55, "attention": 0.20, "seed": 0.15, "extra": 0.10}


def quality_of(qr: dict | None) -> float | None:
    q = (qr or {}).get("quality") or {}
    xs = [q.get(k) for k in ("novelty", "rigor", "significance") if isinstance(q.get(k), (int, float))]
    return sum(xs) / len(xs) if xs else None


def score(meta: dict, qr: dict | None) -> dict:
    q = quality_of(qr)
    att = meta.get("attention") or {}
    a = 0.6 * min(1.0, math.log1p(att.get("hf_upvotes") or 0) / math.log1p(50)) + \
        0.4 * min(1.0, math.log1p(att.get("hn_points") or 0) / math.log1p(200))
    s = min(1.0, len(meta.get("cites_seeds") or []) / 2)
    e = 0.5 * bool(meta.get("code_urls") or (qr or {}).get("code_url")) + \
        0.5 * bool(meta.get("venue") or (qr or {}).get("venue_claim"))
    total = W["quality"] * ((q or 0) / 10) + W["attention"] * a + W["seed"] * s + W["extra"] * e
    return {"total": round(total, 4), "quality": q, "attention": round(a, 3), "seed": s, "extra": e}


def eligible(meta: dict, day: str) -> bool:
    """可进当期精选：属于方向、有速读、没被选过、不是回填进来的旧论文、发表在窗口内。"""
    if meta.get("intake", {}).get("state") != "relevant" or meta.get("picked"):
        return False
    if (store.load_status(meta["id"]) or {}).get("irrelevant"):
        return False
    win = fieldmod.cfg_selection().get("pick_window_days", 90)
    pub = (meta.get("published") or "")[:10]
    try:
        age = (_dt.date.fromisoformat(day) - _dt.date.fromisoformat(pub)).days if len(pub) == 10 else None
    except ValueError:
        age = None
    if meta.get("backfill") and (age is None or age > 14):
        return False
    return age is None or age <= win


def pick(day: str, already: list | None = None) -> list:
    """返回 [{pid, score...}]，按分数降序；已在本期的不重复选。"""
    sel = fieldmod.cfg_selection()
    max_n, min_q = sel.get("max_per_day", 10), sel.get("min_quality", 6.0)
    already = list(already or [])
    room = max_n - len(already)
    if room <= 0:
        return []
    cands = []
    for m in store.all_papers(with_quickread=True):
        if m["id"] in already or not eligible(m, day):
            continue
        qr = m.get("_qr")
        if not qr:
            continue
        sc = score(m, qr)
        if sc["quality"] is None or sc["quality"] < min_q:
            continue
        cands.append({"pid": m["id"], **sc})
    cands.sort(key=lambda x: -x["total"])
    return cands[:room]
