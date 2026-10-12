"""跨论文结果表（ADR-0027 → ADR-0034 → ADR-0035）。

  分类（数学 / 代码 / 问答……）→ 每个数据集一张表：行 = 方法，列 = 指标，格子 = 这个方法在这个数据集上报告的成绩。
  不按 backbone 拆（复现时自己跑）；每个数字仍带出处、评测规模（全量 / 子集 / 未注明）、backbone（悬停可见）。
  每个方法标出它自己论文的代码是否真的开源（codecheck.py），方法名按原文核对过（namecheck.py）。

同一方法在同一数据集、同一指标上有多处报告时：优先它自己的论文，其次全量评测，再次数值最高的那条；其余放在悬停提示里。
只汇总，不改写数字（唯一的换算：同一列大多是百分数而这一条写成 0.xx 时乘 100，并注明）。
"""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict

from .. import config
from ..util import now_iso, read_json, write_json
from . import datasets, store

# ---------------- 归一 ----------------

COST_UNITS = {"$", "tokens", "s", "ms", "min", "mins", "h", "k", "m", "m tokens", "usd"}
COST_RX = re.compile(r"cost|latency|token|time|price|usd|dollar|\$|api call|#call|calls|runtime|wall|second|money|expense", re.I)
LOWER_RX = re.compile(r"error|loss|perplexity|\bppl\b|regret|failure rate", re.I)


def canon_metric(raw: str | None, unit: str | None, category: str) -> tuple:
    """→ (规范指标名, 类型 quality|cost, 越大越好?)。"""
    m = re.sub(r"\s+", " ", (raw or "").strip())
    low = m.lower().rstrip(".")
    u = (unit or "").strip().lower()
    if u in COST_UNITS or "usd" in u or "token" in u or COST_RX.search(low):
        return m or "成本", "cost", False
    base = re.sub(r"\(.*?\)|%", "", low).strip(" .")
    if re.fullmatch(r"pass ?@ ?1|pass@1 ?accuracy|pass rate", base):
        name = "Pass@1"
    elif re.fullmatch(r"acc|accuracy|solve rate|solving rate|solved rate|final[- ]answer accuracy|exact accuracy|correct(ness)?( rate)?|"
                      r"answer accuracy|task accuracy|test accuracy|average accuracy|avg\.? accuracy", base):
        name = "Pass@1" if category == "code" else "Accuracy"
    elif re.fullmatch(r"f1|f1 score|f1-score|token f1", base):
        name = "F1"
    elif re.fullmatch(r"em|exact match|exact-match", base):
        name = "EM"
    elif re.fullmatch(r"sr|success rate|success|task success rate|resolve rate|resolved|% resolved|resolved rate", base):
        name = "Resolve Rate" if category == "agent" and "resol" in base else "Success Rate"
    else:
        name = m or "?"
    return name, "quality", not LOWER_RX.search(low)


PCT_METRICS = {"Accuracy", "Pass@1", "F1", "EM", "Success Rate", "Resolve Rate"}


def bb_key(raw: str | None) -> str:
    b = (raw or "").strip().lower()
    b = re.sub(r"^(openai|anthropic|google|meta-llama|qwen|deepseek-ai|mistralai)/", "", b)
    b = re.sub(r"[-_ ]?(20\d\d[-_]?\d\d[-_]?\d\d|\d{4})$", "", b)  # 日期后缀：-2024-07-18 / -0613
    if b in ("", "null", "none", "n/a", "-", "?"):
        return ""
    return re.sub(r"[\s_\-]+", "", b)  # Qwen-3-8B = Qwen3-8B = qwen3 8b


ABLATION_RX = re.compile(r"\bw/o\b|\bwithout\b|ablat|\(-|\bonly\b|\bvariant\b|^\s*[-+]|−|\bremove", re.I)


def base_name(raw: str | None) -> str:
    """方法的基本名：去掉括号里的配置 / backbone / "Ours"。"DMoA (Few-shot + TTT)" → "DMoA"。"""
    b = re.sub(r"\s*[(（\[].*?[)）\]]", "", raw or "").strip()
    b = re.sub(r"\bours\b|[*†‡§]+", "", b, flags=re.I).strip(" -_:")
    return b or (raw or "?").strip()


def m_key(raw: str | None) -> str:
    """同一方法的不同配置并成一行（消融 w/o … 仍是单独一行，默认隐藏）。"""
    return re.sub(r"[\s\-_]+", "", base_name(raw).lower()) or "?"


_own = {}


