"""本人手动提供论文 + 据此更新研究画像（ADR-0033）。

wb paper add <arXiv号 | 链接 | 标题 | 服务器上的 PDF 路径>... [--seed] [--why "..."] [--explain]
  直接进库（不再判相关性，来源标"本人提供"），立刻做速读，可选设为种子、做 8 步精读。
  本人提供的论文不进每日精选（已经读过 / 知道了），但参与领域地图、结果表、构思和画像。

wb field suggest / wb field apply
  根据种子、本人提供的论文、标为不相关的论文、本人的评价，起草新的方向描述 / 子主题 / 检索词 / 口味；本人确认后才写入。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .. import config
from .. import store as wbstore
from ..util import now_iso, read_json, write_json
from . import field as fieldmod
from . import http, ids, intake, llm, sources, store

S2 = "https://api.semanticscholar.org/graph/v1/paper"


def _from_s2(p: dict) -> dict:
    ext = p.get("externalIds") or {}
    aid = ext.get("ArXiv")
    return {"arxiv": aid, "doi": ext.get("DOI"), "title": (p.get("title") or "").strip(),
            "abstract": p.get("abstract") or "", "published": p.get("publicationDate") or (str(p["year"]) if p.get("year") else None),
            "venue": sources.short_venue(p.get("venue")), "authors": [a.get("name") for a in p.get("authors") or []],
            "url": f"https://arxiv.org/abs/{aid}" if aid else p.get("url"),
            "pdf_url": f"https://arxiv.org/pdf/{aid}" if aid else None}


def _pdf_title(path: Path) -> str | None:
    try:
        info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"^Title:\s*(.+)$", info, re.M)
        if m and len(m.group(1).strip()) > 10:
            return m.group(1).strip()
        first = subprocess.run(["pdftotext", "-l", "1", str(path), "-"], capture_output=True, text=True, timeout=30).stdout
        lines = [x.strip() for x in first.splitlines() if len(x.strip()) > 10]
        return lines[0] if lines else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def resolve(ref: str) -> tuple:
    """返回 (rec, 本地 PDF 路径或 None)。找不到抛 SystemExit，说明原因。"""
    ref = ref.strip()
    local = Path(os.path.expanduser(ref))
    if local.suffix.lower() == ".pdf" and local.exists():
        title = _pdf_title(local)
        rec = None
        if title:
            try:
                d = http.get(f"{S2}/search/match", {"query": title, "fields": sources.S2_FIELDS}, ttl=86400)
                if (d.get("data") or []) and ids.norm_title(d["data"][0].get("title")) == ids.norm_title(title):
                    rec = _from_s2(d["data"][0])
            except Exception:  # noqa: BLE001
                rec = None
        return rec or {"title": title or local.stem, "abstract": ""}, local
    aid = ids.arxiv_id(ref)
    if aid:
        rows = sources.arxiv_by_ids([aid])
        if rows:
            return rows[0], None
        raise SystemExit(f"arXiv 上找不到 {aid}")
    if ref.startswith(("http://", "https://")):
        try:
            return _from_s2(http.get(f"{S2}/URL:{ref}", {"fields": sources.S2_FIELDS}, ttl=86400)), None
        except Exception:  # noqa: BLE001
            raise SystemExit(f"认不出这个链接对应的论文：{ref}（可以改给 arXiv 号、标题，或把 PDF 放到服务器上给路径）") from None
    try:
        d = http.get(f"{S2}/search/match", {"query": ref, "fields": sources.S2_FIELDS}, ttl=86400)
    except Exception:  # noqa: BLE001
        d = {}
    hits = d.get("data") or []
    if not hits:
        raise SystemExit(f"按标题没找到：{ref}")
    return _from_s2(hits[0]), None


def add(refs: list, seed: bool = False, why: str = "本人提供", explain: bool = False, log=print) -> list:
    index = store.Index()
    out = []
    for ref in refs:
        rec, pdf = resolve(ref)
        pid, new = store.upsert(index, rec, {"name": "manual", "ref": ref[:200], "why": why})
        m = store.load(pid)
        m["manual"] = {"at": now_iso(), "by": wbstore._actor(), "why": why}
        m["intake"] = {"state": "relevant", "relevance": 1.0, "reason": f"本人提供：{why}", "judged_at": now_iso()}
        store.save(m)
        if pdf:
            store.pdir(pid).mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf, store.pdir(pid) / "paper.pdf")
            m = store.load(pid)
            m["pdf_url"] = None  # 用本地 PDF
            store.save(m)
        res = {"pid": pid, "title": m.get("title"), "new": new}
        if seed:
            if m.get("arxiv"):
                s = fieldmod.seeds()
                if not any(x["arxiv"] == m["arxiv"] for x in s):
                    s.append({"arxiv": m["arxiv"], "title": m.get("title"), "why": why, "added_by": wbstore._actor(),
                              "added_at": now_iso()})
                    fieldmod.save_seeds(s)
                res["seed"] = True
            else:
                res["seed"] = "没有 arXiv 号，不能当种子（种子靠 arXiv 号追踪引用）"
        try:
            if not store.load_quickread(pid):
                store.save_quickread(pid, intake.quickread(store.load(pid)))
            res["quickread"] = True
            if explain:
                from . import explain as ex
                res["explain"] = ex.explain_one(pid).get("ok")
        except llm.LLMPaused as e:
            res["quickread"] = f"额度暂停，恢复后由回填补做速读：{e}"
        wbstore.journal("paper.add", paper=pid, ref=ref[:200], seed=bool(seed), why=why)
        log(f"{pid}：{m.get('title')}  {'（新收录）' if new else '（库里已有，标为本人提供）'}")
        out.append(res)
    return out


# ---------------- 画像 ----------------

SUGGEST_SYS = """你在帮研究者维护"研究方向画像"，它决定每天抓哪些论文、怎么判断相关。根据下面的材料，提出修改建议：
- 种子论文与本人提供的论文：说明本人关心什么（要覆盖到）；
- 本人标为"不相关"的论文：说明本人不关心什么（描述里要能把它们排除）；
- 本人评价过的论文（1 很差 … 5 很好，带理由）：说明本人的口味——喜欢什么样的工作、讨厌什么样的毛病；
  口味写进 taste（likes / dislikes 各 3–8 条，具体到可以判断一篇新论文，例如"只在 prompt 层面做增量、基线不全的"）；
