"""收录（ADR-0027 第 1 条）：

A. 相关性判定：只判"是否属于研究方向"，不评好坏。一批 30 篇标题 + 摘要，一次调用。
B. 速读：对属于方向的论文读全文，产出 中文发现式标题 / 一句话 / 速读四段 / 实验设置 / 结果抽取 / 维度标签 / 质量分。
   所有数字只能来自原文，不确定就不写（"不可以猜"）。
"""
from __future__ import annotations

import concurrent.futures as cf

from ..util import now_iso
from . import field as fieldmod
from . import datasets, fulltext, llm, store

SYS_RELEVANCE = """你是研究助理，负责判断论文是否属于下面这个研究方向。只判断"是否属于方向"，不评价论文好坏。

研究方向：{name}
{description}
子主题：{subtopics}
种子论文（一定属于方向）：{seeds}
本人标为"不相关"的论文（反例：和这些同类的判为不属于，除非核心贡献明显不同）：{negatives}

判断标准：
- 属于：论文的核心贡献是设计 / 搜索 / 生成 / 优化 / 编排 多智能体或 agentic 系统的结构（workflow、拓扑、角色、协作方式、路由），
  或者是这类系统的评测、失败分析、成本分析、理论分析。
- 不属于：只是把一个现成的多智能体框架用在某个应用领域（医疗、金融、机器人……）而没有对结构本身做方法贡献；
  单智能体工具使用 / RAG / 推理方法（除非明确讨论多智能体或 workflow 结构）；多智能体强化学习（MARL）中与 LLM 无关的工作。
输出 JSON：{{"items": [{{"i": 序号, "relevant": true|false, "score": 0到1的把握, "reason": "一句中文理由"}}]}}，每篇都要有。"""

SYS_QUICKREAD = """你是严谨的研究助理，为"{name}"方向的研究者整理论文速读。用中文写，专有名词、方法名、数据集名、指标名保留英文。

硬性规则：
1. 所有数字、结论只能来自给出的论文原文；原文没有的就不写，**不可以猜**。不确定的数值宁可省略。
2. 每段 1–3 句，信息密度高，不写空话。
3. results 只抽取原文表格 / 正文里明确报告的数值，并写明出处（Table 2 / Fig. 3 / Sec 4.2）。

输出 JSON，字段：
{{
 "title_zh": "中文标题，写成'发现 / 结论'式，带关键数字（如：XX 用 YY 把 ZZ 提升 3.2 点）",
 "one_liner": "一句话：做了什么 + 最关键的结果",
 "quickread": {{"problem": "要解决的问题", "method": "怎么做", "results": "实验与结果（带原文数字）", "limitations": "局限与可以继续做的"}},
 "setup": {{"tasks": ["benchmark / 数据集"], "backbones": ["用到的 LLM"], "metrics": ["指标"], "baselines": ["对比方法"]}},
 "results": [{{"method": "方法名", "is_proposed": true|false, "task": "benchmark", "metric": "指标", "backbone": "LLM 或 null",
              "value": 数值, "unit": "%|$|tokens|s|其它或 null", "where": "Table 2"}}],
 {eval_field},
 "facets": {{{facet_schema}}},
 "quality": {{"novelty": 1-10, "rigor": 1-10, "significance": 1-10, "reason": "两三句：新在哪、实验是否充分（基线、消融、种子数）、是否只是 prompt 层面的增量"}},
 "code_url": "原文给出的代码链接或 null",
 "venue_claim": "原文自称的录用会议（如 'ICLR 2026'）或 null"
}}
results 最多 60 条，优先：本文方法与最强 baseline 在主表上的数字、成本数字。
eval_sets：results 里出现的每个数据集各一条——用的哪个划分、实际评测了多少题、是完整划分还是抽样 / 子集、怎么选的。
只写原文明确说的，没写就填 null，不要用你自己知道的数据集规模去补。"""


def _negative_titles(limit: int = 30) -> str:
    """本人标为"不相关"的论文（ADR-0033）：作为相关性判定的反例。"""
    rows = []
    for m in store.all_papers():
        st = m.get("_status") or {}
        if st.get("irrelevant"):
            rows.append((st["irrelevant"], m.get("title") or m["id"], st.get("note")))
    rows.sort(reverse=True)
    if not rows:
        return "（暂无）"
    return "；".join(f"{t}" + (f"（原因：{n}）" if n else "") for _, t, n in rows[:limit])


def _seed_titles() -> str:
    return "；".join(s.get("title") or s["arxiv"] for s in fieldmod.seeds())