def owners(rows: list) -> dict:
    """方法 → 它自己的论文（任一 backbone / 数据集上 is_proposed 的最早一篇）。"""
    k = (id(rows), len(rows))
    if _own.get("k") != k:
        o = {}
        for r in rows:
            if r["is_proposed"] and (r["mkey"] not in o or r["published"] < o[r["mkey"]]["published"]):
                o[r["mkey"]] = {"pid": r["pid"], "paper": r["paper"], "published": r["published"], "code": r.get("code")}
        _own.update(k=k, v=o)
    return _own["v"]


# ---------------- 汇总行 ----------------

def index_path():
    return config.home() / "reports" / "results_index.json"


_SUP = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")


def _clean_name(n: str) -> str:
    """显示用：全角括号、LaTeX（$\\mathcal{ANN}$ → ANN，E$^3$ → E³）、多余空白。"""
    n = (n or "?").replace("（", " (").replace("）", ")")
    n = re.sub(r"\\[a-zA-Z]+\{([^{}]*)\}", r"\1", n)
    n = re.sub(r"\^\{?([0-9+\-]+)\}?", lambda m: m.group(1).translate(_SUP), n)
    n = re.sub(r"\\[a-zA-Z]+|[${}]", "", n)
    return re.sub(r"\s+", " ", n).replace("( ", "(").strip() or "?"


def build_rows() -> list:
    from . import codecheck, namecheck

    reg = datasets.registry()
    rows, codes = [], {}
    for m in store.all_papers():
        if m.get("intake", {}).get("state") != "relevant":
            continue
        qr = m.get("_qr") or {}
        evs = {datasets.key(e["task"]): e for e in qr.get("eval_sets") or [] if e.get("task")}
        for r in qr.get("results") or []:
            if not isinstance(r.get("value"), (int, float)) or isinstance(r.get("value"), bool):
                continue
            ds = datasets.lookup(r.get("task"), reg)
            if ds.get("aggregate"):
                continue
            raw = (r.get("method") or "?").strip()
            name, prop, nstat = namecheck.corrected(qr, raw)
            if nstat == "missing":
                continue  # 核对后原文里找不到这个方法：抽错了，不进表
            name = _clean_name(name)
            if nstat == "renamed" and name == _clean_name(raw):
                nstat = "ok"
            ev = evs.get(datasets.key(r.get("task"))) or evs.get(datasets.key(ds["name"]))
            metric, mkind, hib = canon_metric(r.get("metric"), r.get("unit"), ds["category"])
            if metric in PCT_METRICS and not 0 <= r["value"] <= 100:
                continue  # 百分比指标却超出 0–100：多半是题数之类抽错了，不进表
            if m["id"] not in codes:
                c = codecheck.load(m["id"]) or {}
                codes[m["id"]] = {"status": c.get("status") or "unchecked", "url": c.get("url"), "stars": c.get("stars")}
            rows.append({
                "pid": m["id"], "paper": qr.get("title_zh") or m.get("title"), "orig_title": m.get("title"),
                "published": m.get("published") or "", "method": name, "method_raw": raw, "name_status": nstat,
                "mkey": m_key(name), "is_proposed": bool(r.get("is_proposed")) if prop is None else bool(prop),
                "task_raw": (r.get("task") or "").strip(), "dataset": ds["name"],
                "category": ds["category"], "pending": bool(ds.get("pending")), "metric": metric, "metric_raw": (r.get("metric") or "").strip(),
                "kind": mkind, "higher_better": hib, "backbone": (r.get("backbone") or "").strip(), "bkey": bb_key(r.get("backbone")),
                "value": r["value"], "unit": r.get("unit"), "where": r.get("where"), "code": codes[m["id"]],
                "scale": datasets.scale(ev, ds.get("full"), ds.get("variant")), "has_eval": "eval_sets" in qr})
    return rows


def rebuild_index(log=print) -> dict:
    rows = build_rows()
    write_json(index_path(), {"built_at": now_iso(), "rows": rows})
    log(f"结果表索引：{len(rows)} 条，{len({r['pid'] for r in rows})} 篇论文")
    return {"rows": len(rows)}


_idx = {}


def all_rows() -> list:
    """面板 / MCP 用：读流程预先建好的索引（按 mtime 缓存）；还没建过就现算。"""
    p = index_path()
    try:
        mt = os.stat(p).st_mtime_ns
    except OSError:
        return build_rows()
    if _idx.get("mt") != mt:
        rows = (read_json(p, {}) or {}).get("rows") or []
        if rows and "code" not in rows[0]:  # 旧版本的索引：字段不全，现算
            rows = build_rows()
        _idx.update(mt=mt, rows=rows)
    return _idx["rows"]


