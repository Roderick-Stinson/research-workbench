"""顶会顶刊必读（ADR-0040）：研究方向内、发表在 CCF-A / ICLR / TPAMI 等 CCF-A 期刊 / Nature·Science 子刊上的论文。

1. 发现：论文库只回填了近 12 个月的 arXiv，领域的经典工作（2023–2025 的顶会论文）大多不在库里。
   用 Semantic Scholar 按关键词 × 顶会顶刊（2023 年起）检索，新论文进库（标 backfill，不进每日精选），走相关性判定与速读。
2. 录用信息：库里属于方向的论文，用 S2（venue / publicationVenue / journal）、arXiv 评论、速读里作者自称的会议识别顶会顶刊（venues.py）。
3. 必读 = 属于方向 + 顶会顶刊 + 至少一条：种子论文 / 领域常用方法的原论文 / 被论文库里 ≥ 3 篇论文当作对比方法 /
   引用数高（发表满一年 ≥ 100，不满一年 ≥ 40，且要过口味过滤）。其余顶会顶刊论文是"值得读"，过口味过滤（ADR-0039）。
   按首次公开时间排序；会议年份只用录用信息里写明的（不拿 arXiv 年份代替）。
"""
from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict

from .. import config
from ..util import now_iso, read_json, write_json
from . import field as fieldmod
from . import http, intake, llm, results, rigor, store, venues

S2 = "https://api.semanticscholar.org/graph/v1/paper"
S2_FIELDS = "title,abstract,externalIds,publicationDate,venue,publicationVenue,journal,year,citationCount,authors,url"
S2_VENUES = [
    "International Conference on Learning Representations", "International Conference on Machine Learning",
    "Neural Information Processing Systems", "Annual Meeting of the Association for Computational Linguistics",
    "AAAI Conference on Artificial Intelligence", "International Joint Conference on Artificial Intelligence",
    "Computer Vision and Pattern Recognition", "IEEE International Conference on Computer Vision",
    "Knowledge Discovery and Data Mining", "The Web Conference", "International Conference on Software Engineering",
    "ACM SIGSOFT International Symposium on Foundations of Software Engineering", "Automated Software Engineering",
    "Annual International ACM SIGIR Conference on Research and Development in Information Retrieval",
    "IEEE Transactions on Pattern Analysis and Machine Intelligence", "Nature Machine Intelligence", "Nature Communications",
    "Nature Computational Science", "Science Advances", "Journal of Machine Learning Research", "Artificial Intelligence",
    "ACM Transactions on Software Engineering and Methodology", "IEEE Transactions on Knowledge and Data Engineering",
]
DEFAULT_QUERIES = [
    '"multi-agent" + (LLM | "language model" | "language models")',
    '"multi-agent system" + (design | optimization | search | topology)',
    '"agentic workflow" | "agentic workflows"',
    '"agentic system" + (design | search | automated)',
    '(workflow | workflows) + (LLM | agent) + (optimization | generation | search)',
    '(agent | agents) + (topology | orchestration | "communication graph" | collaboration)',
    '"multi-agent debate" | "multi-agent collaboration" | "LLM-based agents"',
]
MIN_YEAR = 2023


def out_path():
    return config.home() / "reports" / "must_read.json"


def _vinfo(p: dict) -> list:
    return [("s2", (p.get("publicationVenue") or {}).get("name")), ("s2", (p.get("journal") or {}).get("name")),
            ("s2", p.get("venue"))]


def _date(p: dict) -> str:
    return p.get("publicationDate") or (f"{p['year']}-01-01" if p.get("year") else "")


# ---------------- 发现 ----------------

