"""经典工作（ADR-0044）：按技术路线梳理领域的经典工作。

和"必读"（ADR-0040：只收顶会顶刊、按时间平铺）不同：经典按**影响力**选、不限会议；按**技术路线**分组，
写清每篇提出了什么、为什么算经典、后来谁在它基础上做、在常用 benchmark 上的数字。
经典是参照，不是推荐：不过口味过滤（AgentPrune 这类省 token 的工作照样是必须比的 baseline）。

1. 影响力（全部从论文库 / 结果表算，可复核）：方法被论文库里 ≥ classics.min_compared（默认 8）篇论文当作对比方法；
   或引用数 ≥ classics.min_citations（默认 200）且发表满一年；或是种子论文 / 领域常用方法的原论文。
2. 找原论文：被大量对比、但原论文不在库里（或没认出是哪篇）的方法，例如 AutoGen、DyLAN：
   批量模型先判断它是不是具体的方法 / 系统（"Ours"、"SFT"、"Full" 这类泛称不算），给出原论文标题；
   再用 Semantic Scholar 按标题核对（标题一致才算），进库走相关性判定与速读（标 backfill，不进每日精选）。
   原论文不属于本方向的（例如通用提示优化），列在"常被对比的邻近方向方法"。结果缓存在 reports/classics_resolve.json。
3. 梳理：批量模型把经典分成若干技术路线，写每条路线的主线、每篇的贡献与为什么重要、建议的阅读顺序；只能用给出的 id。
   数字（被对比数、引用数、benchmark 成绩、代码）由工作台填，不让模型写。
存 reports/classics.json：事实每天刷新；路线划分在经典集合变了或满 7 天时重做（--fresh 强制重做）。
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter, defaultdict

from .. import config
from ..util import now_iso, read_json, write_json
from . import field as fieldmod
from . import codecheck, http, ids, intake, llm, manual, results, rigor, sources, store, topvenue

# 结果表里常见、但不是某个具体方法的叫法（rigor 的 GENERIC 之外）
GENERIC = {
    "ours", "ourmethod", "proposed", "full", "fullmodel", "sft", "grpo", "ppo", "dpo", "rl", "naive", "human", "expert",
    "multiagent", "mas", "sas", "sequential", "parallel", "hierarchical", "centralized", "decentralized", "flat", "hybrid",
    "static", "dynamic", "greedy", "text", "rag", "bm25", "control", "reflection", "noskill", "nomemory", "nodefense",
    "nomem", "memory", "allatonce", "stepbystep", "binarysearch", "workflow", "pipeline", "planner", "router", "ensemble",
}
ORGANIZE_DAYS = 7


def path():
    return config.home() / "reports" / "classics.json"


def resolve_path():
    return config.home() / "reports" / "classics_resolve.json"


def load() -> dict:
    return read_json(path(), {}) or {}


def cfg() -> dict:
    c = fieldmod.field().get("classics") or {}
    return {"min_compared": c.get("min_compared", 8), "min_citations": c.get("min_citations", 200),
            "max_resolve": c.get("max_resolve", 40)}


def _generic(mk: str) -> bool:
    return not mk or mk == "?" or len(mk) <= 2 or mk in GENERIC or rigor._excluded(mk)


def _shift(date: str, days: int) -> str:
    try:
        return (_dt.date.fromisoformat(date[:10]) + _dt.timedelta(days=days)).isoformat() if len(date or "") >= 10 else ""
    except ValueError:
        return ""


def _age_days(date: str, today: _dt.date) -> int:
    try:
        return (today - _dt.date.fromisoformat(date[:10])).days if len(date or "") >= 10 else 999
    except ValueError:
        return 999


# ---------------- 影响力 ----------------

def signals(rows: list) -> dict:
    """compared：方法 → 把它当作对比方法的论文；names：方法的常见写法；own：论文 → 它自己提出的方法（含找原论文时认出的）。"""
    compared, names = defaultdict(set), defaultdict(Counter)
    for r in rows:
        if not r["is_proposed"]:
            compared[r["mkey"]].add(r["pid"])
            names[r["mkey"]][results.row_name(r["method"])] += 1
    own = topvenue.own_keys(rows)
    confirmed = {}
    for m in store.all_papers():
        for mk in m.get("classic_for") or []:
            own[m["id"]].add(mk)
            confirmed[mk] = m["id"]
    # 方法 → 原论文：按标题核对过的（classic_for）优先；否则最早声称它的那篇；
    # 有 ≥ 3 篇论文都声称是自己的（如 "ACE"）就是重名，不按名字认，交给"找原论文"
    pub = {m["id"]: m.get("published") or "9999" for m in store.all_papers()}
    claimers = defaultdict(list)
    for pid, mks in own.items():
        for mk in mks:
            claimers[mk].append(pid)
    owner = dict(confirmed)
    for mk, ps in claimers.items():
        if mk not in owner and len(ps) < 3:
            owner[mk] = min(ps, key=lambda p: pub.get(p, "9999"))
    return {"compared": compared, "names": names, "own": own, "owner": owner}


def candidates(rows: list, sig: dict | None = None) -> tuple:
    """(经典论文 {pid: 事实}, 原论文还没找到的方法 [{mkey, name, n}])。"""
    sig = sig or signals(rows)
    c = cfg()
    std = rigor.standards(rows)
    seeds = {s["arxiv"] for s in fieldmod.seeds()}
    today = _dt.date.fromisoformat(store.today())
    papers = {}
    allp = store.all_papers()
    pub = {m["id"]: (m.get("published") or "")[:10] for m in allp}
    for m in allp:
        if m.get("intake", {}).get("state") != "relevant" or (m.get("_status") or {}).get("irrelevant"):
            continue
        mks = {k for k in sig["own"].get(m["id"], set()) if not _generic(k) and sig["owner"].get(k) == m["id"]}
        # 只数在它之后（或同期，60 天内）发表的对比：大多数对比比它还早，说明这个方法名不是它提出的（认错了原论文）
        since = _shift(pub.get(m["id"]) or "", -60)
        cmp_ = {k: sum(1 for q in sig["compared"][k] - {m["id"]} if not since or (pub.get(q) or "9999") >= since) for k in mks}
        n_cmp = max(cmp_.values(), default=0)
        cites = m.get("citations") or 0
        date = ((m.get("top_venue") or {}).get("date") or m.get("published") or "")[:10]
        why = []
        if m.get("arxiv") in seeds:
            why.append("你的种子论文")
        stdk = sorted(std["baselines"][k] for k in mks & set(std["baselines"]))
        if stdk:
            why.append(f"领域常用方法 {'、'.join(stdk)} 的原论文")
        if n_cmp >= c["min_compared"]:
            why.append(f"被论文库里 {n_cmp} 篇论文当作对比方法")
        if cites >= c["min_citations"] and _age_days(date, today) >= 365:
            why.append(f"引用 {cites}")
        if not why:
            continue
        main = max(mks, key=lambda k: cmp_[k]) if mks else None
        short = sig["names"][main].most_common(1)[0][0] if main and sig["names"][main] else None
        if not short:
            head = (m.get("title") or "").split(":")[0].strip()
            short = head if ":" in (m.get("title") or "") and len(head) <= 24 else None
        papers[m["id"]] = {"pid": m["id"], "short": short, "methods": sorted(mks), "main": main, "n_compared": n_cmp,
                           "citations": cites, "date": date, "why": why}
    owned = {k for p in papers.values() for k in p["methods"]}
    orphans = []
    for mk, ps in sig["compared"].items():
        if len(ps) >= c["min_compared"] and not _generic(mk) and mk not in owned and sig["owner"].get(mk) not in papers:
            orphans.append({"mkey": mk, "name": sig["names"][mk].most_common(1)[0][0], "n": len(ps)})
    orphans.sort(key=lambda x: -x["n"])
    return papers, orphans


# ---------------- 找原论文 ----------------

SYS_RESOLVE = """下面是研究方向「{name}」的论文里经常被当作对比方法的名字（括号里是把它当作对比方法的部分论文）。
逐个判断：
- specific：它是不是一个**具体的、有出处的方法 / 系统 / 框架**（例如 AutoGen、DyLAN、TextGrad）。泛称（"Ours"、"SFT"、"Full"、"Multi-Agent"、
  "Sequential"、"Human"）、消融变体、通用训练算法、模型名都不是；
