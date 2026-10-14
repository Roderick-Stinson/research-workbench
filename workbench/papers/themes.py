"""研究主题（ADR-0041）：比维度标签更细的"研究热点"，用来看热点的变化趋势。

维度标签（优化对象 / 搜索方式 / 粒度）太粗（"整体流水线"、"其它"占大头），看不出热点怎么变。
做法：批量模型读一批按时间分层抽样的论文一句话，提出 12–20 个具体的研究主题（存 registry/themes.json，本人可以改）；
再给每篇属于方向的论文打 1–2 个主题（存在 meta 的 themes 里），新论文每天增量打。主题表每 30 天重新提议一次（保留稳定的 id）。
"""
from __future__ import annotations

import concurrent.futures as cf
import random

from .. import store as wbstore
from ..util import now_iso, read_json, write_json
from . import field as fieldmod
from . import llm, store


def path():
    return wbstore.registry_dir() / "themes.json"


def load() -> dict:
    return read_json(path(), {}) or {}


SYS_PROPOSE = """你在为研究方向「{name}」梳理研究热点。下面是按时间分层抽样的论文（发表月份 + 一句话）。
提出 12–20 个**具体的研究主题**，用来给每篇论文归类、观察热点随时间的变化：
- 主题是"研究问题 / 技术路线"层面的（例如"查询级自适应的架构生成"、"用强化学习训练编排器"、"失败归因与诊断"、"通信剪枝与 token 效率"），
  不要太宽（"多智能体系统"）也不要太窄（某一篇论文）；
- 彼此尽量不重叠，合起来能覆盖大多数论文；最后加一个 other（其它）；
- 如果给了现有主题表，尽量沿用其 id 和名字，只在确有必要时增删改。
输出 JSON：{{"themes": [{{"id": "英文小写短 id", "name": "中文名（≤ 14 字）", "desc": "判断标准：什么样的论文属于这个主题（一句话）"}}], "changes": "相对现有主题表改了什么"}}"""

SYS_ASSIGN = """给每篇论文打 1–2 个研究主题（只能用下面列表里的 id；主要贡献对应的主题放第一个；都不合适就用 other）。
主题：
{themes}
输出 JSON：{{"items": [{{"i": 序号, "themes": ["id"]}}]}}，每篇都要有。"""


def _rel():
    return [m for m in store.all_papers() if m.get("intake", {}).get("state") == "relevant" and m.get("_qr")]


def propose(log=print, sample: int = 400) -> dict:
    ms = _rel()
    by_q = {}
    for m in ms:
        by_q.setdefault((m.get("published") or "")[:7], []).append(m)
    rnd = random.Random(42)
    picked = []
    per = max(3, sample // max(1, len(by_q)))
    for k in sorted(by_q):
        xs = by_q[k][:]
        rnd.shuffle(xs)
        picked += xs[:per]
    picked = picked[:sample]
    cur = load()
    f = fieldmod.field()
    user = ("现有主题表：" + str([{k: t[k] for k in ("id", "name", "desc")} for t in cur.get("themes") or []]) + "\n\n论文：\n"
            + "\n".join(f"- {(m.get('published') or '')[:7]} {(m['_qr'].get('one_liner') or m.get('title') or '')[:160]}" for m in picked))
    res = llm.chat(SYS_PROPOSE.format(name=f["name"]), user, task="themes-propose", max_tokens=6000)
    themes = [t for t in res.get("themes") or [] if t.get("id") and t.get("name")]
    if not any(t["id"] == "other" for t in themes):
        themes.append({"id": "other", "name": "其它", "desc": "不属于以上任何主题"})
    out = {"themes": themes, "version": (cur.get("version") or 0) + 1, "created_at": now_iso(), "changes": res.get("changes")}
    write_json(path(), out)
    log(f"研究主题：{len(themes)} 个（v{out['version']}）")
    return out


def assign(pids: list, kind: str = "backfill", batch: int = 40, workers: int = 4, log=print) -> dict:
    th = load()
    ids_ = {t["id"] for t in th.get("themes") or []}
    if not ids_:
        return {"skipped": "还没有主题表"}
    sys = SYS_ASSIGN.format(themes="\n".join(f"- {t['id']}：{t['name']}——{t.get('desc', '')}" for t in th["themes"]))
    chunks = [pids[i:i + batch] for i in range(0, len(pids), batch)]

    def one(chunk):
        ms = [store.load(p) for p in chunk]
        ms = [m for m in ms if m]
        lines = []
        for i, m in enumerate(ms):
            qr = store.load_quickread(m["id"]) or {}
            lines.append(f"[{i}] {m.get('title')}\n  {qr.get('one_liner') or (m.get('abstract') or '')[:300]}")
        res = llm.chat(sys, "\n".join(lines), task="themes-assign", max_tokens=4000, kind=kind)
        n = 0
        for it in res.get("items") or []:
            try:
                m = ms[int(it["i"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            ts = [t for t in it.get("themes") or [] if t in ids_][:2] or ["other"]
            m = store.load(m["id"])
            m["themes"] = ts
            m["themes_v"] = th["version"]
            store.save(m)
            n += 1
        return n

    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, c) for c in chunks]
        for fu in cf.as_completed(futs):
            try:
                done += fu.result()
            except (llm.LLMPaused, llm.LLMNotConfigured):
                for f2 in futs:
                    f2.cancel()
                log(f"主题标注：{done} 篇后暂停（额度）")
                raise
            except Exception as e:  # noqa: BLE001
                log(f"主题标注一批失败：{e}")
    log(f"主题标注：{done} 篇")
    return {"assigned": done}


def pending() -> list:
    v = load().get("version")
    return [m["id"] for m in _rel() if m.get("themes_v") != v] if v else []
