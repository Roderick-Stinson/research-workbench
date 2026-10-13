"""识别顶会顶刊（ADR-0040）：CCF-A 会议 / 期刊（含 TPAMI）、ICLR（CCF 未列，公认顶会，单独标注）、Nature / Science 子刊。

输入是各种来源的录用信息字符串（Semantic Scholar 的 venue / publicationVenue / journal、arXiv 评论、速读里作者自称的会议），
输出 {key, name, tier, year}。研讨会（workshop）、Findings、companion / demo / tutorial / 博士论坛等不算主会。
"""
from __future__ import annotations

import re

# (key, 显示名, 档次, 匹配的正则（小写）)。正则尽量用全称，避免误匹配。
TOP = [
    # —— CCF-A 会议（人工智能 / 机器学习 / NLP / CV）——
    ("NeurIPS", "NeurIPS", "CCF-A 会议", r"\bneurips\b|neural information processing systems|\bnips\b"),
    ("ICML", "ICML", "CCF-A 会议", r"\bicml\b|international conference on machine learning"),
    ("ACL", "ACL", "CCF-A 会议", r"^acl\b|\bacl \d{4}|\bacl'?\d{2}\b|annual meeting of the association for computational linguistics"),
    ("AAAI", "AAAI", "CCF-A 会议", r"\baaai\b(?!.*symposium)|aaai conference on artificial intelligence"),
    ("IJCAI", "IJCAI", "CCF-A 会议", r"\bijcai\b|international joint conference on artificial intelligence"),
    ("CVPR", "CVPR", "CCF-A 会议", r"\bcvpr\b|computer vision and pattern recognition"),
    ("ICCV", "ICCV", "CCF-A 会议", r"\biccv\b|international conference on computer vision"),
    # —— CCF-A 会议（数据挖掘 / 检索 / Web / 软件工程 / 系统 / 数据库 / 人机交互）——
    ("KDD", "KDD", "CCF-A 会议", r"\bkdd\b|knowledge discovery and data mining"),
    ("SIGIR", "SIGIR", "CCF-A 会议", r"\bsigir\b|research and development in information retrieval"),
    ("WWW", "WWW", "CCF-A 会议", r"\bwww\b ?'?\d{0,4}|the web conference|world wide web conference"),
    ("ICSE", "ICSE", "CCF-A 会议", r"\bicse\b|international conference on software engineering"),
    ("FSE", "FSE", "CCF-A 会议", r"\besec/fse\b|\bfse\b|foundations of software engineering"),
    ("ASE", "ASE", "CCF-A 会议", r"\base\b ?'?\d{0,4}|automated software engineering"),
    ("ISSTA", "ISSTA", "CCF-A 会议", r"\bissta\b|software testing and analysis"),
    ("OOPSLA", "OOPSLA", "CCF-A 会议", r"\boopsla\b"),
    ("PLDI", "PLDI", "CCF-A 会议", r"\bpldi\b"),
    ("SIGMOD", "SIGMOD", "CCF-A 会议", r"\bsigmod\b"),
    ("VLDB", "VLDB", "CCF-A 会议", r"\bvldb\b|proceedings of the vldb endowment"),
    ("ICDE", "ICDE", "CCF-A 会议", r"\bicde\b|international conference on data engineering"),
    ("OSDI", "OSDI", "CCF-A 会议", r"\bosdi\b"),
    ("SOSP", "SOSP", "CCF-A 会议", r"\bsosp\b"),
    ("NSDI", "NSDI", "CCF-A 会议", r"\bnsdi\b"),
    ("ATC", "USENIX ATC", "CCF-A 会议", r"usenix annual technical conference|\busenix atc\b"),
    ("EuroSys", "EuroSys", "CCF-A 会议", r"\beurosys\b"),
    ("ASPLOS", "ASPLOS", "CCF-A 会议", r"\basplos\b"),
    ("MM", "ACM MM", "CCF-A 会议", r"\bacm mm\b|acm (international conference on )?multimedia\b"),
    ("CHI", "CHI", "CCF-A 会议", r"\bchi\b ?'?\d{0,4}|human factors in computing systems"),
    ("CSCW", "CSCW", "CCF-A 会议", r"\bcscw\b|computer[- ]supported cooperative work"),
    ("CCS", "CCS", "CCF-A 会议", r"\bacm ccs\b|computer and communications security"),
    ("S&P", "IEEE S&P", "CCF-A 会议", r"ieee symposium on security and privacy|\bieee s&p\b"),
    ("USENIX Security", "USENIX Security", "CCF-A 会议", r"usenix security"),
    ("NDSS", "NDSS", "CCF-A 会议", r"\bndss\b"),
    # —— ICLR：CCF 目录未收录，但公认顶会 ——
    ("ICLR", "ICLR", "ICLR（CCF 未列，公认顶会）", r"\biclr\b|international conference on learning representations"),
    # —— CCF-A 期刊 ——
    ("TPAMI", "TPAMI", "CCF-A 期刊", r"\btpami\b|pattern analysis and machine intelligence"),
    ("IJCV", "IJCV", "CCF-A 期刊", r"\bijcv\b|international journal of computer vision"),
    ("JMLR", "JMLR", "CCF-A 期刊", r"\bjmlr\b|journal of machine learning research"),
    ("AIJ", "Artificial Intelligence", "CCF-A 期刊", r"^artificial intelligence$|artificial intelligence journal|\baij\b"),
    ("TOSEM", "TOSEM", "CCF-A 期刊", r"\btosem\b|transactions on software engineering and methodology"),
    ("TSE", "TSE", "CCF-A 期刊", r"\btse\b|ieee transactions on software engineering"),
    ("TKDE", "TKDE", "CCF-A 期刊", r"\btkde\b|transactions on knowledge and data engineering"),
    ("TOIS", "TOIS", "CCF-A 期刊", r"\btois\b|transactions on information systems"),
    ("TIP", "TIP", "CCF-A 期刊", r"\btip\b|transactions on image processing"),
    ("TPDS", "TPDS", "CCF-A 期刊", r"\btpds\b|transactions on parallel and distributed systems"),
    ("JACM", "JACM", "CCF-A 期刊", r"\bjacm\b|journal of the acm"),
    ("VLDBJ", "VLDB Journal", "CCF-A 期刊", r"\bvldb journal\b"),
    # —— Nature / Science 及子刊 ——
    ("Nature", "Nature", "Nature / Science", r"^nature$"),
    ("Science", "Science", "Nature / Science", r"^science$"),
    ("NatureSub", None, "Nature 子刊", r"^nature [a-z]"),
    ("ScienceSub", None, "Science 子刊", r"^science (advances|robotics)"),
]
_RX = [(k, n, t, re.compile(p, re.I)) for k, n, t, p in TOP]
NOT_MAIN = re.compile(r"workshop|findings|companion|\bdemo\b|tutorial|doctoral|student research|extended abstract|"
                      r"symposium on (?!security)|challenge|competition|\bw\d+\b|\bwork-in-progress\b|poster abstract|"
                      r"\bnier\b|industry day|preliminary version", re.I)