- title：如果是，给出**提出它的原论文**的完整英文标题（必须是你确定的真实论文；不确定就留空，不要编）；
- in_field：这篇原论文本身是否属于「{name}」方向（例如通用的提示优化、记忆库、检索方法算邻近方向，不属于）。
输出 JSON：{{"items": [{{"i": 序号, "specific": true/false, "title": "原论文标题或空", "in_field": true/false, "note": "一句话"}}]}}，每个都要有。"""


def _s2_match(title: str) -> tuple:
    """(命中, 出错)。标题归一化后完全一致才算命中；限流 / 网络错误时快速放弃（下次再查），不当作"找不到"。"""
    try:
        d = http.get(f"{manual.S2}/search/match", {"query": title, "fields": sources.S2_FIELDS + ",citationCount"},
                     ttl=7 * 86400, retries=1)
    except http.HttpError as e:
        return (None, None) if e.code == 404 else (None, f"HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:120]
    hit = (d.get("data") or [None])[0]
    return (hit if hit and ids.norm_title(hit.get("title")) == ids.norm_title(title) else None), None


def _ask(todo: list, cache: dict, kind: str) -> None:
    """批量模型判断每个名字是不是具体方法、给出原论文标题；结果马上写进缓存（status = generic / lookup / not_found）。"""
    ctx = defaultdict(list)
    keys = {o["mkey"] for o in todo}
    for r in results.all_rows():
        if not r["is_proposed"] and r["mkey"] in keys and len(ctx[r["mkey"]]) < 3 and r["orig_title"] not in ctx[r["mkey"]]:
            ctx[r["mkey"]].append(r["orig_title"])
    user = "\n".join(f"[{i}] {o['name']}（被 {o['n']} 篇对比；例如：{'；'.join((t or '')[:80] for t in ctx[o['mkey']])}）"
                     for i, o in enumerate(todo))
    res = llm.chat(SYS_RESOLVE.format(name=fieldmod.field()["name"]), user, task="classics-resolve", max_tokens=8000, kind=kind)
    for it in res.get("items") or []:
        try:
            o = todo[int(it["i"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        ent = {"name": o["name"], "n": o["n"], "at": now_iso(), "note": it.get("note"), "in_field": bool(it.get("in_field"))}
        title = (it.get("title") or "").strip()
        if not it.get("specific"):
            ent["status"] = "generic"
        else:  # 模型没给标题（多半是太新）：按名字找"名字: ……"开头的论文
            ent.update(status="lookup", claimed_title=title or None)
        cache[o["mkey"]] = ent
    write_json(resolve_path(), cache)


OPENALEX = "https://api.openalex.org/works"


def _head(title: str | None) -> str:
    t = title or ""
    return results.m_key(t.split(":")[0]) if ":" in t else ""


LOOKUP_V = 3  # 匹配规则的版本：规则变了，旧的匹配结果重新核对一遍


STOP = {"a", "an", "the", "of", "for", "and", "in", "on", "with", "via", "to", "by", "from", "is", "are", "can", "llm", "llms"}
PROPOSE_RX = r"(?:propose|introduce|present|named|called|dubbed|term(?:ed)?)\b[^.]{0,160}"


def _words(t: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if w not in STOP}


def _accept(cand: dict, title: str | None, name: str, mk: str, latest: str) -> bool:
    """候选论文能不能算这个方法的原论文（宁缺毋滥：认错比找不到更糟）：
    - 时间：不晚于"对比它的论文"里最早那一批（前 20%）的时间 + 30 天——原论文不可能比对比它的论文还晚；
    - 给了标题：标题一致，或冒号前的部分一致；否则摘要里写明提出了这个名字（"we propose … (DyLAN)"）且标题大体重合
      （论文改过标题的情况，如 DyLAN）；只是在摘要里提到它（后续工作拿它当 baseline）不算；
    - 只有名字（模型不知道标题）：标题必须以"名字:"开头，且有 arXiv 号（排除 Zenodo 之类的杂项记录）。"""
    pub = (cand.get("published") or "")[:10]
    if latest and pub and pub > latest:
        return False
    ct = cand.get("title") or ""
    if not title:
        return _head(ct) == mk and bool(cand.get("arxiv"))
    if ids.norm_title(ct) == ids.norm_title(title) or (_head(ct) and _head(ct) == _head(title)):
        return True
    a, b = _words(ct), _words(title)
    overlap = len(a & b) / max(1, min(len(a), len(b)))
    proposed = re.search(PROPOSE_RX + r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", cand.get("abstract") or "")
    return overlap >= 0.5 and bool(proposed)


def _lib_match(mk: str, title: str | None, name: str, latest: str) -> str | None:
    """论文库里已有的：标题一致或冒号前一致；模型没给标题时，标题以"方法名:"开头（例如 "Meta-Harness: End-to-End ……"）。
    同样要过 _accept。取最早的。"""
    nt, th = (ids.norm_title(title), _head(title)) if title else (None, None)
    hits = []
    for m in store.all_papers():
        t = m.get("title")
        if (nt and (ids.norm_title(t) == nt or (th and _head(t) == th))) or (not nt and _head(t) == mk):
            if _accept({"title": t, "published": m.get("published"), "arxiv": m.get("arxiv"), "abstract": m.get("abstract")},
                       title, name, mk, latest):
                hits.append(m)
    return min(hits, key=lambda m: m.get("published") or "9999")["id"] if hits else None


def _from_openalex(w: dict) -> dict:
    aid = None
    for u in [(w.get("ids") or {}).get("doi") or ""] + [l.get("landing_page_url") or "" for l in w.get("locations") or []]:
        aid = aid or ids.arxiv_id(u)
    inv = w.get("abstract_inverted_index") or {}
    words = sorted((pos, wd) for wd, ps in inv.items() for pos in ps)
    doi = ((w.get("ids") or {}).get("doi") or "").replace("https://doi.org/", "") or None
    return {"arxiv": aid, "doi": None if aid and doi and "arxiv" in doi.lower() else doi, "title": (w.get("title") or "").strip(),
            "abstract": " ".join(wd for _, wd in words), "published": w.get("publication_date"),
            "authors": [(a.get("author") or {}).get("display_name") for a in w.get("authorships") or []],
            "url": f"https://arxiv.org/abs/{aid}" if aid else (w.get("ids") or {}).get("openalex"),
            "pdf_url": f"https://arxiv.org/pdf/{aid}" if aid else None}


def _openalex(mk: str, name: str, title: str | None, latest: str) -> tuple:
    """(rec, 引用数, 出错)。按标题（或"名字 + 领域词"）检索，按 _accept 过滤，取最早的。"""
    q = title or f"{name} large language model agents"
    try:
        d = http.get(OPENALEX, {"search": q, "per-page": 10}, ttl=7 * 86400, retries=1)
    except Exception as e:  # noqa: BLE001
        return None, None, str(e)[:120]
    recs = [(_from_openalex(w), w.get("cited_by_count")) for w in d.get("results") or []]
    recs = [x for x in recs if _accept(x[0], title, name, mk, latest)]
    if not recs:
        return None, None, None
    recs.sort(key=lambda x: (not x[0]["arxiv"], x[0]["published"] or "9999"))
    return recs[0][0], recs[0][1], None


def _lookup(mk: str, ent: dict, index, latest: str) -> tuple:
    """找原论文并进库。顺序：论文库 → OpenAlex → Semantic Scholar（OpenAlex 出错时）。返回 (是否新进库, 出错)。"""
    title, name = ent.get("claimed_title"), ent["name"]
    pid, rec, cites, is_new = _lib_match(mk, title, name, latest), None, None, False
    if not pid:
        rec, cites, err = _openalex(mk, name, title, latest)
        if err and title:
            hit, err2 = _s2_match(title)
            if err2:
                return False, f"OpenAlex：{err}；S2：{err2}"
            if hit:
                r2 = manual._from_s2(hit)
                rec, cites = (r2, hit.get("citationCount")) if _accept(r2, title, name, mk, latest) else (None, None)
        elif err:
            return False, f"OpenAlex：{err}"
    old = ent.get("pid")
    ent["at"], ent["searched"], ent["v"] = now_iso(), True, LOOKUP_V
    if not pid and rec:
        pid, is_new = store.upsert(index, rec, {"name": "classics", "ref": name})
    if old and old != pid:  # 之前匹配错了：去掉旧论文上的标记
        om = store.load(old)
        if om and mk in (om.get("classic_for") or []):
            om["classic_for"] = [k for k in om["classic_for"] if k != mk]
            store.save(om)
    if not pid:
        ent["status"] = "not_found"
        for k in ("pid", "title", "url", "citations"):
            ent.pop(k, None)
        return False, None
    m = store.load(pid)
    m["classic_for"] = sorted(set(m.get("classic_for") or []) | {mk})
    if cites is not None:
        m["citations"] = max(m.get("citations") or 0, cites)
    if is_new:
        m["backfill"] = True  # 不进每日精选
    store.save(m)
    ent.update(status="resolved", pid=pid, title=m.get("title"), url=m.get("url"), citations=m.get("citations"))
    return is_new, None


def _latest(rows: list, keys: set) -> dict:
    """方法 → 原论文最晚可能的发表日期：对比它的论文里最早的 20% 的发表时间 + 30 天（少数日期记错的论文不至于卡死）。"""
    ds = defaultdict(list)
    for r in rows:
        if not r["is_proposed"] and r["mkey"] in keys and r.get("published"):
            ds[r["mkey"]].append(r["published"][:10])
    out = {}
    for k, xs in ds.items():
        xs = sorted(set(xs))
        out[k] = _shift(xs[len(xs) // 5], 30)
    return out


def resolve(orphans: list, log=print, kind: str = "backfill") -> dict:
    """给原论文不在库里的方法找原论文。缓存 {mkey: {...}}：模型的判断马上落盘；S2 限流时停下，下次接着查；
    找不到的 30 天后再问一次模型。"""
    cache = read_json(resolve_path(), {}) or {}
    now = _dt.datetime.now()
    todo = []
    for ent in cache.values():  # 匹配规则更新过：旧规则下的结果（含"找不到"）重新核对一遍
        if ent.get("status") in ("resolved", "not_found") and ent.get("v") != LOOKUP_V:
            ent["status"] = "lookup"
    for o in orphans[: cfg()["max_resolve"]]:
        x = cache.get(o["mkey"])
        if x and not (x.get("status") == "not_found" and (now - _dt.datetime.fromisoformat(x["at"][:19])).days >= 30):
            continue
        todo.append(o)
    if todo:
        _ask(todo, cache, kind)
    index, new, err = store.Index(), [], None
    latest = _latest(results.all_rows(), {k for k, e in cache.items() if e.get("status") == "lookup"})
    for mk, ent in cache.items():
        if ent.get("status") != "lookup":
            continue
        is_new, err = _lookup(mk, ent, index, latest.get(mk, ""))
        write_json(resolve_path(), cache)
        if err:
            log(f"经典工作：Semantic Scholar 核对标题出错（{err}），剩下的下次再查")
            break
        if is_new:
            new.append(mk)
    left = sum(1 for e in cache.values() if e.get("status") == "lookup")
    log(f"经典工作：问了 {len(todo)} 个常被对比的方法；已找到原论文 {sum(1 for e in cache.values() if e.get('status') == 'resolved')} 个"
        f"（本次新进库 {len(new)} 篇），待核对 {left} 个")
    # 新进库的：相关性判定 → 速读（回填预算；额度不够时下次接着做）
    cand = [m["id"] for m in store.all_papers() if any(s.get("name") == "classics" for s in m.get("sources") or [])
            and m.get("intake", {}).get("state") in (None, "candidate")]
    if cand:
        intake.run_relevance(cand, log=log, kind=kind)
    rel = [m["id"] for m in store.all_papers() if any(s.get("name") == "classics" for s in m.get("sources") or [])
           and m.get("intake", {}).get("state") == "relevant" and not m.get("_qr")]
    if rel:
        intake.run_quickreads(rel, log=log, kind=kind)
    return cache


# ---------------- 事实 ----------------

def facts(papers: dict, rows: list, sig: dict) -> dict:
    """每篇经典的事实：标题、时间、会议、代码、它比过的更早经典、后来在它基础上的工作、常用 benchmark 上的成绩。"""
    std = rigor.standards(rows)
    ds_papers = defaultdict(set)
    for r in rows:
        ds_papers[r["dataset"]].add(r["pid"])
    mk_owner = {k: pid for pid, p in papers.items() for k in p["methods"]}
    meta = {m["id"]: m for m in store.all_papers()}
    out = {}
    for pid, p in papers.items():
        m = meta.get(pid) or {}
        qr = m.get("_qr") or {}
        later = set()
        for k in p["methods"]:
            later |= sig["compared"][k] - {pid}
        later_l = sorted(later, key=lambda q: (-(meta.get(q, {}).get("citations") or 0), meta.get(q, {}).get("published") or ""))
        prev = sorted({mk_owner[r["mkey"]] for r in rows if r["pid"] == pid and not r["is_proposed"]
                       and mk_owner.get(r["mkey"]) not in (None, pid)}, key=lambda q: papers[q]["date"])
        bench = {}
        for r in rows:
            if r["pid"] != pid or not r["is_proposed"] or r["mkey"] not in p["methods"] or r["dataset"] not in std["benchmarks"] \
                    or r["kind"] != "quality":
                continue
            b = bench.get(r["dataset"])
            better = b is None or (r["scale"]["status"] == "full") > (b["scale"] == "full") or \
                ((r["scale"]["status"] == "full") == (b["scale"] == "full") and
                 (r["value"] > b["value"] if r["higher_better"] else r["value"] < b["value"]))
            if better:
                bench[r["dataset"]] = {"dataset": r["dataset"], "metric": r["metric"], "value": r["value"], "unit": r.get("unit"),
                                       "scale": r["scale"]["status"], "n": r["scale"].get("n"), "backbone": r["backbone"]}
        bench_l = sorted(bench.values(), key=lambda b: -len(ds_papers[b["dataset"]]))[:5]
        v = m.get("top_venue") or {}
        cc = codecheck.load(pid) or {}
        out[pid] = dict(p, title=m.get("title"), title_zh=qr.get("title_zh"), one_liner=qr.get("one_liner"),
                        venue=(f"{v['name']} {v.get('year') or ''}".strip() if v else (m.get("venue") or None)),
                        top_venue=bool(v), themes=m.get("themes") or [], url=m.get("url"),
                        code={"status": cc.get("status"), "url": cc.get("url"), "stars": cc.get("stars")},
                        has_explainer=(store.pdir(pid) / "explainer.html").exists(),
                        rating=((m.get("_status") or {}).get("rating") or {}).get("score"),
                        later=later_l[:6], n_later=len(later_l), prev=prev, bench=bench_l)
    return out


# ---------------- 梳理 ----------------

SYS_ORGANIZE = """你在为研究方向「{name}」梳理**经典工作**，给一位刚进入这个方向、要做出能中 CCF-A 的工作的研究者读。
下面每行是一篇经典：[id] 方法短名 | 标题 | 首次公开 | 会议 | 一句话 | 研究主题 | 影响力 | 它对比过的更早经典。
任务：
1. 把它们分成 4–8 条**技术路线**（例如"自动化 workflow / 架构搜索"、"通信拓扑设计与剪枝"、"角色分工与 SOP"、"辩论与共识"、"路由与查询自适应编排"、
   "失败归因与评测"……按实际内容定，不要照抄例子）。每条路线写 summary：这条路线要解决什么、主线怎么演进（2–3 句，提到的论文用 [id]）。
