"""领域地图的"看懂领域"部分（ADR-0041）：发展史、研究热点变化、评测与基线、空白与其它发现。

统计部分全部从论文库 / 结果表算出来（可复核）；叙述部分（各阶段的主线、趋势解读、其它发现）由批量模型依据这些统计和里程碑论文写，
每条都要引用论文 id 或统计数字，不能编。存 map/overview.json，面板读它。

注意采样偏差：论文库对近 12 个月是全量收录，更早的只有顶会顶刊论文，所以热点趋势一律看"占比"而不是篇数，早期时间段样本少、单独标注。
"""
from __future__ import annotations

import datetime as _dt
from collections import Counter, defaultdict

from ..util import now_iso, read_json, write_json
from . import field as fieldmod
from . import fieldmap, llm, results, rigor, store, themes, topvenue

PCT = {"Accuracy", "Pass@1", "F1", "EM", "Success Rate", "Resolve Rate"}
# 饱和度：每半年、每篇论文"本文方法"的最高分，取第 90 百分位（不用最大值——最大值常来自子集或特殊设置）；子集评测不算，少于 3 篇不算
SAT_DATASETS = ["GSM8K", "HumanEval", "MBPP", "MATH", "HotpotQA", "DROP", "MMLU", "GPQA Diamond", "GAIA", "SWE-bench Verified"]


def path():
    return fieldmap.map_dir() / "overview.json"


def load() -> dict:
    return read_json(path(), {}) or {}


def half(d: str) -> str | None:
    if not d or len(d) < 7 or not d[:4].isdigit():
        return None
    return f"{d[:4]}H{1 if int(d[5:7]) <= 6 else 2}"


def _halves(start: str, end: str) -> list:
    y, h = int(start[:4]), int(start[-1])
    out = []
    while f"{y}H{h}" <= end:
        out.append(f"{y}H{h}")
        y, h = (y, 2) if h == 1 else (y + 1, 1)
    return out


