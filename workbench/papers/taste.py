"""本人对论文的评价（ADR-0036）。

评价 = 1–5 分 + 一句话理由，存在论文的 status.json（rating），改动留在 history。
1 很差 · 2 差 · 3 一般 · 4 好 · 5 很好。

评价怎么起作用：
1. **每天的精选（自动）**：按原有分数取前 N 篇候选，先过硬门槛（rigor.py），再交给批量模型做中稿评估，
   并对照"本人评价过的论文 + 理由"和画像里的口味（画像里写了口味，或评价攒够 3 条，才算口味分），
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


SYS_TASTE = """你在帮一位研究者从候选论文里挑出值得他读的。对每篇候选给两个分：

1. accept：这篇论文能不能中 CCF-A 会议（NeurIPS / ICML / ICLR / ACL 等）主会，1–10，按严格的审稿人标准，不要客气：
   - 主要实验没有和领域常用方法正面比较（只和自己的变体 / 消融、通用做法、现成工具或模型比）→ 不超过 4；
   - 只在自建 benchmark 或少量抽样子集上评测，没有常用 benchmark → 不超过 5；
   - 贡献主要是工程拼装 / prompt 层面的增量，或者主要贡献是省 token / 省钱而效果持平 → 不超过 5；
   - 在常用 benchmark 上和领域常用方法正面比较、有明显提升、有消融 → 7–9；
   以"证据"一行为准（那是从论文结果表里统计出来的），论文自己的说法和"质量评语"只作参考（质量评语往往偏宽松）。
2. like：他会不会喜欢（0 到 1），依据他的口味和他评价过的论文（看理由）；口味里明确说不喜欢的那类论文给低分。
   没有口味、也没有评价时 like 填 null。

