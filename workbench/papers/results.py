"""跨论文结果表（ADR-0027，ADR-0034 改版）：把每篇速读里抽取的数字汇成论文式的对比表。

  分类（数学 / 代码 / 问答……）→ backbone → 表：行 = 方法，列 = 数据集 · 指标，格子 = 数值；每列最好加粗、第二下划线。
  每个数字都带评测规模（全量 / 抽样或子集 / 未注明），能点回论文与表号。

只汇总，不改写数字（唯一的换算：同一列大多是百分数而这一条写成 0.xx 时乘 100，并在提示里注明）。
数据集名归一与分类见 datasets.py；指标、backbone、方法名在这里做轻量归一，原始写法保留在提示里。
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


def bb_key(raw: str | None) -> str:
    b = (raw or "").strip().lower()
    b = re.sub(r"^(openai|anthropic|google|meta-llama|qwen|deepseek-ai|mistralai)/", "", b)
    b = re.sub(r"[-_ ]?(20\d\d[-_]?\d\d[-_]?\d\d|\d{4})$", "", b)  # 日期后缀：-2024-07-18 / -0613
    if b in ("", "null", "none", "n/a", "-", "?"):
        return ""
    return re.sub(r"[\s_\-]+", "", b)  # Qwen-3-8B = Qwen3-8B = qwen3 8b


ABLATION_RX = re.compile(r"\bw/o\b|\bwithout\b|ablat|\(-|\bonly\b|\bvariant\b|^\s*[-+]|−|\bremove", re.I)
# 方法名后面的括号里如果只是 backbone / 样本数，就不算另一个方法
PAREN_NOISE = re.compile(r"\(\s*(ours?|\d+[- ]?shots?|(gpt|claude|qwen|llama|deepseek|gemini|mistral|o\d)[^)]*)\s*\)", re.I)


def m_key(raw: str | None) -> str:
    m = PAREN_NOISE.sub("", (raw or "").lower())
    m = re.sub(r"\bours\b|[*†‡§]+", "", m)
    return re.sub(r"[\s\-_]+", "", m).strip() or "?"


_own = {}


def owners(rows: list) -> dict:
    """方法 → 它自己的论文（任一 backbone / 数据集上 is_proposed 的最早一篇）。"""
    k = (id(rows), len(rows))
    if _own.get("k") != k:
        o = {}
        for r in rows:
            if r["is_proposed"] and (r["mkey"] not in o or r["published"] < o[r["mkey"]]["published"]):
                o[r["mkey"]] = {"pid": r["pid"], "paper": r["paper"], "published": r["published"]}
        _own.update(k=k, v=o)
    return _own["v"]


# ---------------- 汇总行 ----------------

def index_path():
    return config.home() / "reports" / "results_index.json"


def build_rows() -> list:
    reg = datasets.registry()
    rows = []
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
            ev = evs.get(datasets.key(r.get("task"))) or evs.get(datasets.key(ds["name"]))
            metric, mkind, hib = canon_metric(r.get("metric"), r.get("unit"), ds["category"])
            rows.append({
                "pid": m["id"], "paper": qr.get("title_zh") or m.get("title"), "orig_title": m.get("title"),
                "published": m.get("published") or "", "method": (r.get("method") or "?").strip(), "mkey": m_key(r.get("method")),
                "is_proposed": bool(r.get("is_proposed")), "task_raw": (r.get("task") or "").strip(), "dataset": ds["name"],
                "category": ds["category"], "pending": bool(ds.get("pending")), "metric": metric, "metric_raw": (r.get("metric") or "").strip(),
                "kind": mkind, "higher_better": hib, "backbone": (r.get("backbone") or "").strip(), "bkey": bb_key(r.get("backbone")),
                "value": r["value"], "unit": r.get("unit"), "where": r.get("where"),
                "scale": datasets.scale(ev, ds.get("full")), "has_eval": "eval_sets" in qr})
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
        _idx.update(mt=mt, rows=(read_json(p, {}) or {}).get("rows") or [])
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


def backbones(rows: list, category: str, kind: str = "quality") -> list:
    c = defaultdict(lambda: {"cells": 0, "papers": set(), "names": Counter()})
    for r in rows:
        if r["category"] == category and r["kind"] == kind:
            x = c[r["bkey"]]
            x["cells"] += 1
            x["papers"].add(r["pid"])
            x["names"][r["backbone"] or "未注明"] += 1
    out = [{"key": k, "name": v["names"].most_common(1)[0][0] if k else "未注明", "cells": v["cells"], "papers": len(v["papers"])}
           for k, v in c.items()]
    out.sort(key=lambda x: (x["key"] == "", -x["papers"], -x["cells"]))
    return out


def _scaled_value(r: dict, pct_col: bool) -> tuple:
    v = r["value"]
    if pct_col and r["kind"] == "quality" and r.get("unit") != "%" and 0 <= v <= 1:
        return round(v * 100, 4), True
    return v, False


def pivot(sel: list, col_of, min_papers: int = 2, max_cols: int = 16, ablations: bool = False, col_order=None,
          own: dict | None = None) -> dict:
    """行 = 方法，列 = col_of(r) 的取值。同一方法在同一列多处报告时：优先它自己的论文，其次全量评测，再次最新的论文。"""
    by_col = defaultdict(list)
    for r in sel:
        by_col[col_of(r)].append(r)
    cols = [{"key": k, "n_papers": len({r["pid"] for r in rs}), "rows": rs} for k, rs in by_col.items()]
    cols = [c for c in cols if c["n_papers"] >= min_papers]
    cols.sort(key=col_order or (lambda c: -c["n_papers"]))
    hidden_cols = max(0, len(cols) - max_cols)
    cols = cols[:max_cols]

    methods = {}
    for ci, c in enumerate(cols):
        vals = sorted(r["value"] for r in c["rows"])
        pct_col = vals[len(vals) // 2] > 1.5
        c["hib"] = c["rows"][0]["higher_better"]
        cand = defaultdict(list)
        for r in c["rows"]:
            cand[r["mkey"]].append(r)
        for mk, rs in cand.items():
            rs = sorted(rs, key=lambda r: (not r["is_proposed"], r["scale"]["status"] != "full", _neg_date(r["published"])))
            v, scaled = _scaled_value(rs[0], pct_col)
            mt = methods.setdefault(mk, {"key": mk, "names": Counter(), "cells": {}, "own": None})
            mt["names"][rs[0]["method"]] += 1
            mt["cells"][ci] = {"value": v, "scaled": scaled, "r": rs[0],
                               "others": [dict(o, shown=_scaled_value(o, pct_col)[0]) for o in rs[1:]]}
        # 评测规模：这一列里各论文用的什么设置
        sc = {}
        for r in c["rows"]:
            s = r["scale"]
            g = sc.setdefault((s["status"], s["n"]), {"status": s["status"], "n": s["n"], "full": s["full"], "how": s["how"], "papers": {}})
            g["how"] = g["how"] or s["how"]
            g["papers"][r["pid"]] = r["paper"]
        c["scales"] = sorted(sc.values(), key=lambda g: ({"full": 0, "subset": 1, "unknown": 2}[g["status"]], -(g["n"] or 0)))
        known = {g["status"] for g in c["scales"]} - {"unknown"}
        c["mixed"] = len(known) > 1 or len({g["n"] for g in c["scales"] if g["status"] == "subset"}) > 1
        del c["rows"]

    for mk, mt in methods.items():
        mt["own"] = (own or {}).get(mk)
    rows, hidden_abl = [], 0
    for mt in methods.values():
        mt["name"] = mt.pop("names").most_common(1)[0][0]
        mt["ablation"] = bool(ABLATION_RX.search(mt["name"]))
        if mt["ablation"] and not ablations:
            hidden_abl += 1
        else:
            rows.append(mt)

    for ci, c in enumerate(cols):  # 每列最好 / 第二
        vs = sorted({mt["cells"][ci]["value"] for mt in rows if ci in mt["cells"]}, reverse=c["hib"])
        for mt in rows:
            cell = mt["cells"].get(ci)
            if cell:
                cell["rank"] = 1 if cell["value"] == vs[0] else (2 if len(vs) > 1 and cell["value"] == vs[1] else 0)

    # 行序（像论文一样）：通用基线（库里没有它自己的论文）在上，按覆盖列数；有出处的方法按发表时间
    rows.sort(key=lambda mt: (mt["own"] is not None, -len(mt["cells"]) if mt["own"] is None else 0,
                              (mt["own"] or {}).get("published") or "", mt["name"].lower()))
    return {"cols": cols, "rows": [mt for mt in rows if mt["cells"]], "hidden_cols": hidden_cols, "hidden_ablations": hidden_abl,
            "n_papers": len({r["pid"] for r in sel})}


def matrix(rows: list, category: str, bkey: str, kind: str = "quality", full_only: bool = False,
           ablations: bool = False, single: bool = False, max_cols: int = 16) -> dict:
    """分类 × backbone 的论文式对比表：列 = 数据集 · 指标。"""
    sel = [r for r in rows if r["category"] == category and r["kind"] == kind and r["bkey"] == bkey
           and (not full_only or r["scale"]["status"] == "full")]
    pop = Counter()
    for r in sel:
        pop[r["dataset"]] += 1
    t = pivot(sel, lambda r: (r["dataset"], r["metric"]), 1 if single else 2, max_cols, ablations,
              col_order=lambda c: (-pop[c["key"][0]], c["key"][0], -c["n_papers"]), own=owners(rows))
    for c in t["cols"]:
        c["dataset"], c["metric"] = c["key"]
        c["full_size"] = datasets.FULL.get(c["dataset"]) or next(({"split": None, "n": g["full"]} for g in c["scales"] if g["full"]), None)
    return dict(t, category=category, bkey=bkey, kind=kind, full_only=full_only)


def dataset_view(rows: list, dataset: str, metric: str | None = None, full_only: bool = False, ablations: bool = False) -> dict:
    """单个数据集：选一个指标，行 = 方法，列 = backbone。"""
    mine = [r for r in rows if r["dataset"] == dataset]
    mets = Counter((r["metric"], r["kind"]) for r in mine)
    metrics = [{"name": m, "kind": k, "n": n} for (m, k), n in sorted(mets.items(), key=lambda x: (x[0][1] != "quality", -x[1]))]
    metric = metric or (metrics[0]["name"] if metrics else None)
    sel = [r for r in mine if r["metric"] == metric and (not full_only or r["scale"]["status"] == "full")]
    names = defaultdict(Counter)
    for r in sel:
        names[r["bkey"]][r["backbone"] or "未注明"] += 1
    t = pivot(sel, lambda r: r["bkey"], 1, 12, ablations, col_order=lambda c: (c["key"] == "", -c["n_papers"]), own=owners(rows))
    for c in t["cols"]:
        c["backbone"] = names[c["key"]].most_common(1)[0][0] if c["key"] else "未注明"
    category = mine[0]["category"] if mine else "other"
    return dict(t, dataset=dataset, metric=metric, metrics=metrics, category=category, full_size=datasets.FULL.get(dataset),
                full_only=full_only)


def _neg_date(d: str) -> str:
    """日期降序排序用：把 '2025-03-01' 变成可升序比较的反序串。"""
    return "".join(chr(0x7f - ord(ch)) for ch in (d or ""))


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