def stats() -> dict:
    ms = [m for m in store.all_papers() if m.get("intake", {}).get("state") == "relevant" and m.get("_qr")]
    th = themes.load()
    tnames = {t["id"]: t["name"] for t in th.get("themes") or []}
    today = _dt.date.fromisoformat(store.today())
    cur_h = half(today.isoformat())
    periods = _halves("2023H1", cur_h)
    n_by = Counter(half(m.get("published")) for m in ms)

    # —— 研究主题的占比变化 ——
    t_by = defaultdict(Counter)
    for m in ms:
        h = half(m.get("published"))
        for t in m.get("themes") or []:
            t_by[t][h] += 1
    recent_from = (today - _dt.timedelta(days=182)).isoformat()
    prev_from = (today - _dt.timedelta(days=365)).isoformat()
    rec = [m for m in ms if (m.get("published") or "") >= recent_from]
    prev = [m for m in ms if prev_from <= (m.get("published") or "") < recent_from]
    theme_rows = []
    for tid, name in tnames.items():
        r = sum(1 for m in rec if tid in (m.get("themes") or []))
        p = sum(1 for m in prev if tid in (m.get("themes") or []))
        sr, sp = (r / len(rec) if rec else 0), (p / len(prev) if prev else 0)
        theme_rows.append({"id": tid, "name": name, "recent": r, "prev": p, "share_recent": round(100 * sr, 1),
                           "share_prev": round(100 * sp, 1), "delta": round(100 * (sr - sp), 1),
                           "series": [{"period": h, "share": round(100 * t_by[tid][h] / n_by[h], 1) if n_by[h] else None,
                                       "n": n_by[h]} for h in periods]})
    theme_rows.sort(key=lambda x: -x["share_recent"])

    def facet_series(key):
        c = defaultdict(Counter)
        for m in ms:
            for v in ((m["_qr"].get("facets") or {}).get(key) or []):
                c[v.split(":")[0]][half(m.get("published"))] += 1
        out = []
        for v, by in c.items():
            if v in ("其它",):
                continue
            out.append({"name": v, "series": [{"period": h, "share": round(100 * by[h] / n_by[h], 1) if n_by[h] else None,
                                               "n": n_by[h]} for h in periods],
                        "total": sum(by.values())})
        return sorted(out, key=lambda x: -x["total"])

    # —— 评测与基线 ——
    rows = results.all_rows()
    pub = {m["id"]: (m.get("published") or "") for m in ms}
    std = rigor.standards(rows)
    year_ds, year_bl = defaultdict(Counter), defaultdict(Counter)
    seen_ds, seen_bl = set(), set()
    for r in rows:
        y = pub.get(r["pid"], "")[:4]
        if not y:
            continue
        if (r["pid"], r["dataset"]) not in seen_ds:
            seen_ds.add((r["pid"], r["dataset"]))
            year_ds[y][r["dataset"]] += 1
        if not r["is_proposed"] and r["mkey"] in std["baselines"] and (r["pid"], r["mkey"]) not in seen_bl:
            seen_bl.add((r["pid"], r["mkey"]))
            year_bl[y][std["baselines"][r["mkey"]]] += 1
    must_compare = Counter()
    for (pid, mk) in seen_bl:
        if pub.get(pid, "") >= prev_from:
            must_compare[std["baselines"][mk]] += 1
    n_recent_method = sum(1 for m in ms if (m.get("published") or "") >= prev_from
                          and "方法" in ((m["_qr"].get("facets") or {}).get("kind") or []))
    sat = []
    for ds in SAT_DATASETS:
        vals = defaultdict(lambda: defaultdict(list))  # 指标 → 半年 → [(值, 论文)]
        for r in rows:
            if r["dataset"] != ds or r["metric"] not in PCT or not r["is_proposed"] or not 0 <= r["value"] <= 100:
                continue
            if r["scale"]["status"] == "subset":
                continue  # 子集 / 抽样评测的分数不可比，不算
            h = half(pub.get(r["pid"], ""))
            if h:
                vals[r["metric"]][h].append((r["value"], r["pid"]))
        if not vals:
            continue
        met = max(vals, key=lambda k: sum(len(v) for v in vals[k].values()))
        series = []
        for h in periods:
            xs = sorted({pid: v for v, pid in sorted(vals[met][h])}.items(), key=lambda x: x[1])  # 每篇论文取最高
            if len(xs) >= 3:
                p90 = xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]
                series.append({"period": h, "value": round(p90[1], 1), "pid": p90[0], "n": len(xs)})
        if series:
            solid = [x for x in series if x["n"] >= 8]  # 论文太少的半年不下结论
            last = solid[-1] if solid else None
            sat.append({"dataset": ds, "metric": met, "series": series, "best": last["value"] if last else None,
                        "best_period": last["period"] if last else None, "saturated": bool(last and last["value"] >= 95)})

    # —— 空白：主题 × 搜索方式（近 12 个月）——
    grid = defaultdict(Counter)
    for m in ms:
        if (m.get("published") or "") < prev_from:
            continue
        for t in (m.get("themes") or [])[:1]:
            for v in ((m["_qr"].get("facets") or {}).get("search") or []):
                v = v.split(":")[0]
                if v not in ("其它",):
                    grid[t][v] += 1
    search_vals = [v for v in (fieldmod.field()["facets"].get("search") or {}).get("values", []) if v != "其它"]
    return {
        "periods": [{"period": h, "n": n_by[h], "low": n_by[h] < 30} for h in periods],
        "themes": theme_rows, "themes_version": th.get("version"),
        "search": facet_series("search"), "granularity": facet_series("granularity"),
        "benchmarks_by_year": {y: c.most_common(8) for y, c in sorted(year_ds.items())},
        "baselines_by_year": {y: c.most_common(8) for y, c in sorted(year_bl.items())},
        "must_compare": must_compare.most_common(12), "n_recent_method": n_recent_method,
        "saturation": sat,
        "whitespace": {"themes": [{"id": t["id"], "name": t["name"]} for t in theme_rows if t["id"] != "other"],
                       "search": search_vals, "counts": {t: dict(c) for t, c in grid.items()}},
        "n_papers": len(ms), "recent_from": recent_from, "prev_from": prev_from,
    }


SYS = """你在为研究方向「{name}」写领域地图里"看懂这个领域"的部分，读者是做这个方向、想发 CCF-A 的研究者。
只能依据给出的里程碑论文与统计数字，不要编造论文、年份或结论；提到论文时用 [论文id]，提到统计时写出数字。

输出 JSON：
{{
 "overview": "整个领域从哪来、现在在哪、往哪走（3–4 句）",
 "eras": [{{"period": "如 2023、2024H1、2025–2026", "title": "这一阶段的主线（≤ 16 字）",
            "shift": "这一阶段相对上一阶段变了什么、为什么（2–4 句，引用里程碑）",
            "landmarks": ["论文id"],
            "notes": {{"论文id": "一句话：它做了什么、在谁的基础上（看 compares_with）"}}}}],
 "trend_notes": [{{"theme": "主题 id", "note": "一句话解读这个主题为什么升 / 降，引用论文或数字"}}],
 "insights": [{{"title": "一句话结论", "detail": "2–3 句，说明对他的研究有什么用（选题、实验设计、投稿时要注意什么）", "evidence": ["论文id 或 统计项"]}}]
}}
eras 3–5 个，按时间先后；每个阶段 3–8 篇里程碑（只能用给出的 id）。trend_notes 只写变化最明显的 4–8 个主题。
insights 4–7 条，写他自己可能没想到、但对研究有用的东西，例如：投稿时现在必须比的 baseline、已经接近饱和的 benchmark、
少有人做的方向组合（看空白矩阵）、领域里有争议 / 互相矛盾的结论、评测设置的通行做法和常见陷阱。
每条都必须有给出的统计或论文支撑，不要写没有依据的判断（例如"审稿人越来越关注……"）；饱和度只用 best（论文数 ≥ 8 的半年）下结论，
best 为空的数据集不要说饱和或不饱和。结合他的口味（taste）：他更关注提升效果的方法，不喜欢以省 token / 省钱为主要贡献的工作。"""