# ---------------- 论文式对比表 ----------------

def categories(rows: list) -> list:
    c = defaultdict(lambda: {"datasets": set(), "papers": set()})
    for r in rows:
        if r["kind"] == "quality":
            c[r["category"]]["datasets"].add(r["dataset"])
            c[r["category"]]["papers"].add(r["pid"])
    return [{"key": k, "name": n, "n_datasets": len(c[k]["datasets"]), "n_papers": len(c[k]["papers"])}
            for k, n in datasets.CATEGORIES if k in c]


def _scaled_value(r: dict, pct_col: bool) -> tuple:
    v = r["value"]
    if pct_col and r["kind"] == "quality" and r.get("unit") != "%" and 0 <= v <= 1:
        return round(v * 100, 4), True
    return v, False


def _pick_order(rs: list, c: dict) -> list:
    """同一方法、同一格的多处报告排序，第一条显示：方法自己的论文（取其中最好的一条）> 全量评测的转引 > 其它转引；
    转引取中位数那条（不同论文设置不同，取最高值会挑出偶然的高分）。"""
    sgn = -1 if c["hib"] else 1
    val = lambda r: _scaled_value(r, c["pct"])[0]  # noqa: E731
    own = sorted([r for r in rs if r["is_proposed"]], key=lambda r: sgn * val(r))
    rest = [r for r in rs if not r["is_proposed"]]
    pool = [r for r in rest if r["scale"]["status"] == "full"] or rest
    pool = sorted(pool, key=val)
    mid = [pool[(len(pool) - 1) // 2]] if pool else []
    return own + mid + [r for r in rest if r not in mid]


def dataset_table(rows: list, dataset: str, kind: str = "quality", full_only: bool = False, ablations: bool = False,
                  max_metrics: int = 3, own: dict | None = None, min_metric_frac: float = 0.0) -> dict:
    """一个数据集：行 = 方法，列 = 指标（报告最多的几个）。不分 backbone。"""
    sel = [r for r in rows if r["dataset"] == dataset and r["kind"] == kind and (not full_only or r["scale"]["status"] == "full")]
    own = own if own is not None else owners(rows)
    mcount = defaultdict(set)
    for r in sel:
        mcount[r["metric"]].add(r["mkey"])
    metrics = sorted(mcount, key=lambda k: -len(mcount[k]))
    cols = []
    top = len(mcount[metrics[0]]) if metrics else 0
    for met in [m for m in metrics if len(mcount[m]) >= max(1, min_metric_frac * top)][:max_metrics]:
        rs = [r for r in sel if r["metric"] == met]
        vals = sorted(r["value"] for r in rs)
        cols.append({"metric": met, "hib": rs[0]["higher_better"], "pct": vals[len(vals) // 2] > 1.5, "n": len(mcount[met])})
    methods = {}
    for ci, c in enumerate(cols):
        cand = defaultdict(list)
        for r in sel:
            if r["metric"] == c["metric"]:
                cand[r["mkey"]].append(r)
        for mk, rs in cand.items():
            rs = _pick_order(rs, c)
            v, scaled = _scaled_value(rs[0], c["pct"])
            mt = methods.setdefault(mk, {"key": mk, "names": Counter(), "cells": {}, "own": own.get(mk)})
            mt["names"][base_name(rs[0]["method"])] += 1
            mt["cells"][ci] = {"value": v, "scaled": scaled, "r": rs[0],
                               "others": [dict(o, shown=_scaled_value(o, c["pct"])[0]) for o in rs[1:]]}
    out, hidden = [], 0
    for mt in methods.values():
        mt["name"] = mt.pop("names").most_common(1)[0][0]
        mt["ablation"] = bool(ABLATION_RX.search(mt["name"]))
        if mt["ablation"] and not ablations:
            hidden += 1
            continue
        first = mt["cells"].get(0) or next(iter(mt["cells"].values()))
        mt["main"] = first  # 规模 / 出处看主指标那一格
        mt["config"] = first["r"]["method"] if first["r"]["method"] != mt["name"] else None
        out.append(mt)
    for ci, c in enumerate(cols):
        vs = sorted({mt["cells"][ci]["value"] for mt in out if ci in mt["cells"]}, reverse=c["hib"])
        for mt in out:
            cell = mt["cells"].get(ci)
            if cell:
                cell["rank"] = 1 if cell["value"] == vs[0] else (2 if len(vs) > 1 and cell["value"] == vs[1] else 0)
    hib0 = cols[0]["hib"] if cols else True
    out.sort(key=lambda mt: (0 not in mt["cells"], -(mt["cells"][0]["value"]) if 0 in mt["cells"] and hib0
                             else (mt["cells"][0]["value"] if 0 in mt["cells"] else 0), mt["name"].lower()))
    # 评测规模明细（主指标）
    sc = {}
    for r in sel:
        if cols and r["metric"] == cols[0]["metric"]:
            s_ = r["scale"]
            g = sc.setdefault((s_["status"], s_["n"]), {"status": s_["status"], "n": s_["n"], "full": s_["full"], "how": s_["how"], "papers": {}})
            g["how"] = g["how"] or s_["how"]
            g["papers"][r["pid"]] = r["paper"]
    scales = sorted(sc.values(), key=lambda g: ({"full": 0, "subset": 1, "unknown": 2}[g["status"]], -(g["n"] or 0)))
    known = {g["status"] for g in scales} - {"unknown"}
    cat = sel[0]["category"] if sel else next((r["category"] for r in rows if r["dataset"] == dataset), "other")
    return {"dataset": dataset, "category": cat, "cols": cols, "rows": out, "hidden_ablations": hidden,
            "n_papers": len({r["pid"] for r in sel}), "full_size": datasets.FULL.get(dataset), "scales": scales,
            "mixed": len(known) > 1 or len({g["n"] for g in scales if g["status"] == "subset"}) > 1,
            "metrics_all": [{"name": m, "n": len(mcount[m])} for m in metrics]}


def board(rows: list, category: str, kind: str = "quality", full_only: bool = False, ablations: bool = False,
          min_methods: int = 3, max_tables: int = 15) -> dict:
    """一个分类下方法最多的几个数据集各一张表（每张只放主要指标）；其余数据集列成清单，点进去看。"""
    own = owners(rows)
    mk = defaultdict(set)
    for r in rows:
        if r["category"] == category and r["kind"] == kind:
            mk[r["dataset"]].add(r["mkey"])
    names = sorted(mk, key=lambda d: (-len(mk[d]), d))
    big = [d for d in names if len(mk[d]) >= min_methods][:max_tables]
    tables = [dataset_table(rows, d, kind, full_only, ablations, max_metrics=2, own=own, min_metric_frac=0.25) for d in big]
    tables = [t for t in tables if t["rows"]]
    return {"category": category, "kind": kind, "tables": tables, "small": [(d, len(mk[d])) for d in names if d not in big]}


# ---------------- 单个数据集（CLI / MCP / 复现参照）----------------

def norm_task(t: str | None) -> str:
    return datasets.lookup(t)["name"] if t else "?"


def benchmarks(rows: list | None = None) -> list:
    rows = rows if rows is not None else all_rows()
    c = defaultdict(set)
    for r in rows:
        c[(r["dataset"], r["category"])].add(r["pid"])
    return sorted(((t, n, len(p)) for (t, n), p in c.items()), key=lambda x: -x[2])


def table(task: str, rows: list | None = None) -> dict:
    """某个数据集的全部报告：按 (指标, backbone) 分组，组内按数值排序，带评测规模。"""
    rows = [r for r in (rows if rows is not None else all_rows()) if r["dataset"] == task]
    groups = defaultdict(list)
    for r in rows:
        groups[(r["metric"], r["backbone"] or "未注明")].append(r)
    out = []
    for (metric, bb), rs in sorted(groups.items(), key=lambda x: -len(x[1])):
        rs.sort(key=lambda r: -r["value"] if r["higher_better"] else r["value"])
        out.append({"metric": metric, "backbone": bb, "rows": rs})
    return {"task": task, "groups": out, "n_papers": len({r["pid"] for r in rows})}


def reported_for(pid: str, task: str | None = None, metric: str | None = None, backbone: str | None = None) -> list:
    """复现参照：某篇论文自己报告的本方法数字（ADR-0023）。"""
    out = []
    for r in (store.load_quickread(pid) or {}).get("results") or []:
        if not r.get("is_proposed") or not isinstance(r.get("value"), (int, float)):
            continue
        if task and norm_task(r.get("task")) != norm_task(task):
            continue
        if metric and metric.lower() not in (r.get("metric") or "").lower():
            continue
        if backbone and backbone.lower() not in (r.get("backbone") or "").lower():
            continue
        out.append(r)
    return out
