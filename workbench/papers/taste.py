"""本人对论文的评价（ADR-0036）。

评价 = 1–5 分 + 一句话理由，存在论文的 status.json（rating），改动留在 history。
1 很差 · 2 差 · 3 一般 · 4 好 · 5 很好。

评价怎么起作用：
1. **每天的精选（自动）**：按原有分数取前 N 篇候选，交给批量模型对照"本人评价过的论文 + 理由"和画像里的口味，
   给每篇打"本人会不会喜欢"的分，与原分数加权后重排。额度不够时退回原排序，不影响出刊。
2. **画像（确认后生效）**：新评价攒够 3 条，每日流程自动起草画像修改（含"口味"：喜欢 / 不喜欢什么），
   推送里提示；本人确认后 `wb field apply` 才写入（ADR-0033）。
3. 构思时论文库里带上评价，评价低的论文不作为方法依据。
"""
from __future__ import annotations

from .. import store as wbstore
from ..util import now_iso
from . import field as fieldmod
from . import llm, store

LABELS = {1: "很差", 2: "差", 3: "一般", 4: "好", 5: "很好"}


def rate(pid: str, score: int | None, note: str | None = None, undo: bool = False) -> dict:
    st = store.load_status(pid)
    if undo:
        old = st.pop("rating", None)
        st.setdefault("history", []).append({"ts": now_iso(), "by": wbstore._actor(), "unrate": old})
    else:
        if score not in LABELS:
            raise SystemExit("评分是 1–5：1 很差 · 2 差 · 3 一般 · 4 好 · 5 很好")
        st["rating"] = {"score": score, "label": LABELS[score], "note": note, "at": now_iso(), "by": wbstore._actor()}
        st.setdefault("history", []).append({"ts": now_iso(), "by": wbstore._actor(), "rate": score, "note": note})
    store.save_status(pid, st)
    wbstore.journal("paper.rate", paper=pid, score=None if undo else score, note=note, undo=undo)
    return st.get("rating") or {}


def rated() -> list:
    out = []
    for m in store.all_papers():
        r = (m.get("_status") or {}).get("rating")
        if r:
            qr = m.get("_qr") or {}
            out.append({"id": m["id"], "title": m.get("title"), "one_liner": qr.get("one_liner"), "score": r["score"],
                        "note": r.get("note"), "at": r.get("at"), "facets": qr.get("facets"),
                        "quality_reason": (qr.get("quality") or {}).get("reason")})
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out


SYS_TASTE = """你在帮一位研究者从候选论文里挑出他会喜欢的。依据只有两样：
1. 他的口味（画像里写的喜欢 / 不喜欢什么，可能为空）；
2. 他亲自评价过的论文（1 很差 … 5 很好）和理由——这是最重要的证据：和高分论文同类、同样扎实的给高分，
   和低分论文有同样毛病（看理由）的给低分。
对每篇候选给 like（0 到 1：他会喜欢的把握）和一句中文理由（点出像哪篇评价过的论文、或者踩中 / 避开了哪个毛病）。
不评价是否属于研究方向（候选都已属于方向）。
输出 JSON：{"items": [{"i": 序号, "like": 0.0-1.0, "reason": "一句话"}]}，每篇都要有。"""


def _brief(x: dict) -> str:
    return f"{x['title']} —— {x.get('one_liner') or ''}"


def judge(cands: list, kind: str = "daily") -> dict:
    """cands: [{pid, ...}] → {pid: {like, reason}}。评价过的论文太少（< 3）时不调用模型，返回 {}。"""
    rs = rated()
    if len(rs) < 3 or not cands:
        return {}
    taste = fieldmod.field().get("taste") or {}
    ex = "\n".join(f"- [{x['score']} 分 {LABELS[x['score']]}] {_brief(x)}" + (f"\n  理由：{x['note']}" if x.get("note") else "")
                   for x in rs[:40])
    lines = []
    for i, c in enumerate(cands):
        m = store.load(c["pid"]) or {}
        qr = store.load_quickread(c["pid"]) or {}
        lines.append(f"[{i}] {m.get('title')}\n  一句话：{qr.get('one_liner') or ''}\n  质量评语：{(qr.get('quality') or {}).get('reason') or ''}")
    user = (f"口味：喜欢 {taste.get('likes') or '（未写）'}；不喜欢 {taste.get('dislikes') or '（未写）'}\n\n"
            f"他评价过的论文：\n{ex}\n\n候选论文：\n" + "\n".join(lines))
    res = llm.chat(SYS_TASTE, user, task="taste", max_tokens=6000, kind=kind)
    out = {}
    for it in res.get("items") or []:
        try:
            pid = cands[int(it["i"])]["pid"]
            out[pid] = {"like": max(0.0, min(1.0, float(it.get("like")))), "reason": it.get("reason")}
        except (KeyError, ValueError, IndexError, TypeError):
            continue
    return out


def rerank(cands: list, room: int, log=print) -> list:
    """按口味重排：total' = (1 - w)·原分数 + w·like。模型不可用时退回原排序。"""
    w = fieldmod.cfg_selection().get("taste_weight", 0.4)
    try:
        likes = judge(cands)
    except llm.LLMPaused as e:
        log(f"口味重排跳过（额度）：{e}")
        likes = {}
    except Exception as e:  # noqa: BLE001  口味重排失败不影响出刊
        log(f"口味重排跳过：{e}")
        likes = {}
    if not likes:
        return cands[:room]
    for c in cands:
        t = likes.get(c["pid"])
        if t:
            c["base_total"] = c["total"]
            c["like"], c["like_reason"] = t["like"], t["reason"]
            c["total"] = round((1 - w) * c["total"] + w * t["like"], 4)
    cands.sort(key=lambda c: -c["total"])
    log(f"口味重排：{len(likes)} 篇候选，参考 {len(rated())} 条评价")
    return cands[:room]


def new_since_profile() -> int:
    """上次画像修改（起草或写入）之后的新评价条数。"""
    from . import manual
    from ..util import read_json

    prop = read_json(manual.proposal_path()) or {}
    last = max(fieldmod.field().get("updated_at") or "", prop.get("created_at") or "")
    return sum(1 for x in rated() if (x["at"] or "") > last)
