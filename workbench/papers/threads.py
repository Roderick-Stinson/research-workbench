"""研究线（ADR-0043）：少而深、持续推进的构思。

一条研究线 = 一个具体问题，持续多天推进：每天由 codex 推进一步（精读关键论文 / 查新 / 分析失败样本 / 形式化方法 / 设计最小实验 /
修改 IDEA 草稿），发现与证据记在线上；草稿成熟后交独立审稿，通过就成为正式 IDEA 并告知本人，没通过就按审稿意见继续，
审稿 3 次仍不过就搁置并写明原因。同时最多推进 3 条。

来源（优先级从高到低）：
- mine：本人自己的研究问题（`wb thread add "问题" --mine`）；
- repro：复现与 bad case——本人显式配置的结果目录（ideation.badcase_roots）里各方法的逐样本预测与评测（配置了才自动建一条）；
- open：领域里的开放问题——以往接近通过的候选（没被否决、均分 ≥ 6，已有审稿意见可用）优先，其次是地图里各类"没考虑到什么"；
- scout：每周一次的广撒网构思（ideate.run）里接近通过的，也进 open 的候选。
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
from pathlib import Path

from .. import config
from .. import store as wbstore
from ..util import now_iso, read_json, write_json
from . import codex, fieldmap, ideate, llm
from . import field as fieldmod

SOURCES = {"mine": "我的研究问题", "repro": "复现与 bad case", "open": "领域开放问题"}
PRIORITY = {"mine": 0, "repro": 1, "open": 2}


def cfg() -> dict:
    """本人的工作区和结果目录只用显式配置的（ideation.workspace / ideation.badcase_roots），不从 baseline_dir 推断——
    那里可能只是备份（2026-10-17 本人纠正）。"""
    c = (fieldmod.field().get("ideation") or {})
    return {"max_active": c.get("max_active_threads", 3), "max_reviews": c.get("max_reviews_per_thread", 3),
            "badcase_roots": c.get("badcase_roots") or [], "workspace": c.get("workspace") or None,
            "scout_weekday": c.get("scout_weekday", 0)}


def tdir() -> Path:
    d = ideate.ideas_dir() / "threads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(tid: str) -> dict | None:
    return read_json(tdir() / tid / "thread.json")


def save(t: dict) -> None:
    t["updated_at"] = now_iso()
    write_json(tdir() / t["id"] / "thread.json", t)


def all_threads() -> list:
    out = [read_json(p) for p in tdir().glob("*/thread.json")]
    return sorted([t for t in out if t], key=lambda t: (t.get("status") != "active", PRIORITY.get(t.get("source"), 9), t.get("created_at") or ""))


def add(question: str, source: str = "mine", context: dict | None = None, title: str | None = None,
        seed: dict | None = None, status: str | None = None) -> dict:
    if source not in SOURCES:
        raise SystemExit(f"来源只能是 {list(SOURCES)}")
    tid = ideate._id("T", title or question)
    t = {"id": tid, "title": title or question[:40], "question": question, "source": source, "context": context or {},
         "status": status or "queued", "created_at": now_iso(), "steps": [], "reviews": [], "idea_draft": (seed or {}).get("draft"),
         "next_steps": (seed or {}).get("next_steps") or [], "ready_for_review": False, "by": wbstore._actor()}
    if seed and seed.get("review"):
        t["reviews"].append(dict(seed["review"], at=now_iso(), from_seed=True))
    save(t)
    wbstore.journal("thread.add", thread=tid, source=source, question=question[:200])
    return t


def set_status(tid: str, status: str, reason: str | None = None) -> dict:
    t = load(tid)
    if not t:
        raise SystemExit(f"没有研究线 {tid}")
    t["status"] = status
    t.setdefault("history", []).append({"ts": now_iso(), "status": status, "reason": reason, "by": wbstore._actor()})
    if reason:
        t["status_reason"] = reason
    save(t)
    wbstore.journal(f"thread.{status}", thread=tid, reason=reason)
    return t


# ---------------- 新研究线从哪来 ----------------

def _badcase_index(limit: int = 400) -> list:
    """结果目录里有逐样本预测的运行：方法 / 数据集 / 运行目录 / 文件。"""
    out = []
    for root in cfg()["badcase_roots"]:
        for f in sorted(Path(root).glob("**/predictions.jsonl")):
            parts = f.relative_to(root).parts
            if any(p.startswith(("_smoke", "_quarantine", "_supervisor", "_evaluator")) for p in parts):
                continue
            out.append({"path": str(f.parent), "files": sorted(x.name for x in f.parent.iterdir())[:20], "rel": "/".join(parts[:-1])})
            if len(out) >= limit:
                return out
    return out


def _seed_candidates() -> list:
    """开放问题的候选：以往接近通过、还没开过线的 IDEA。"""
    started = {(t.get("context") or {}).get("from_idea") for t in all_threads()}
    out = []
    for i in ideate.all_ideas(include_hidden=True):
        if i.get("informed") or i["id"] in started:
            continue
        rv = read_json(ideate.ideas_dir() / ".hidden" / i["id"] / "review.json") or {}
        if rv.get("verdict") == "error" or rv.get("novelty_veto") or rv.get("problem_veto") or (rv.get("mean") or 0) < 6:
            continue
        c = read_json(ideate.ideas_dir() / ".hidden" / i["id"] / "candidate.json") or {}
        out.append({"idea": i, "candidate": c, "review": rv})
    out.sort(key=lambda x: -(x["review"].get("mean") or 0))
    return out


def fill_slots(log=print) -> list:
    """活跃的研究线不足上限时补上：排队的 mine > repro（有结果目录且还没有）> 接近通过的候选。"""
    c = cfg()
    ts = all_threads()
    active = [t for t in ts if t["status"] == "active"]
    started = []
    room = c["max_active"] - len(active)
    for t in sorted([t for t in ts if t["status"] == "queued"], key=lambda t: (PRIORITY.get(t["source"], 9), t["created_at"])):
        if room <= 0:
            break
        t["status"] = "active"
        save(t)
        started.append(t)
        room -= 1
    if room > 0 and c["badcase_roots"] and not any(t["source"] == "repro" and t["status"] in ("active", "queued") for t in ts):
        idx = _badcase_index()
        if idx:
            t = add("对比各方法在同一数据集上的逐样本结果，找出反复出现、现有方法都没解决的失败模式，并从中提炼可以发 CCF-A 的问题",
                    "repro", {"badcase_roots": c["badcase_roots"]}, title="复现结果里的共同失败模式", status="active")
            started.append(t)
            room -= 1
    for s in _seed_candidates():
        if room <= 0:
            break
        cand, rv = s["candidate"], s["review"]
        q = ((cand.get("problem") or {}).get("title")) or cand.get("summary") or s["idea"]["title"]
        t = add(q, "open", {"from_idea": s["idea"]["id"]}, title=s["idea"]["title"], status="active",
                seed={"draft": cand, "review": {k: rv.get(k) for k in ("mean", "scores", "summary", "weaknesses", "suggestions")},
                      "next_steps": ["按上次审稿意见补形式化定义与优化目标", "找最接近的 ≥ 3 篇工作并说清本质区别", "把实验收缩到预算内"]})
        started.append(t)
        room -= 1
    for t in started:
        log(f"研究线开始：{t['title']}（{SOURCES[t['source']]}）")
    return started


# ---------------- 推进一步 ----------------

STEP_PROMPT = """你在推进一条研究线（研究方向「{name}」），目标是做出一个能中 CCF-A（NeurIPS / ICLR 等）的研究 IDEA。
当前目录的材料：
- thread.json：这条研究线——问题、来源、已经做过的每一步和发现（steps）、当前 IDEA 草稿（idea_draft）、审稿记录（reviews）、下一步（next_steps）
- library.json：论文库（一句话、方法、局限、维度、影响力；全文在 {home}/papers/<id>/fulltext.txt）
- map.json：领域地图；past_reviews.json：以前被拒的候选与审稿意见
- badcase_index.json：结果目录里有逐样本预测的运行（predictions.jsonl 等；评测结果可能在同一目录或 _private_evaluation 下）{workspace}