def landmarks(rows: list) -> list:
    """里程碑：必读清单 + 领域常用方法的原论文；带 compares_with（它对比过哪些更早的里程碑方法）。"""
    d = topvenue.load()
    own = topvenue.own_keys(rows)
    std = rigor.standards(rows)
    keys = {}
    for x in d.get("must") or []:
        keys[x["pid"]] = x
    for pid, mks in own.items():
        if mks & set(std["baselines"]) and pid not in keys:
            m = store.load(pid) or {}
            qr = store.load_quickread(pid) or {}
            keys[pid] = {"pid": pid, "title": m.get("title"), "title_zh": qr.get("title_zh"), "one_liner": qr.get("one_liner"),
                         "date": m.get("published") or "", "venue": m.get("top_venue") or {}}
    name_of = {}
    for pid in keys:
        for k in own.get(pid, set()):
            name_of[k] = pid
    cmp = defaultdict(set)
    for r in rows:
        if r["pid"] in keys and not r["is_proposed"] and name_of.get(r["mkey"]) and name_of[r["mkey"]] != r["pid"]:
            cmp[r["pid"]].add(name_of[r["mkey"]])
    out = []
    for pid, x in keys.items():
        out.append({"id": pid, "date": (x.get("date") or "")[:7], "venue": (x.get("venue") or {}).get("name"),
                    "title": x.get("title"), "one_liner": x.get("one_liner"),
                    "compares_with": sorted(cmp[pid], key=lambda p: keys[p].get("date") or "")})
    out.sort(key=lambda x: x["date"])
    return out


def build(log=print, narrative: bool = True) -> dict:
    st = stats()
    rows = results.all_rows()
    lms = landmarks(rows)
    out = {"built_at": now_iso(), "stats": st, "landmarks": lms}
    old = load()
    if narrative:
        f = fieldmod.field()
        brief = {
            "landmarks": [{k: x[k] for k in ("id", "date", "venue", "title", "one_liner", "compares_with")} for x in lms],
            "theme_trends": [{k: t[k] for k in ("id", "name", "share_prev", "share_recent", "delta", "recent")} for t in st["themes"]],
            "search_trend": [{"name": s["name"], "series": [(p["period"], p["share"]) for p in s["series"] if p["share"] is not None]} for s in st["search"][:8]],
            "benchmarks_by_year": st["benchmarks_by_year"], "baselines_by_year": st["baselines_by_year"],
            "must_compare_last_12_months": st["must_compare"], "n_recent_method_papers": st["n_recent_method"],
            "saturation": [{k: s[k] for k in ("dataset", "metric", "best", "best_period", "saturated")} for s in st["saturation"]],
            "taste": fieldmod.field().get("taste"),
            "whitespace_last_12_months": st["whitespace"]["counts"],
            "sampling_note": "近 12 个月全量收录，更早的只有顶会顶刊论文；early periods have small n: "
                             + str([(p["period"], p["n"]) for p in st["periods"]]),
        }
        import json
        try:
            res = llm.chat(SYS.format(name=f["name"]), json.dumps(brief, ensure_ascii=False), task="map-overview", max_tokens=16000)
            valid = {x["id"] for x in lms}
            for e in res.get("eras") or []:
                e["landmarks"] = [p for p in e.get("landmarks") or [] if p in valid]
            out.update({k: res.get(k) for k in ("overview", "eras", "trend_notes", "insights")})
        except Exception as e:  # noqa: BLE001  叙述没写成就保留上一版叙述，统计照常更新
            log(f"地图叙述没更新：{e}")
            out.update({k: old.get(k) for k in ("overview", "eras", "trend_notes", "insights")})
            out["narrative_error"] = str(e)[:300]
    else:
        out.update({k: old.get(k) for k in ("overview", "eras", "trend_notes", "insights")})
    write_json(path(), out)
    log(f"领域地图（发展史 / 热点 / 评测与基线 / 空白）：{len(lms)} 个里程碑，{len(st['themes'])} 个主题")
    return {"landmarks": len(lms), "themes": len(st["themes"]), "narrative": bool(out.get("eras"))}
