"""论文来源（ADR-0017 / ADR-0027 / ADR-0028）。每个函数返回 [rec]，rec 是 store.upsert 能吃的字典。

- arxiv_search：arXiv API 关键词检索（每日新提交 / 回填近 12 个月）
- hf_daily：Hugging Face Daily Papers（同时给出点赞 = 关注度）
- seed_citations：引用了种子论文的论文（Semantic Scholar，匿名限速）
- s2_batch：录用会议 + 引用数（DBLP 有防爬验证，不用）
- hf_paper / github_new_repos：代码仓库
- hn_attention：Hacker News 讨论（关注度）
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import xml.etree.ElementTree as ET

from . import http, ids

ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


# ---------------- arXiv ----------------


def _arxiv_query(term: str, categories: list, start: _dt.datetime, end: _dt.datetime) -> str:
    t = term if (term.startswith(("ti:", "abs:", "all:")) or " AND " in term) else f'abs:"{term}"' if " " in term or "-" in term else f"abs:{term}"
    q = f"({t})"
    if categories:
        q += " AND (" + " OR ".join(f"cat:{c}" for c in categories) + ")"
    q += f" AND submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]"
    return q


def parse_arxiv_atom(xml: str) -> list:
    root = ET.fromstring(xml)
    out = []
    for e in root.findall("a:entry", ATOM):
        aid = ids.arxiv_id(e.findtext("a:id", "", ATOM))
        if not aid:
            continue
        title = re.sub(r"\s+", " ", e.findtext("a:title", "", ATOM)).strip()
        abstract = re.sub(r"\s+", " ", e.findtext("a:summary", "", ATOM)).strip()
        pub = (e.findtext("a:published", "", ATOM) or "")[:10]
        authors = [a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)]
        cats = [c.get("term") for c in e.findall("a:category", ATOM)]
        comment = e.findtext("arxiv:comment", "", ATOM) or ""
        rec = {"arxiv": aid, "title": title, "abstract": abstract, "published": pub, "authors": authors,
               "categories": cats, "url": f"https://arxiv.org/abs/{aid}", "pdf_url": f"https://arxiv.org/pdf/{aid}",
               "comment": comment}
        codes = re.findall(r"https?://github\.com/[\w.\-]+/[\w.\-]+", comment + " " + abstract)
        if codes:
            rec["code_urls"] = list(dict.fromkeys(c.rstrip(".") for c in codes))
        m = re.search(r"(?:accepted|to appear|published)\s+(?:at|in|by|to)\s+([A-Z][\w\-]*(?:\s?\d{4})?)", comment, re.I)
        if m:
            rec["venue"] = m.group(1)
            rec["venue_is_acceptance"] = True
        out.append(rec)
    return out


def arxiv_search(keywords: list, categories: list, start: _dt.datetime, end: _dt.datetime,
                 max_per_term: int = 2000, page: int = 200) -> list:
    """每个检索词单独查（arXiv API 的 OR 组合不可靠），结果按 arXiv ID 合并。"""
    seen = {}
    for term in keywords:
        q = _arxiv_query(term, categories, start, end)
        for offset in range(0, max_per_term, page):
            xml = http.get("https://export.arxiv.org/api/query", {
                "search_query": q, "start": offset, "max_results": page,
                "sortBy": "submittedDate", "sortOrder": "descending"}, as_json=False, ttl=6 * 3600, timeout=120)
            batch = parse_arxiv_atom(xml)
            for r in batch:
                r.setdefault("matched_terms", [])
                if r["arxiv"] in seen:
                    seen[r["arxiv"]]["matched_terms"].append(term)
                else:
                    r["matched_terms"].append(term)
                    seen[r["arxiv"]] = r
            if len(batch) < page:
                break
    return list(seen.values())


def arxiv_by_ids(arxiv_ids: list) -> list:
    out = []
    for i in range(0, len(arxiv_ids), 100):
        chunk = arxiv_ids[i:i + 100]
        xml = http.get("https://export.arxiv.org/api/query", {"id_list": ",".join(chunk), "max_results": len(chunk)},
                       as_json=False, ttl=7 * 86400, timeout=120)
        out += parse_arxiv_atom(xml)
    return out


# ---------------- Hugging Face Daily Papers ----------------


def hf_daily(day: str) -> list:
    """某一天的 HF Daily Papers。rec 带 attention.hf_upvotes。"""
    try:
        data = http.get("https://huggingface.co/api/daily_papers", {"date": day, "limit": 100}, ttl=3 * 3600)
    except http.HttpError:
        return []
    out = []
    for it in data or []:
        p = it.get("paper") or {}
        aid = ids.arxiv_id(p.get("id") or "")
        if not aid:
            continue
        out.append({
            "arxiv": aid, "title": re.sub(r"\s+", " ", p.get("title") or it.get("title") or "").strip(),
            "abstract": (p.get("summary") or "").strip(), "published": (p.get("publishedAt") or "")[:10],
            "authors": [a.get("name") for a in p.get("authors") or [] if a.get("name")],
            "url": f"https://arxiv.org/abs/{aid}", "pdf_url": f"https://arxiv.org/pdf/{aid}",
            "code_urls": [p["githubRepo"]] if p.get("githubRepo") else [],
            "hf_upvotes": p.get("upvotes") or 0, "hf_day": day,
        })
    return out


# ---------------- 引用了种子的论文 ----------------

S2_FIELDS = "title,abstract,externalIds,publicationDate,venue,authors,year,url"


def seed_citations(seed_arxiv: str, max_total: int = 3000) -> list:
    out = []
    offset = 0
    while offset < max_total:
        try:
            data = http.get(f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{seed_arxiv}/citations",
                            {"fields": S2_FIELDS, "limit": 500, "offset": offset}, ttl=20 * 3600, timeout=90)
        except http.HttpError as e:
            if e.code == 404:
                break
            raise
        rows = data.get("data") or []
        for r in rows:
            p = r.get("citingPaper") or {}
            if not p.get("title"):
                continue
            ext = p.get("externalIds") or {}
            aid = ext.get("ArXiv")
            out.append({
                "arxiv": aid, "doi": ext.get("DOI"), "title": p["title"].strip(), "abstract": p.get("abstract") or "",
                "published": p.get("publicationDate") or (str(p["year"]) if p.get("year") else None),
                "venue": short_venue(p.get("venue")), "authors": [a.get("name") for a in p.get("authors") or []],
                "url": f"https://arxiv.org/abs/{aid}" if aid else p.get("url"),
                "pdf_url": f"https://arxiv.org/pdf/{aid}" if aid else None,
                "cites_seeds": [seed_arxiv],
            })
        if data.get("next") is None or len(rows) == 0:
            break
        offset = data["next"]
    return out


# ---------------- 会议录用 / 引用数（Semantic Scholar 批量接口）----------------

VENUE_ALIASES = {
    "learning representations": "ICLR", "neural information processing": "NeurIPS", "neurips": "NeurIPS",
    "machine learning": "ICML", "icml": "ICML", "iclr": "ICLR", "association for computational linguistics": "ACL",
    "empirical methods": "EMNLP", "emnlp": "EMNLP", "naacl": "NAACL", "aaai": "AAAI", "ijcai": "IJCAI",
    "knowledge discovery": "KDD", "world wide web": "WWW", "colm": "COLM", "language modeling": "COLM",
    "autonomous agents and multiagent": "AAMAS", "aamas": "AAMAS", "pattern analysis": "TPAMI", "tmlr": "TMLR",
}


def short_venue(v: str | None) -> str | None:
    if not v or v.lower() in ("arxiv.org", "arxiv", "corr"):
        return None
    lv = v.lower()
    for k, name in VENUE_ALIASES.items():
        if k in lv:
            return name
    return v


def s2_batch(arxiv_ids: list) -> dict:
    """{arxiv: {venue, year, citations}}；匿名调用，每次最多 500 篇。"""
    out = {}
    for i in range(0, len(arxiv_ids), 400):
        chunk = arxiv_ids[i:i + 400]
        body = json.dumps({"ids": [f"arXiv:{a}" for a in chunk]}).encode()
        try:
            data = http.get("https://api.semanticscholar.org/graph/v1/paper/batch",
                            {"fields": "venue,publicationVenue,year,citationCount,externalIds"},
                            headers={"Content-Type": "application/json"}, data=body, ttl=3 * 86400, timeout=120)
        except Exception:
            continue
        for a, p in zip(chunk, data or []):
            if not p:
                continue
            pv = (p.get("publicationVenue") or {}).get("name")
            v = short_venue(pv or p.get("venue"))
            out[a] = {"venue": f"{v} {p.get('year') or ''}".strip() if v else None, "citations": p.get("citationCount")}
    return out


# ---------------- 代码仓库 ----------------


def hf_paper(arxiv: str) -> dict:
    """HF 论文页：githubRepo、点赞。"""
    try:
        p = http.get(f"https://huggingface.co/api/papers/{arxiv}", ttl=3 * 86400)
    except Exception:
        return {}
    return {"github": p.get("githubRepo"), "upvotes": p.get("upvotes") or 0}


def github_new_repos(keywords: list, since: str, min_stars: int = 5) -> list:
    out, seen = [], set()
    for kw in keywords[:6]:
        try:
            data = http.get("https://api.github.com/search/repositories",
                            {"q": f'"{kw}" created:>={since} stars:>={min_stars}', "sort": "stars", "per_page": 10},
                            ttl=12 * 3600)
        except http.HttpError:
            continue
        for r in data.get("items") or []:
            name = r["full_name"].lower()
            if r["html_url"] in seen or any(x in name for x in ("awesome", "paper-list", "papers", "daily", "reading-list")):
                continue
            seen.add(r["html_url"])
            out.append({"url": r["html_url"], "name": r["full_name"], "description": r.get("description") or "",
                        "stars": r.get("stargazers_count", 0), "created": r.get("created_at"), "keyword": kw})
    return sorted(out, key=lambda x: -x["stars"])


# ---------------- Hacker News ----------------


def hn_attention(arxiv: str | None, title: str) -> dict:
    """HN 上的讨论：按 arXiv ID 和标题各搜一次，取讨论帖的点数 / 评论数。"""
    hits = {}
    queries = [q for q in (arxiv, f'"{title[:80]}"' if title else None) if q]
    for q in queries:
        try:
            data = http.get("https://hn.algolia.com/api/v1/search", {"query": q, "tags": "story"}, ttl=12 * 3600)
        except Exception:
            continue
        for h in data.get("hits") or []:
            url = h.get("url") or ""
            if arxiv and arxiv not in url and ids.norm_title(title)[:40] not in ids.norm_title(h.get("title")):
                continue
            hits[h["objectID"]] = {"points": h.get("points") or 0, "comments": h.get("num_comments") or 0,
                                   "created_at": h.get("created_at"),
                                   "url": f"https://news.ycombinator.com/item?id={h['objectID']}"}
    return {"hn_posts": len(hits), "hn_points": sum(h["points"] for h in hits.values()),
            "hn_comments": sum(h["comments"] for h in hits.values()), "hn_links": [h["url"] for h in hits.values()][:5]}