def discover(log=print, max_per_query: int = 1000) -> dict:
    """S2 按关键词 × 顶会顶刊检索，新论文进库并做相关性判定 / 速读（回填预算）。"""
    f = fieldmod.field()
    queries = f.get("topvenue_queries") or DEFAULT_QUERIES
    index = store.Index()
    found, new, rejected = {}, [], []
    for q in queries:
        token, got = None, 0
        while got < max_per_query:
            params = {"query": q, "venue": ",".join(S2_VENUES), "year": f"{MIN_YEAR}-", "fields": S2_FIELDS}
            if token:
                params["token"] = token
            try:
                d = http.get(f"{S2}/search/bulk", params, ttl=7 * 86400, timeout=120)
            except Exception as e:  # noqa: BLE001
                log(f"S2 检索失败（{q}）：{e}")
                break
            for p in d.get("data") or []:
                v = venues.best(_vinfo(p))
                if venues.not_main_doi((p.get("externalIds") or {}).get("DOI")):
                    rejected.append(p)  # Findings / 研讨会 / demo
                elif v and p.get("title"):
                    found[p["paperId"]] = (p, v)
            got += len(d.get("data") or [])
            token = d.get("token")
            if not token:
                break
    for p, v in found.values():
        ext = p.get("externalIds") or {}
        aid = ext.get("ArXiv")
        rec = {"arxiv": aid, "doi": ext.get("DOI"), "title": p["title"].strip(), "abstract": p.get("abstract") or "",
               "published": p.get("publicationDate") or (str(p["year"]) if p.get("year") else None),
               "authors": [a.get("name") for a in p.get("authors") or []],
               "url": f"https://arxiv.org/abs/{aid}" if aid else p.get("url"),
               "pdf_url": f"https://arxiv.org/pdf/{aid}" if aid else None}
        pid, is_new = store.upsert(index, rec, {"name": "top-venue", "ref": v["name"]})
        m = store.load(pid)
        m["top_venue"] = dict(v, date=_date(p), checked_at=now_iso())
        m["citations"] = p.get("citationCount")
        if is_new:
            m["backfill"] = True  # 不进每日精选
            new.append(pid)
        store.save(m)
    n_rej = 0
    for p in rejected:  # 之前误标为主会的（例如 ACL Findings）：去掉顶会标记
        ext = p.get("externalIds") or {}
        pid = index.find(arxiv=ext.get("ArXiv"), doi=ext.get("DOI"), title=p.get("title"))
        m = store.load(pid) if pid else None
        if m and m.get("top_venue") and m["top_venue"].get("source") == "s2":
            m.pop("top_venue")
            m["top_venue_rejected"] = {"doi": ext.get("DOI"), "at": now_iso()}
            store.save(m)
            n_rej += 1
    log(f"顶会顶刊检索：{len(found)} 篇，其中新进库 {len(new)} 篇；Findings / 研讨会等 {len(rejected)} 篇不算（清掉之前的误标 {n_rej} 篇）")
    return {"found": len(found), "new": len(new), "not_main": len(rejected)}


def process(log=print, kind: str = "backfill") -> dict:
    """顶会顶刊检索进来的论文：相关性判定 → 速读（回填预算；额度不够时下次接着做）。"""
    ms = [m for m in store.all_papers() if m.get("top_venue") and any(s.get("name") == "top-venue" for s in m.get("sources") or [])]
    cand = [m["id"] for m in ms if m.get("intake", {}).get("state") in (None, "candidate")]
    out = {"judged": 0, "read": 0}
    if cand:
        out["judged"] = intake.run_relevance(cand, log=log, kind=kind)
    rel = [m["id"] for m in store.all_papers() if m.get("top_venue") and any(s.get("name") == "top-venue" for s in m.get("sources") or [])
           and m.get("intake", {}).get("state") == "relevant" and not m.get("_qr")]
    if rel:
        out["read"] = intake.run_quickreads(rel, log=log, kind=kind)["ok"]
    return out


# ---------------- 录用信息 ----------------

def refresh_venues(log=print, max_age_days: int = 14) -> dict:
    """库里属于方向的论文：识别顶会顶刊（S2 > arXiv 评论 > 作者自称）。"""
    now = _dt.datetime.now()
    todo = []
    for m in store.all_papers():
        if m.get("intake", {}).get("state") != "relevant":
            continue
        chk = (m.get("top_venue") or {}).get("checked_at") or m.get("venue_checked_at")
        if chk and (now - _dt.datetime.fromisoformat(chk[:19])).days < max_age_days:
            continue
        todo.append(m)
    s2 = {}
    ids_ = [m["arxiv"] for m in todo if m.get("arxiv")]
    for i in range(0, len(ids_), 400):
        chunk = ids_[i:i + 400]
        try:
            data = http.get(f"{S2}/batch", {"fields": "venue,publicationVenue,journal,year,publicationDate,citationCount,externalIds"},
                            headers={"Content-Type": "application/json"},
                            data=json.dumps({"ids": [f"arXiv:{a}" for a in chunk]}).encode(), ttl=3 * 86400, timeout=120)
        except Exception as e:  # noqa: BLE001
            log(f"S2 批量查询失败：{e}")
            continue
        for a, p in zip(chunk, data or []):
            if p:
                s2[a] = p
    n_top = 0
    for m in todo:
        m = store.load(m["id"])
        p = s2.get(m.get("arxiv")) or {}
        qr = store.load_quickread(m["id"]) or {}
        cands = _vinfo(p) + [("arXiv 评论", m.get("comment")), ("作者自称", qr.get("venue_claim"))]
        v = venues.best(cands)
        if v and venues.not_main_doi((p.get("externalIds") or {}).get("DOI")):
            v = None
        if p.get("citationCount") is not None:
            m["citations"] = p["citationCount"]
        if v:
            m["top_venue"] = dict(v, date=_date(p) or m.get("published") or "",
                                  checked_at=now_iso())
            n_top += 1
        else:
            m.pop("top_venue", None)
            m["venue_checked_at"] = now_iso()
        store.save(m)
    log(f"录用信息：核对 {len(todo)} 篇，顶会顶刊 {n_top} 篇")
    return {"checked": len(todo), "top": n_top}