今天做**一步**：从 next_steps 和审稿意见里挑最有价值的一件事做扎实，例如——
精读 1–3 篇关键论文（读全文，记下事实、公式、数字和出处）；联网查新，找最接近的工作并写清区别；
读逐样本预测，统计并归纳失败模式（写清方法 / 数据集 / 文件路径 / 样本 id / 原文片段 / 数量）；形式化方法（定义对象、目标、算法、可检验的性质）；
设计最小但有说服力的实验（6 张 GPU（4090 / L40S）+ 有限 API、2–3 个月）；按审稿意见修改 IDEA 草稿。
硬性要求：所有事实和数字必须来自你读过的论文原文或结果文件，写明出处；不可以猜、不可以编。

把结果写到 ./update.json：
{{"did": "今天做了什么（一句话）",
  "findings": [{{"text": "发现（具体，带数字）", "refs": ["论文 id 或 文件路径#样本 id"]}}],
  "idea_draft": null 或 {{"title": "", "summary": "", "problem": {{"title": "", "source": "papers|badcase",
                 "evidence": [{{"type": "paper|run", "ref": "", "where": "", "quote": "", "numbers": ""}}]}},
                 "method": "方法（形式化：对象、目标 / 损失、算法）", "closest_work": ["最接近的工作及本质区别"],
                 "expected_contribution": "", "minimal_experiment": ""}},
  "next_steps": ["下一步（≤ 3 条，具体）"],
  "ready_for_review": false,
  "park": null 或 "这条线走不通的原因（问题不真实、已被完全解决、证据推翻了假设……）"}}
