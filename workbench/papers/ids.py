"""论文身份：arXiv ID > DOI > 归一化标题（ADR-0027 去重规则）。"""
from __future__ import annotations

import hashlib
import re
import unicodedata


def arxiv_id(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(?:arxiv\.org/(?:abs|pdf|html)/|arxiv[:\s/]|10\.48550/arxiv\.)(\d{4}\.\d{4,5})", s, re.I)
    if m:
        return m.group(1)
    m = re.fullmatch(r"\s*(\d{4}\.\d{4,5})(v\d+)?\s*", s)
    return m.group(1) if m else None


def norm_doi(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip().lower()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    return s or None


def norm_title(t: str | None) -> str:
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t).lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def paper_id(arxiv: str | None = None, doi: str | None = None, title: str | None = None) -> str:
    """稳定的论文目录名。"""
    if arxiv:
        return f"arxiv-{arxiv}"
    if doi:
        if doi.startswith("10.48550/arxiv."):
            return f"arxiv-{doi.split('arxiv.', 1)[1]}"
        return "doi-" + re.sub(r"[^a-z0-9._-]+", "_", doi)[:120]
    nt = norm_title(title)
    return "t-" + hashlib.sha1(nt.encode()).hexdigest()[:16]