# ---------------- 必读 ----------------

def build(log=print) -> dict:
    """生成必读 / 值得读清单，写 reports/must_read.json（面板读它）。"""
    from . import taste

    rows = results.all_rows()
    std = rigor.standards(rows)
    own = results.owners(rows)
    own_by_pid = defaultdict(set)
    for mk, o in own.items():
        own_by_pid[o["pid"]].add(mk)
    for m in store.all_papers():  # 本文方法还可以来自方法名核对、标题冒号前的短名（"MetaGPT: …"）
        qr = m.get("_qr") or {}
        for n in (qr.get("method_check") or {}).get("proposed") or []:
            own_by_pid[m["id"]].add(results.m_key(n))
        head = (m.get("title") or "").split(":")[0].strip()
        if ":" in (m.get("title") or "") and 0 < len(head.split()) <= 2 and len(head) <= 24:
            own_by_pid[m["id"]].add(results.m_key(head))
    compared = defaultdict(set)
    for r in rows:
        if not r["is_proposed"]:
            compared[r["mkey"]].add(r["pid"])
    seeds = {s["arxiv"] for s in fieldmod.seeds()}
    today = _dt.date.fromisoformat(store.today())
    must, worth = [], []
    for m in store.all_papers():
        v = m.get("top_venue")
        if not v or m.get("intake", {}).get("state") != "relevant":
            continue
        st = m.get("_status") or {}
        if st.get("irrelevant") or (st.get("rating") or {}).get("score", 5) <= 2:
            continue
        if v.get("year") and v["year"] < MIN_YEAR:
            continue
        qr = m.get("_qr") or {}
        mks = {k for k in own_by_pid.get(m["id"], set()) if k and k != "?"}
        n_cmp = max((len(compared[k] - {m["id"]}) for k in mks), default=0)
        cites = m.get("citations") or 0
        date = v.get("date") or m.get("published") or ""
        try:
            age = (today - _dt.date.fromisoformat(date[:10])).days if len(date) >= 10 else 999
        except ValueError:
            age = 999
        why = []
        if m.get("arxiv") in seeds:
            why.append("你的种子论文")
        if mks & set(std["baselines"]):
            why.append(f"领域常用方法 {'、'.join(sorted(std['baselines'][k] for k in mks & set(std['baselines'])))} 的原论文")
        if n_cmp >= 3:
            why.append(f"被论文库里 {n_cmp} 篇论文当作对比方法")
        cite_only = False
        if cites >= (100 if age >= 365 else 40):
            cite_only = not why
            why.append(f"引用 {cites}")
        item = {"pid": m["id"], "title": m.get("title"), "title_zh": qr.get("title_zh"), "one_liner": qr.get("one_liner"),
                "venue": v, "date": date, "citations": cites, "compared_by": n_cmp, "why": why,
                "methods": sorted(mks), "has_explainer": (store.pdir(m["id"]) / "explainer.html").exists(), "cite_only": cite_only}
        (must if why else worth).append(item)
    # 只靠引用数进必读的，也要合口味（高引用的应用类论文不算必读）
    co = [x for x in must if x["cite_only"]]
    for it in co:
        it["key"], it["kind"], it["text"] = it["pid"], "论文", it.get("one_liner") or ""
    kept = {x["pid"] for x in taste.keep(co, log=log, label="高引用必读")} if co else set()
    for it in co:
        for k in ("key", "kind", "text"):
            it.pop(k, None)
        if it["pid"] not in kept:
            must.remove(it)
            it["why"] = []
            worth.append(it)
    for it in worth:
        it["key"], it["kind"], it["text"] = it["pid"], "论文", it.get("one_liner") or ""
    worth = taste.keep(worth, log=log, label="顶会值得读") if worth else worth
    for it in worth:
        for k in ("key", "kind", "text"):
            it.pop(k, None)
    for xs in (must, worth):
        xs.sort(key=lambda x: x["date"], reverse=True)
    out = {"built_at": now_iso(), "must": must, "worth": worth}
    write_json(out_path(), out)
    log(f"顶会顶刊必读 {len(must)} 篇、值得读 {len(worth)} 篇")
    return {"must": len(must), "worth": len(worth)}


def load() -> dict:
    return read_json(out_path(), {}) or {}


def run(log=print, discover_too: bool = True) -> dict:
    """完整流程：检索 → 判定 / 速读 → 录用信息 → 清单。额度不够时判定 / 速读下次接着做，清单照常生成。"""
    out = {}
    if discover_too:
        out["discover"] = discover(log=log)
    try:
        out["process"] = process(log=log)
    except llm.LLMPaused as e:
        out["process"] = f"暂停：{e}"
    out["venues"] = refresh_venues(log=log)
    out["list"] = build(log=log)
    return out