def judge_relevance(metas: list, batch: int = 30, workers: int = 4, kind: str = "daily") -> dict:
    """{pid: {relevant, score, reason}}；分批并行。"""
    f = fieldmod.field()
    sys = SYS_RELEVANCE.format(name=f["name"], description=f["description"], subtopics="、".join(f["subtopics"]),
                               seeds=_seed_titles(), negatives=_negative_titles())
    chunks = [metas[i:i + batch] for i in range(0, len(metas), batch)]

    def one(chunk):
        lines = [f"[{j}] {m.get('title')}\n{(m.get('abstract') or '')[:1500]}" for j, m in enumerate(chunk)]
        res = llm.chat(sys, "\n\n".join(lines), task="relevance", max_tokens=4096, kind=kind)
        out = {}
        for it in res.get("items") or []:
            try:
                m = chunk[int(it["i"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            out[m["id"]] = {"relevant": bool(it.get("relevant")), "score": it.get("score"), "reason": it.get("reason")}
        return out

    out = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for fu in cf.as_completed([ex.submit(one, c) for c in chunks]):
            try:
                out.update(fu.result())
            except (llm.LLMNotConfigured, llm.LLMPaused):
                raise
            except Exception:  # noqa: BLE001  这一批失败：保持 candidate，下次再判
                pass
    return out


def facet_schema(f: dict) -> str:
    parts = []
    for k, d in f["facets"].items():
        parts.append(f'"{k}": ["从这些里选，可多选；都不合适写 其它:简短描述"]  /* {d["label"]}：{" / ".join(d["values"])} */')
    return ", ".join(parts)


def normalize_facets(facets: dict, f: dict) -> dict:
    """把模型给的维度值对齐到允许的取值；对不上的写成 "其它:原值"（ADR-0021：取值集合由地图调整）。"""
    out = {}
    for k, d in f["facets"].items():
        allowed = d["values"]
        vals = []
        for v in (facets or {}).get(k) or []:
            v = str(v).strip()
            if v in allowed:
                hit = v
            else:
                core = v.split(":", 1)[-1].strip() if v.startswith(("其它", "其他")) else v
                hit = next((a for a in allowed if core and (core in a or a.split(" ")[0] in core)), None) or f"其它:{core}"
            if hit not in vals:
                vals.append(hit)
        if vals:
            out[k] = vals
    return out


def quickread(meta: dict, kind: str = "daily") -> dict:
    f = fieldmod.field()
    if llm.creds().get("WB_LLM_PROTOCOL") == "fake":
        text, basis = f"{meta.get('title')}\n\n{meta.get('abstract')}", "abstract"
    else:
        text, basis = fulltext.fetch(meta)
    sys = SYS_QUICKREAD.format(name=f["name"], facet_schema=facet_schema(f), eval_field=datasets.EVAL_FIELD)
    user = f"标题：{meta.get('title')}\n作者：{', '.join(meta.get('authors') or [])[:400]}\n\n=== 原文（{basis}）===\n{text}"
    res = llm.chat(sys, user, task="quickread", max_tokens=8192, kind=kind)
    res["facets_raw"] = res.get("facets")
    res["facets"] = normalize_facets(res.get("facets"), f)
    if res.get("eval_sets"):
        res["eval_sets"] = datasets.norm_eval_sets(res["eval_sets"])
        res["eval_sets_at"] = now_iso()
    else:
        res.pop("eval_sets", None)  # 没抽到：留给 datasets.fill() 补
    res["basis"] = basis
    res["model_task"] = "quickread"
    res["generated_at"] = now_iso()
    return res


def run_relevance(pids: list, log=print, kind: str = "daily") -> int:
    metas = [m for m in (store.load(p) for p in pids) if m and m.get("intake", {}).get("state") == "candidate"]
    if not metas:
        return 0
    res = judge_relevance(metas, kind=kind)
    n = 0
    for m in metas:
        r = res.get(m["id"])
        if r is None:
            continue
        m["intake"] = {"state": "relevant" if r["relevant"] else "irrelevant", "relevance": r["score"],
                       "reason": r["reason"], "judged_at": now_iso()}
        store.save(m)
        n += r["relevant"]
    log(f"相关性判定 {len(metas)} 篇，属于方向 {n} 篇")
    return n


def run_quickreads(pids: list, workers: int | None = None, log=print, kind: str = "daily") -> dict:
    """对 relevant 且还没速读的论文做速读。返回 {ok, failed}。"""
    todo = []
    for p in pids:
        m = store.load(p)
        if m and m.get("intake", {}).get("state") == "relevant" and not store.load_quickread(p):
            todo.append(m)
    ok, failed = 0, []
    workers = workers or (fieldmod.field().get("intake") or {}).get("workers", 6)

    def one(m):
        qr = quickread(m, kind=kind)
        store.save_quickread(m["id"], qr)
        if qr.get("code_url") and qr["code_url"] not in m.setdefault("code_urls", []):
            m["code_urls"].append(qr["code_url"])
            store.save(m)
        return m["id"]

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, m): m["id"] for m in todo}
        for fu in cf.as_completed(futs):
            try:
                fu.result()
                ok += 1
            except (llm.LLMNotConfigured, llm.LLMPaused):
                for f2 in futs:
                    f2.cancel()
                log(f"速读 {ok} 篇后暂停（额度 / 预算）")
                raise
            except Exception as e:  # noqa: BLE001
                failed.append({"id": futs[fu], "error": str(e)[:300]})
    log(f"速读 {ok} 篇，失败 {len(failed)} 篇")
    return {"ok": ok, "failed": failed}