2. 每篇放进**一条**最合适的路线，给出 role（开创 / 奠基 / 代表 / 改进 / 评测与分析 之一）、contribution（提出了什么，≤ 40 字）、
   why（为什么算经典：依据给出的影响力和它对后续工作的作用，≤ 40 字，不要编数字）。
3. reading_order：8–12 篇建议**先读**的，按阅读顺序排，每篇一句理由。
只能用给出的 id；不确定的不要写。
输出 JSON：{{"routes": [{{"name": "≤ 14 字", "summary": "...", "members": [{{"id": "...", "role": "...", "contribution": "...", "why": "..."}}]}}],
"reading_order": [{{"id": "...", "reason": "..."}}]}}"""


def organize(items: dict, log=print, kind: str = "daily") -> dict:
    themes = {t["id"]: t["name"] for t in (read_json(config.home() / "registry" / "themes.json", {}) or {}).get("themes") or []}
    lines = []
    for pid, x in sorted(items.items(), key=lambda kv: kv[1]["date"]):
        prev = "、".join(items[q]["short"] or q for q in x["prev"]) or "—"
        lines.append(f"[{pid}] {x['short'] or '—'} | {x['title']} | {x['date'][:7]} | {x['venue'] or '—'} | {x.get('one_liner') or ''} | "
                     f"{'、'.join(themes.get(t, t) for t in x['themes']) or '—'} | {'；'.join(x['why'])} | {prev}")
    res = llm.chat(SYS_ORGANIZE.format(name=fieldmod.field()["name"]), "\n".join(lines), task="classics-organize",
                   max_tokens=12000, kind=kind)
    seen, routes = set(), []
    for rt in res.get("routes") or []:
        mem = []
        for x in rt.get("members") or []:
            if x.get("id") in items and x["id"] not in seen:
                seen.add(x["id"])
                mem.append({k: x.get(k) for k in ("id", "role", "contribution", "why")})
        if mem and rt.get("name"):
            mem.sort(key=lambda x: items[x["id"]]["date"])
            routes.append({"name": rt["name"], "summary": rt.get("summary") or "", "members": mem})
    rest = sorted((p for p in items if p not in seen), key=lambda p: items[p]["date"])
    if rest:
        routes.append({"name": "其它经典", "summary": "", "members": [{"id": p} for p in rest]})
    order = []
    for x in res.get("reading_order") or []:
        if x.get("id") in items and x["id"] not in {o["id"] for o in order}:
            order.append({"id": x["id"], "reason": x.get("reason") or ""})
    log(f"经典工作：{len(items)} 篇分成 {len(routes)} 条路线，建议先读 {len(order)} 篇")
    return {"routes": routes, "reading_order": order}


def build(log=print, fresh: bool = False, kind: str = "daily") -> dict:
    """生成 reports/classics.json（面板读它）。额度不够时：能算的事实照常更新，找原论文 / 梳理下次再做。"""
    prev = load()
    rows = results.all_rows()
    paused = []
    papers, orphans = candidates(rows)
    try:
        cache = resolve(orphans, log=log)
        rows = results.all_rows()
        papers, orphans = candidates(rows)
    except (llm.LLMPaused, llm.LLMNotConfigured) as e:
        cache = read_json(resolve_path(), {}) or {}
        paused.append(f"找原论文：{e}")
    sig = signals(rows)
    items = facts(papers, rows, sig)
    signature = sorted(items)
    age = None
    if prev.get("organized_at"):
        age = (_dt.datetime.now() - _dt.datetime.fromisoformat(prev["organized_at"][:19])).days
    org = {"routes": prev.get("routes") or [], "reading_order": prev.get("reading_order") or []}
    organized_at = prev.get("organized_at")
    if fresh or not org["routes"] or prev.get("signature") != signature or (age is not None and age >= ORGANIZE_DAYS):
        try:
            org = organize(items, log=log, kind=kind)
            organized_at = now_iso()
        except (llm.LLMPaused, llm.LLMNotConfigured) as e:
            paused.append(f"梳理路线：{e}")
    # 沿用旧的路线划分时：去掉已经不算经典的，新进的经典放"新进的经典"
    placed = set()
    for rt in org["routes"]:
        rt["members"] = [x for x in rt["members"] if x["id"] in items]
        placed |= {x["id"] for x in rt["members"]}
    org["routes"] = [rt for rt in org["routes"] if rt["members"]]
    new = sorted((p for p in items if p not in placed), key=lambda p: items[p]["date"])
    if new:
        org["routes"].append({"name": "新进的经典", "summary": "最近达到经典门槛、还没归入路线的工作（下次梳理时归类）。",
                              "members": [{"id": p} for p in new]})
    org["reading_order"] = [x for x in org["reading_order"] if x["id"] in items]
    adjacent = []
    for x in cache.values():
        if x.get("status") != "resolved" or x.get("pid") in items:
            continue
        state = ((store.load(x["pid"]) or {}).get("intake") or {}).get("state")
        adjacent.append({"name": x["name"], "n": x["n"], "title": x.get("title"), "url": x.get("url"), "pid": x.get("pid"),
                         "citations": x.get("citations"), "note": x.get("note"),
                         "pending": state in (None, "candidate") or (state == "relevant")})  # 还没判定 / 速读完
    adjacent.sort(key=lambda x: -x["n"])
    unresolved = sorted(({"name": x["name"], "n": x["n"], "pending": x.get("status") == "lookup"}
                         for x in cache.values() if x.get("status") in ("not_found", "lookup")),
                        key=lambda x: -x["n"])
    out = {"built_at": now_iso(), "organized_at": organized_at, "signature": signature, "items": items,
           "routes": org["routes"], "reading_order": org["reading_order"], "adjacent": adjacent, "unresolved": unresolved,
           "criteria": cfg(), "paused": paused}
    write_json(path(), out)
    log(f"经典工作：{len(items)} 篇，{len(org['routes'])} 条路线；邻近方向 {len(adjacent)} 个，没找到原论文 {len(unresolved)} 个"
        + (f"；暂停：{'；'.join(paused)}" if paused else ""))
    return {"classics": len(items), "routes": len(org["routes"]), "adjacent": len(adjacent), "paused": bool(paused)}