NOT_VENUE = re.compile(r"\barxiv\b|\bcorr\b|under review|submitted to|in submission", re.I)


def classify(text: str | None) -> dict | None:
    """一个录用信息字符串 → {key, name, tier, year}；不是顶会顶刊主会 / 主刊时返回 None。"""
    if not text or not isinstance(text, str):
        return None
    t = re.sub(r"\s+", " ", text).strip()
    if NOT_MAIN.search(t) or NOT_VENUE.search(t):
        return None
    for key, name, tier, rx in _RX:
        if rx.search(t):
            y = re.search(r"\b(20\d{2})\b", t) or re.search(r"['’](\d{2})\b", t)
            year = (int(y.group(1)) if len(y.group(1)) == 4 else 2000 + int(y.group(1))) if y else None
            if key in ("NatureSub", "ScienceSub"):
                name = re.sub(r"\s*\(.*\)$", "", t).strip().title().replace("Of", "of")
            return {"key": key, "name": name, "tier": tier, "year": year}
    return None


DOI_NOT_MAIN = re.compile(r"findings|workshop|[./-]ws[./-]|-demo|demos|srw|industry|tutorial|\.wn\.|-short-findings", re.I)


def not_main_doi(doi: str | None) -> bool:
    """S2 不区分 ACL 主会和 Findings / 研讨会，但 ACL Anthology 的 DOI 区分（2025.findings-acl.123 vs 2025.acl-long.123）。"""
    return bool(doi) and bool(DOI_NOT_MAIN.search(doi))


def best(candidates: list) -> dict | None:
    """多个来源（S2 > arXiv 评论 > 作者自称）里第一个识别成功的。candidates = [(来源, 字符串)]。"""
    for src, text in candidates:
        c = classify(text)
        if c:
            return dict(c, source=src, raw=text[:160])
    return None