- 当前画像：尽量保持稳定，只做必要的修改。
arxiv_keywords 是 arXiv 检索词：每个词单独查 arXiv，所以要宽一点（能召回）而不是窄（相关性另有模型判断）；
写法：单个词或短语（如 "multi-agent"），或者 "abs:xxx AND abs:yyy" / "ti:xxx" 这样的 arXiv 查询；数量 6–15 个。
输出 JSON：{"description": "", "subtopics": [""], "arxiv_keywords": [""], "taste": {"likes": [""], "dislikes": [""]},
"changes": ["改了什么、为什么（引用材料里的论文与评价）"]}"""


def proposal_path() -> Path:
    return wbstore.registry_dir() / "field.proposed.json"


def suggest(log=print) -> dict:
    f = fieldmod.field()
    papers = store.all_papers()
    seeds = {s["arxiv"] for s in fieldmod.seeds()}

    def brief(m):
        qr = m.get("_qr") or {}
        return {"title": m.get("title"), "one_liner": qr.get("one_liner"), "abstract": (m.get("abstract") or "")[:600]}

    pos = [brief(m) for m in papers if m.get("arxiv") in seeds or m.get("manual")]
    neg = [dict(brief(m), note=(m.get("_status") or {}).get("note")) for m in papers if (m.get("_status") or {}).get("irrelevant")]
    rated = [dict(brief(m), rating=m["_status"]["rating"]["score"], reason=m["_status"]["rating"].get("note"))
             for m in papers if (m.get("_status") or {}).get("rating")]
    if not pos and not rated:
        raise SystemExit("还没有种子、本人提供或评价过的论文：先 wb paper add / wb seed add / wb paper rate")
    user = json.dumps({"current": {k: f.get(k) for k in ("name", "description", "subtopics", "arxiv_keywords", "taste")},
                       "positive_papers": pos[:60], "negative_papers": neg[:40], "rated_papers": rated[:80]}, ensure_ascii=False)
    res = llm.chat(SUGGEST_SYS, user, task="field-suggest", max_tokens=6000)
    prop = {"created_at": now_iso(), "based_on": {"positive": len(pos), "negative": len(neg), "rated": len(rated)},
            "description": res.get("description"), "subtopics": res.get("subtopics"),
            "arxiv_keywords": res.get("arxiv_keywords"), "taste": res.get("taste"), "changes": res.get("changes"),
            "before": {k: f.get(k) for k in ("description", "subtopics", "arxiv_keywords", "taste")}}
    write_json(proposal_path(), prop)
    return prop


def apply(log=print) -> dict:
    prop = read_json(proposal_path())
    if not prop:
        raise SystemExit("没有待确认的画像修改：先 wb field suggest")
    f = fieldmod.field()
    hist = config.home() / "registry" / "field.history"
    hist.mkdir(parents=True, exist_ok=True)
    write_json(hist / f"field-{now_iso().replace(':', '')}.json", f)  # 旧版本留底
    for k in ("description", "subtopics", "arxiv_keywords", "taste"):
        if prop.get(k):
            f[k] = prop[k]
    f["updated_at"] = now_iso()
    fieldmod.save_field(f)
    proposal_path().unlink()
    wbstore.journal("field.apply", changes=prop.get("changes"))
    return {"applied": True, "changes": prop.get("changes")}
