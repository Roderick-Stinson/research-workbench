"""跨论文结果表（ADR-0027，P3）：把每篇速读里抽取的数字汇成"方法 × benchmark"，每个数字都能点回论文与表号。

只汇总，不改写数字；benchmark 名做归一（大小写、空格、常见别名），指标原样保留。
"""
from __future__ import annotations

import re
from collections import defaultdict

from . import store

ALIASES = {
    "humaneval": "HumanEval", "human eval": "HumanEval", "mbpp": "MBPP", "gsm8k": "GSM8K", "gsm-8k": "GSM8K",
    "math": "MATH", "math-500": "MATH-500", "math500": "MATH-500", "gsm-hard": "GSM-Hard", "gsmhard": "GSM-Hard",
    "multiarith": "MultiArith", "svamp": "SVAMP", "aqua": "AQuA", "mmlu": "MMLU", "gaia": "GAIA",
    "hotpotqa": "HotpotQA", "drop": "DROP", "livecodebench": "LiveCodeBench", "aime": "AIME", "gpqa": "GPQA",
    "swe-bench": "SWE-bench", "swe bench": "SWE-bench", "webshop": "WebShop", "alfworld": "ALFWorld",
}


def norm_task(t: str | None) -> str:
    if not t:
        return "?"
    k = re.sub(r"\s+", " ", t.strip().lower())
    return ALIASES.get(k, ALIASES.get(k.replace(" ", ""), t.strip()))


def all_rows() -> list:
    rows = []
    for m in store.all_papers():
        if m.get("intake", {}).get("state") != "relevant":
            continue
        qr = m.get("_qr") or {}
        for r in qr.get("results") or []:
            if not isinstance(r.get("value"), (int, float)):
                continue
            rows.append({"pid": m["id"], "paper": qr.get("title_zh") or m.get("title"), "published": m.get("published"),
                         "method": (r.get("method") or "?").strip(), "is_proposed": bool(r.get("is_proposed")),
                         "task": norm_task(r.get("task")), "metric": (r.get("metric") or "").strip(),
                         "backbone": (r.get("backbone") or "").strip(), "value": r["value"], "unit": r.get("unit"),
                         "where": r.get("where")})
    return rows


def benchmarks(rows: list | None = None) -> list:
    rows = rows if rows is not None else all_rows()
    c = defaultdict(set)
    for r in rows:
        c[r["task"]].add(r["pid"])
    return sorted(((t, len(p)) for t, p in c.items()), key=lambda x: -x[1])


def table(task: str, rows: list | None = None) -> dict:
    """某个 benchmark 的对比：按 (指标, backbone) 分组，组内按数值降序；同一方法在多篇论文里报告过的都列出。"""
    rows = [r for r in (rows if rows is not None else all_rows()) if r["task"] == task]
    groups = defaultdict(list)
    for r in rows:
        groups[(r["metric"] or "?", r["backbone"] or "未注明")].append(r)
    out = []
    for (metric, bb), rs in sorted(groups.items(), key=lambda x: -len(x[1])):
        rs.sort(key=lambda r: -r["value"])
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