只有当草稿满足：问题证据逐条核对过、找过 ≥ 3 篇最接近工作并说清区别、方法形式化、实验在预算内，才把 ready_for_review 设为 true。
完成后只回复 DONE 或 FAILED: 原因。"""


def step(t: dict, day: str, log=print) -> dict:
    w = ideate.ideas_dir() / ".work" / "threads" / t["id"] / day
    if w.exists():
        shutil.rmtree(w)
    w.mkdir(parents=True)
    write_json(w / "thread.json", t)
    (w / "library.json").write_text(json.dumps(fieldmap._library_digest(), ensure_ascii=False))
    (w / "map.json").write_text(json.dumps(fieldmap.load() or {}, ensure_ascii=False))
    (w / "past_reviews.json").write_text(json.dumps(ideate.past_reviews(), ensure_ascii=False))
    write_json(w / "badcase_index.json", _badcase_index() if t["source"] in ("repro", "mine") else [])
    c = cfg()
    ws = f"\n- 本人的工作区：{c['workspace']}（代码、文档、结果都在这里，可以读）" if c["workspace"] and t["source"] in ("mine", "repro") else ""
    res = codex.run(f"thread:{t['id']}:{day}", w, STEP_PROMPT.format(name=fieldmod.field()["name"], home=config.home(), workspace=ws),
                    network=True)
    up = read_json(w / "update.json")
    if not res["ok"] or not up:
        raise RuntimeError(f"研究线 {t['id']} 这一步没做成：{res.get('error') or '没有产出 update.json'}")
    t = load(t["id"])
    t["steps"].append({"day": day, "at": now_iso(), "did": up.get("did"), "findings": up.get("findings") or []})
    if up.get("idea_draft"):
        t["idea_draft"] = up["idea_draft"]
    t["next_steps"] = (up.get("next_steps") or [])[:3]
    t["ready_for_review"] = bool(up.get("ready_for_review")) and bool(t.get("idea_draft"))
    event = None
    if up.get("park"):
        t["status"], t["status_reason"] = "parked", up["park"]
        event = {"kind": "parked", "text": up["park"]}
    save(t)
    return {"thread": t, "event": event}


def review(t: dict, day: str, log=print) -> dict:
    """草稿交独立审稿：通过 → 正式 IDEA；没通过 → 意见进下一步；第 max_reviews 次仍不过 → 搁置。"""
    from . import taste

    cand = dict(t["idea_draft"], thread=t["id"])
    w = ideate.ideas_dir() / ".work" / "threads" / t["id"] / day
    rv = ideate._review(cand, w / f"review-{len([r for r in t['reviews'] if not r.get('from_seed')]) + 1}", w / "library.json")
    t = load(t["id"])
    t["ready_for_review"] = False
    if rv.get("verdict") == "error":
        save(t)
        return {"event": None}
    if rv.get("verdict") == "pass":
        sc = taste.screen([{"key": t["id"], "kind": "研究想法", "title": cand.get("title"), "text": cand.get("summary") or ""}], log=log)
        if t["id"] in sc and sc[t["id"]]["like"] < taste.min_like():
            rv = dict(rv, verdict="taste", taste=sc[t["id"]])
    t["reviews"].append({k: rv.get(k) for k in ("mean", "scores", "summary", "weaknesses", "suggestions", "verdict",
                                                  "novelty_veto", "problem_veto", "taste")} | {"at": now_iso(), "day": day})
    if rv["verdict"] == "pass":
        idea = ideate.promote(cand, rv, day, log=log, thread=t["id"])
        t["status"], t["idea_id"] = "done", idea["id"]
        save(t)
        return {"event": {"kind": "passed", "text": f"通过审稿（均分 {rv.get('mean')}）→ IDEA {idea['id']}"}, "idea": idea}
    n_real = len([r for r in t["reviews"] if not r.get("from_seed")])
    if n_real >= cfg()["max_reviews"]:
        t["status"], t["status_reason"] = "parked", f"审稿 {n_real} 次仍未通过（最近均分 {rv.get('mean')}）：{(rv.get('summary') or '')[:200]}"
        save(t)
        return {"event": {"kind": "parked", "text": t["status_reason"]}}
    t["next_steps"] = ([f"回应审稿意见：{x}" for x in (rv.get("weaknesses") or [])[:2]] + t["next_steps"])[:3]
    save(t)
    why = "新颖性否决" if rv.get("novelty_veto") else ("问题真实性否决" if rv.get("problem_veto") else
                                                     ("不合口味" if rv["verdict"] == "taste" else f"均分 {rv.get('mean')}"))
    return {"event": {"kind": "reviewed", "text": f"第 {n_real} 次审稿没通过（{why}），按意见继续"}}


def advance(day: str, log=print) -> dict:
    """每日：补满活跃研究线，每条推进一步，成熟的交审稿。返回 {events, passed}。"""
    events, passed = [], []
    for t in fill_slots(log=log):
        events.append({"thread": t["id"], "title": t["title"], "kind": "started", "text": f"开始（{SOURCES[t['source']]}）：{t['question'][:120]}"})
    for t in [t for t in all_threads() if t["status"] == "active"]:
        try:
            r = step(t, day, log=log)
        except llm.LLMPaused:
            raise
        except Exception as e:  # noqa: BLE001  一条线出错不影响其它线
            log(f"研究线 {t['id']} 推进失败：{e}")
            continue
        t = r["thread"]
        log(f"研究线 {t['title']}：{(t['steps'][-1].get('did') or '')[:80]}")
        if r["event"]:
            events.append(dict(r["event"], thread=t["id"], title=t["title"]))
            continue
        if t.get("ready_for_review"):
            rr = review(t, day, log=log)
            if rr.get("event"):
                events.append(dict(rr["event"], thread=t["id"], title=t["title"]))
            if rr.get("idea"):
                passed.append(rr["idea"])
    return {"events": events, "passed": passed}


def scout_due(day: str) -> bool:
    """广撒网构思每周一次（默认周一）。"""
    return _dt.date.fromisoformat(day).weekday() == cfg()["scout_weekday"]