每篇给一句中文理由，点出最关键的依据（比如"只和 Claude Code 比，没有对比 AFlow / MaAS"）。
输出 JSON：{"items": [{"i": 序号, "accept": 1-10, "like": 0.0-1.0 或 null, "reason": "一句话"}]}，每篇都要有。"""


def _brief(x: dict) -> str:
    return f"{x['title']} —— {x.get('one_liner') or ''}"


def judge(cands: list, kind: str = "daily") -> dict:
    """cands: [{pid, evidence?}] → {pid: {accept, like, reason}}。"""
    if not cands:
        return {}
    rs = rated()
    taste = fieldmod.field().get("taste") or {}
    has_taste = len(rs) >= 3 or bool(taste.get("likes") or taste.get("dislikes"))
    ex = "\n".join(f"- [{x['score']} 分 {LABELS[x['score']]}] {_brief(x)}" + (f"\n  理由：{x['note']}" if x.get("note") else "")
                   for x in rs[:40]) or "（还没有）"
    lines = []
    for i, c in enumerate(cands):
        m = store.load(c["pid"]) or {}
        qr = store.load_quickread(c["pid"]) or {}
        venue = m.get("venue") or qr.get("venue_claim")
        lines.append(f"[{i}] {m.get('title')}" + (f"（{venue}）" if venue else "")
                     + f"\n  一句话：{qr.get('one_liner') or ''}\n  证据：{c.get('evidence_text') or '（没有抽取到结果）'}"
                     + f"\n  质量评语：{(qr.get('quality') or {}).get('reason') or ''}")
    user = (f"口味：喜欢 {taste.get('likes') or '（未写）'}；不喜欢 {taste.get('dislikes') or '（未写）'}\n\n"
            f"他评价过的论文：\n{ex}\n\n候选论文：\n" + "\n".join(lines))
    res = llm.chat(SYS_TASTE, user, task="taste", max_tokens=8000, kind=kind)
    out = {}
    for it in res.get("items") or []:
        try:
            pid = cands[int(it["i"])]["pid"]
            like = it.get("like")
            out[pid] = {"accept": max(1.0, min(10.0, float(it.get("accept")))),
                        "like": max(0.0, min(1.0, float(like))) if like is not None and has_taste else None,
                        "reason": it.get("reason")}
        except (KeyError, ValueError, IndexError, TypeError):
            continue
    return out


def rerank(cands: list, room: int, log=print, strict: bool = False) -> list:
    """精选的最后一步（ADR-0036、0037）：
    1. 硬门槛：方法论文没和领域常用方法比较 → 去掉（rigor.gate）；
    2. 中稿评估 + 口味：总分 = 0.3·原分数 + 0.4·中稿评估 + 0.3·喜欢（没有口味时 0.4·原分数 + 0.6·中稿评估），
       中稿评估低于 selection.min_accept（默认 5）的不进精选；
    模型不可用时：每日流程（strict=False）只用门槛 + 原分数，不耽误出刊；本人要求的重新精选（strict=True）抛出。"""
    from . import rigor, results

    sel = fieldmod.cfg_selection()
    rows = results.all_rows()
    std = rigor.standards(rows)
    kept, dropped = [], 0
    for c in cands:
        m = store.load(c["pid"]) or {}
        qr = store.load_quickread(c["pid"]) or {}
        ev = rigor.evidence(c["pid"], rows, std)
        c["evidence"] = ev
        c["evidence_text"] = rigor.text(ev)
        why = rigor.gate(m, qr, ev)
        if why:
            dropped += 1
            continue
        kept.append(c)
    log(f"硬门槛：{len(cands)} 篇候选去掉 {dropped} 篇（方法论文没和领域常用方法比较）")
    pool = kept[:max(room * 3, 30)]
    try:
        js = judge(pool)
    except llm.LLMPaused as e:
        if strict:
            raise
        log(f"中稿评估 / 口味重排跳过（额度）：{e}")
        js = {}
    except Exception as e:  # noqa: BLE001  评估失败不影响出刊
        if strict:
            raise
        log(f"中稿评估 / 口味重排跳过：{e}")
        js = {}
    if not js:
        return kept[:room]
    min_acc = sel.get("min_accept", 5)
    out = []
    for c in pool:
        j = js.get(c["pid"])
        if not j:
            continue
        c["base_total"] = c["total"]
        c["accept"], c["like"], c["like_reason"] = j["accept"], j["like"], j["reason"]
        if j["accept"] < min_acc:
            continue
        if j["like"] is not None and j["like"] < min_like():
            continue  # 口味过滤（ADR-0039）
        if j["like"] is None:
            c["total"] = round(0.4 * c["total"] + 0.6 * j["accept"] / 10, 4)
        else:
            c["total"] = round(0.3 * c["total"] + 0.4 * j["accept"] / 10 + 0.3 * j["like"], 4)
        out.append(c)
    out.sort(key=lambda c: -c["total"])
    log(f"中稿评估 + 口味：评估 {len(js)} 篇，{len(out)} 篇达到门槛（中稿评估 ≥ {min_acc}），取 {min(room, len(out))} 篇")
    return out[:room]


SYS_SCREEN = """你在帮一位研究者过滤推给他的东西（论文、代码仓库、研究想法）。依据他的口味（画像里写的喜欢 / 不喜欢）
和他评价过的论文与理由，判断每一项他会不会喜欢：like 0 到 1；口味里明确说不喜欢的那类（看主要贡献是什么）给低分。
只判断合不合他的口味，不判断是否属于研究方向（都已属于方向）。每项给一句中文理由。
输出 JSON：{"items": [{"i": 序号, "like": 0.0-1.0, "reason": "一句话"}]}，每项都要有。"""


def has_taste() -> bool:
    t = fieldmod.field().get("taste") or {}
    return bool(t.get("likes") or t.get("dislikes")) or len(rated()) >= 3


def screen(items: list, log=print, batch: int = 40) -> dict:
    """口味过滤（ADR-0039）：items = [{key, kind: 论文 / 代码仓库 / 研究想法, title, text}] → {key: {like, reason}}。
    每次调用最多 batch 项（太多会让网关超时）。没有口味、模型不可用时返回 {}（调用方照常展示，不过滤）。"""
    if not items or not has_taste():
        return {}
    if len(items) > batch:
        out = {}
        for i in range(0, len(items), batch):
            part = screen(items[i:i + batch], log=log, batch=batch)
            if not part:
                return {}  # 有一批失败：整体不过滤，避免一半过滤一半没过滤
            out.update(part)
        return out
    taste = fieldmod.field().get("taste") or {}
    ex = "\n".join(f"- [{x['score']} 分 {LABELS[x['score']]}] {_brief(x)}" + (f"\n  理由：{x['note']}" if x.get("note") else "")
                   for x in rated()[:40]) or "（还没有）"
    lines = "\n".join(f"[{i}]（{it.get('kind', '论文')}）{it.get('title')}\n  {(it.get('text') or '')[:400]}" for i, it in enumerate(items))
    user = (f"口味：喜欢 {taste.get('likes') or '（未写）'}；不喜欢 {taste.get('dislikes') or '（未写）'}\n\n"
            f"他评价过的论文：\n{ex}\n\n要过滤的：\n{lines}")
    try:
        res = llm.chat(SYS_SCREEN, user, task="taste-screen", max_tokens=6000)
    except Exception as e:  # noqa: BLE001  过滤失败就不过滤，不让推送空着
        log(f"口味过滤跳过：{e}")
        return {}
    out = {}
    for it in res.get("items") or []:
        try:
            out[items[int(it["i"])]["key"]] = {"like": max(0.0, min(1.0, float(it.get("like")))), "reason": it.get("reason")}
        except (KeyError, ValueError, IndexError, TypeError):
            continue
    return out


def min_like() -> float:
    return fieldmod.cfg_selection().get("min_like", 0.4)


def keep(items: list, key=lambda x: x["key"], log=print, label: str = "") -> list:
    """过滤并把 like / like_reason 写回每一项；返回留下的。"""
    sc = screen(items, log=log)
    if not sc:
        return items
    out = []
    for it in items:
        s = sc.get(key(it))
        if s:
            it["like"], it["like_reason"] = s["like"], s["reason"]
            if s["like"] < min_like():
                continue
        out.append(it)
    log(f"口味过滤{('（' + label + '）') if label else ''}：{len(items)} 项去掉 {len(items) - len(out)} 项")
    return out


def new_since_profile() -> int:
    """上次画像修改（起草或写入）之后的新评价条数。"""
    from . import manual
    from ..util import read_json

    prop = read_json(manual.proposal_path()) or {}
    last = max(fieldmod.field().get("updated_at") or "", prop.get("created_at") or "")
    return sum(1 for x in rated() if (x["at"] or "") > last)
