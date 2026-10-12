"""论文库存取：$WB_HOME/papers/<paper-id>/。

meta.json       事实：标识、标题、作者、摘要、日期、来源证据、关注度、收录判定、打分、精选
quickread.json  速读 + 维度标签 + 实验设置 + 抽取的结果（批量模型生成）
status.json     本人的反馈（已读 / 收藏 / 不相关 / 想复现 …），只由 wb CLI 写
explainer.html  精读（仅精选，codex 生成）
fulltext.txt    全文文本（arXiv HTML 或 PDF 抽取）
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from .. import config
from ..util import now_iso, read_json, write_json
from . import ids


def papers_dir() -> Path:
    return config.home() / "papers"


def pdir(pid: str) -> Path:
    return papers_dir() / pid


def load(pid: str) -> dict | None:
    return read_json(pdir(pid) / "meta.json")


def save(meta: dict) -> None:
    meta["updated_at"] = now_iso()
    write_json(pdir(meta["id"]) / "meta.json", meta)


def load_quickread(pid: str) -> dict | None:
    return read_json(pdir(pid) / "quickread.json")


def save_quickread(pid: str, qr: dict) -> None:
    write_json(pdir(pid) / "quickread.json", qr)


def load_status(pid: str) -> dict:
    return read_json(pdir(pid) / "status.json", {}) or {}


def save_status(pid: str, st: dict) -> None:
    write_json(pdir(pid) / "status.json", st)


def all_ids() -> list:
    d = papers_dir()
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if (p / "meta.json").exists())


_cache: dict = {}


def all_papers(with_quickread: bool = False) -> list:
    """全部论文（按 mtime 缓存，面板反复调用不慢）。"""
    out = []
    for pid in all_ids():
        d = pdir(pid)
        try:
            key = ((d / "meta.json").stat().st_mtime_ns,
                   (d / "quickread.json").stat().st_mtime_ns if (d / "quickread.json").exists() else 0,
                   (d / "status.json").stat().st_mtime_ns if (d / "status.json").exists() else 0)
        except OSError:
            continue
        c = _cache.get(pid)
        if c and c[0] == key:
            out.append(c[1])
            continue
        m = load(pid)
        if not m:
            continue
        m["_qr"] = load_quickread(pid)
        m["_status"] = load_status(pid)
        _cache[pid] = (key, m)
        out.append(m)
    return out


class Index:
    """去重索引：arXiv ID / DOI / 归一化标题 → paper id。"""

    def __init__(self):
        self.by_arxiv, self.by_doi, self.by_title = {}, {}, {}
        for pid in all_ids():
            m = load(pid)
            if m:
                self.add(m)

    def add(self, m: dict):
        if m.get("arxiv"):
            self.by_arxiv[m["arxiv"]] = m["id"]
        if m.get("doi"):
            self.by_doi[m["doi"]] = m["id"]
        nt = ids.norm_title(m.get("title"))
        if len(nt) > 20:
            self.by_title[nt] = m["id"]

    def find(self, arxiv=None, doi=None, title=None) -> str | None:
        if arxiv and arxiv in self.by_arxiv:
            return self.by_arxiv[arxiv]
        if doi and doi in self.by_doi:
            return self.by_doi[doi]
        nt = ids.norm_title(title)
        if len(nt) > 20 and nt in self.by_title:
            return self.by_title[nt]
        return None


def today() -> str:
    """期号日期（北京时间）。"""
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%d")


def upsert(index: Index, rec: dict, source: dict, day: str | None = None) -> tuple:
    """合并一条来源记录。rec 至少含 title；可含 arxiv / doi / authors / abstract / published / venue / urls。

    返回 (paper id, 是否新收录)。已有论文只补空字段、追加来源证据，不覆盖已有内容。
    """
    arxiv = rec.get("arxiv")
    doi = ids.norm_doi(rec.get("doi"))
    if doi and doi.startswith("10.48550/arxiv.") and not arxiv:
        arxiv = doi.split("arxiv.", 1)[1]
    pid = index.find(arxiv, doi, rec.get("title"))
    new = pid is None
    if new:
        pid = ids.paper_id(arxiv, doi, rec.get("title"))
        m = {"id": pid, "arxiv": arxiv, "doi": doi, "title": (rec.get("title") or "").strip(),
             "authors": rec.get("authors") or [], "abstract": rec.get("abstract") or "",
             "published": rec.get("published"), "venue": rec.get("venue"), "url": rec.get("url"),
             "pdf_url": rec.get("pdf_url"), "code_urls": list(rec.get("code_urls") or []),
             "categories": rec.get("categories") or [], "sources": [], "cites_seeds": [],
             "attention": {}, "discovered_at": now_iso(), "discovered_day": day or today(),
             "intake": {"state": "candidate"}}
    else:
        m = load(pid) or {"id": pid}
        for k in ("arxiv", "doi", "abstract", "published", "venue", "url", "pdf_url"):
            v = rec.get(k) if k not in ("arxiv", "doi") else {"arxiv": arxiv, "doi": doi}[k]
            if v and not m.get(k):
                m[k] = v
        if rec.get("authors") and not m.get("authors"):
            m["authors"] = rec["authors"]
        for u in rec.get("code_urls") or []:
            if u not in m.setdefault("code_urls", []):
                m["code_urls"].append(u)
        if rec.get("venue") and rec.get("venue") != m.get("venue") and rec.get("venue_is_acceptance"):
            # 被录用是"进展"：记下来，同一期更新（ADR-0027）
            m["venue"] = rec["venue"]
            m.setdefault("progress", []).append({"at": now_iso(), "kind": "venue", "text": f"被 {rec['venue']} 录用"})
    src = dict(source, at=now_iso())
    key = (src.get("name"), src.get("ref"))
    if not any((s.get("name"), s.get("ref")) == key for s in m.setdefault("sources", [])):
        m["sources"].append(src)
    for s in rec.get("cites_seeds") or []:
        if s not in m.setdefault("cites_seeds", []):
            m["cites_seeds"].append(s)
    save(m)
    index.add(m)
    return pid, new
