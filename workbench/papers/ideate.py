"""构思（ADR-0018 / 0024 / 0025，P4）：每天根据手里的全部内容想多个 IDEA → 独立审稿 → 通过的写文档并告诉本人。

ideas/<id>/        告诉过本人的 IDEA：idea.json、candidate.json、review.json、idea.html；只有本人说删才删
ideas/.hidden/<id> 没通过评审的：candidate.json、review.json（用于去重，本人可以让 agent 翻出来）
ideas/.deleted/<id> 本人删除的（可恢复）
problems/<id>/     问题：problem.json、problem.html、figs/
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as _dt
import hashlib
import json
import shutil

from .. import config, stats, store as wbstore
from ..util import now_iso, read_json, write_json
from . import codex, fieldmap, store
from . import field as fieldmod


def ideas_dir():
    return config.home() / "ideas"


def problems_dir():
    return config.home() / "problems"


def _id(prefix: str, title: str) -> str:
    return f"{prefix}{_dt.date.today():%Y%m%d}-" + hashlib.sha1(title.encode()).hexdigest()[:6]


def all_ideas(include_hidden: bool = False) -> list:
    out = []
    for sub in [ideas_dir()] + ([ideas_dir() / ".hidden"] if include_hidden else []):
        if not sub.exists():
            continue
        for d in sorted(sub.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and (d / "idea.json").exists():
                it = read_json(d / "idea.json")
                if it:
                    it["_dir"] = str(d)
                    out.append(it)
    return sorted(out, key=lambda x: x.get("created_at") or "", reverse=True)


def all_problems() -> list:
    d = problems_dir()
    if not d.exists():
        return []
    out = [read_json(p / "problem.json") for p in sorted(d.iterdir()) if (p / "problem.json").exists()]
    return [p for p in out if p]


# ---------------- 材料 ----------------


def _experiments_digest() -> dict:
    """论文项目 / claim / 配置组 / 最近的 Run（含输出文件位置，供 bad case 分析）。"""
    projects = wbstore.all_projects()
    runs = wbstore.all_runs()
    recent = sorted(runs, key=lambda r: -r.get("start_ts", 0))[:60]
    return {
        "projects": [{"id": p["id"], "title": p.get("title"),
                      "claims": [{"id": e["id"], "claim": e.get("claim"), "variants": e.get("variants")} for e in p.get("experiments", [])]}
                     for p in projects],
        "variants": [{"id": v["id"], "title": v.get("title"), "baseline": v.get("baseline"),
                      "aggregate": {k: {"mean": a["mean"], "std": a["std"], "n": a["n"]}
                                    for k, a in stats.aggregate(stats.variant_runs(v["id"], runs)).items()}}
                     for v in wbstore.all_variants()],
        "recent_runs": [{"id": r["id"], "cmd": r.get("cmdline"), "cwd": r.get("cwd"), "state": r.get("state"),
                         "exit": (r.get("exit") or {}).get("guess"), "variant": (r.get("claim") or {}).get("variant"),
                         "purpose": (r.get("claim") or {}).get("purpose"),
                         "outputs": [o["path"] for o in (r.get("outputs") or []) if o["path"].endswith((".jsonl", ".json"))][:20],
                         "run_dir": str(wbstore.run_dir(r["id"]))} for r in recent],
    }


def past_reviews(limit: int = 40) -> list:
    """以前没通过的 IDEA 与审稿意见（最近的在前）：撞车的已有工作、证据错在哪、主要弱点。构思时据此避坑。"""
    out = []
    for i in sorted(all_ideas(include_hidden=True), key=lambda x: x.get("created_at") or "", reverse=True):
        if i.get("informed"):
            continue
        d = ideas_dir() / ".hidden" / i["id"]
        rv = read_json(d / "review.json") or {}
        if not rv or rv.get("verdict") == "error":
            continue
        closest = [c.get("title") for q in rv.get("novelty_search") or [] for c in (q.get("closest") or [])][:6]
        bad_ev = [e.get("note") or e.get("evidence") for e in rv.get("evidence_check") or [] if e.get("verified") is False][:3]
        out.append({"title": i.get("title"), "mean": rv.get("mean"), "scores": rv.get("scores"),
                    "novelty_veto": rv.get("novelty_veto"), "collided_with": closest, "problem_veto": rv.get("problem_veto"),
                    "evidence_errors": bad_ev, "weaknesses": (rv.get("weaknesses") or [])[:5], "summary": rv.get("summary")})
        if len(out) >= limit:
            break
    return out


def _materials(w):
    lib = fieldmap._library_digest()
    (w / "library.json").write_text(json.dumps(lib, ensure_ascii=False))
    (w / "map.json").write_text(json.dumps(fieldmap.load() or {}, ensure_ascii=False))
    (w / "problems.json").write_text(json.dumps(all_problems(), ensure_ascii=False))
    prev = [{"id": i["id"], "title": i.get("title"), "problem": i.get("problem_id"), "status": i.get("status"),
             "informed": i.get("informed"), "summary": i.get("summary")} for i in all_ideas(include_hidden=True)]
    (w / "previous_ideas.json").write_text(json.dumps(prev, ensure_ascii=False))
    (w / "experiments.json").write_text(json.dumps(_experiments_digest(), ensure_ascii=False, default=str))
    (w / "past_reviews.json").write_text(json.dumps(past_reviews(), ensure_ascii=False))
    (w / "field.json").write_text(json.dumps(fieldmod.field(), ensure_ascii=False))
    return len(lib)


PROPOSE_PROMPT = """你是「{name}」方向的研究者，为本人每天构思可以发 CCF-A 的研究 IDEA（ADR-0018 / 0024）。

当前目录的材料：
- library.json：论文库（每篇的一句话、方法、局限、维度；my_rating 是本人评价 1–5 分和理由，评价 ≤ 2 的论文不要当作方法依据）；全文在 {home}/papers/<id>/fulltext.txt，速读与抽取的数字在 quickread.json
- map.json：领域地图（每类"没考虑到什么"是问题的天然入口）
- problems.json：已有的问题；previous_ideas.json：以前想过的 IDEA（包括没通过评审的）——**不要重复**
- experiments.json：本人的论文项目、claim、配置组均值方差、最近的 Run（outputs 里是逐样本结果，可做 bad case 分析）
- past_reviews.json：以前没通过的 IDEA 和独立审稿意见（撞车的已有工作、证据错在哪、主要弱点）——**先读它，别再犯同样的错**
- field.json：方向定义

任务：想 {n} 个**彼此不同**的 IDEA。每个 IDEA 必须针对一个具体的问题：
- 问题来源只能是：(a) 聚合 ≥2 篇库内论文发现的共性问题；(b) 分析 Run 的 bad case 定位出的问题。
- **证据必须真实可核对**：论文证据写 id + 英文原句 + 表 / 节号 + 数字（去 fulltext.txt 里找原句）；
  bad case 写 Run ID + 文件路径 + sample_id + 原文片段。不可以猜、不可以编。
- 已有问题可以复用（写 existing_problem_id）。

审稿标准是 NeurIPS / ICLR（新颖性、问题真实性一票否决，四项均分 ≥ 7 才算通过）。以往被拒的主要原因和对应要求：
1. **撞车**：提交前在 library.json 和全文里找最接近的 ≥ 3 篇工作（尤其 past_reviews.json 里点名撞过的），写清楚核心机制上的本质区别；
   只是把已有部件组合起来、或换个领域包装的，不要提交。
2. **深度不够**：方法要定义清楚数学对象、优化目标 / 损失、训练或搜索过程，以及为什么能解决问题（最好有一个可证明或可检验的性质），不要只写模块名。
3. **证据出错**：每条论文证据的原句、表 / 节 / 公式编号和数字都要在 fulltext.txt 里逐字核对过；对不上的证据宁可不用。
4. **不可行**：实验要在 6 张 GPU（4090 / L40S）+ 有限 API 预算、2–3 个月内做完；先写最小但有说服力的实验，不要铺太大。

输出写到 ./candidates.json：
{{"candidates": [{{
  "title": "IDEA 标题（中文）", "summary": "两三句：做什么、为什么能解决问题",
  "problem": {{"existing_problem_id": null, "title": "问题一句话", "source": "papers|badcase",
              "evidence": [{{"type": "paper|run", "ref": "论文 id 或 Run ID", "where": "Table 2 / Sec 4 / 文件路径#sample_id",
                             "quote": "原文 / 样本片段", "numbers": "相关数字"}}]}},
  "method": "方法概要（模块、关键机制）", "closest_work": ["库里最接近的论文 id 及区别"],
  "expected_contribution": "预期贡献", "minimal_experiment": "最小验证实验"
}}]}}
完成后只回复 DONE 或 FAILED: 原因。
"""

REVISE_PROMPT = """你是这个 IDEA 的作者。./candidate.json 是你的 IDEA，./review.json 是独立审稿人的意见（没有一票否决，但均分没到 7）。
按审稿意见修改 IDEA，写到 ./revised.json（结构和 candidate.json 相同），重点是：
- 逐条回应 weaknesses 和 suggestions：补上缺的形式化定义、优化目标、关键机制；说清楚和审稿人点名的最接近工作的本质区别；
- 实验收缩到预算内（6 张 GPU + 有限 API、2–3 个月），保留最能说明问题的部分；
- 问题与证据保持不变，除非你在 {home}/papers/<id>/fulltext.txt 里逐字核对过新的证据；不能编。
另外写 ./response.json：{{"changes": ["针对哪条意见改了什么"]}}。论文库在 ./library.json。完成后只回复 DONE 或 FAILED: 原因。"""

REVIEW_PROMPT = """按照 skill {skill}/SKILL.md 独立评审当前目录的 candidate.json（论文库在 ./library.json）。
你不是这个 IDEA 的作者。把结果写到 ./review.json。完成后只回复 DONE 或 FAILED: 原因。"""

PROBLEM_PROMPT = """按照 skill {skill}/SKILL.md，为 ./problem_input.json 描述的问题写 ./problem.html（真实数据作图，脚本与数据放 ./figs/）。
论文库：{home}/papers/<id>/（fulltext.txt、quickread.json）；Run：{home}/runs/<RUN>/。不可以猜。
完成后只回复 DONE 或 FAILED: 原因。"""

IDEA_PROMPT = """按照 skill {skill}/SKILL.md，为 ./candidate.json 的 IDEA 写 ./idea.html（只写 Step 1–6）。
对应的问题文档：{problem_html}；审稿意见：./review.json；领域地图：{home}/map/ 当前版本；论文库：{home}/papers/。
完成后只回复 DONE 或 FAILED: 原因。"""


def _review(cand: dict, w, lib_path) -> dict:
    w.mkdir(parents=True, exist_ok=True)
    write_json(w / "candidate.json", cand)
    shutil.copy2(lib_path, w / "library.json")
    res = codex.run(f"review:{w.name}", w, REVIEW_PROMPT.format(skill=codex.SKILLS / "idea-reviewer"), network=True)
    rv = read_json(w / "review.json")
    if not res["ok"] or not rv:
        return {"verdict": "error", "error": res.get("error") or "没有产出 review.json"}
    sc = rv.get("scores") or {}
    vals = [sc.get(k) for k in ("significance", "novelty", "depth", "feasibility") if isinstance(sc.get(k), (int, float))]
    rv["mean"] = round(sum(vals) / len(vals), 2) if vals else 0
    # 规则以工作台为准（不完全信审稿 agent 自己写的 verdict）
    rv["verdict"] = "pass" if (not rv.get("novelty_veto") and not rv.get("problem_veto") and rv["mean"] >= 7
                               and len(vals) == 4) else "reject"
    return rv


def run(day: str, log=print, n: int | None = None) -> dict:
    n = n or (fieldmod.field().get("ideation") or {}).get("candidates_per_day", 4)
    w = ideas_dir() / ".work" / day
    if w.exists():
        shutil.rmtree(w)
    w.mkdir(parents=True)
    nlib = _materials(w)
    if nlib < 10:
        return {"skipped": f"论文库只有 {nlib} 篇，材料太少"}
    f = fieldmod.field()
    res = codex.run(f"ideate:{day}", w, PROPOSE_PROMPT.format(name=f["name"], home=config.home(), n=n), network=False)
    cands = (read_json(w / "candidates.json") or {}).get("candidates") or []
    if not res["ok"] or not cands:
        raise RuntimeError(f"构思失败：{res.get('error') or '没有产出 candidates.json'}")
    log(f"构思出 {len(cands)} 个候选")
    reviews = {}
    with cf.ThreadPoolExecutor(max_workers=codex.cfg()["parallel"]) as ex:
        futs = {ex.submit(_review, c, w / f"review-{i}", w / "library.json"): i for i, c in enumerate(cands)}
        for fu in cf.as_completed(futs):
            try:
                reviews[futs[fu]] = fu.result()
            except Exception as e:  # noqa: BLE001
                reviews[futs[fu]] = {"verdict": "error", "error": str(e)[:300]}
    # 接近通过的（没有一票否决、均分 ≥ 6）：作者按审稿意见改一轮，换一个审稿人重新评（ADR-0042）
    near = [i for i, rv in reviews.items() if rv.get("verdict") == "reject" and not rv.get("novelty_veto")
            and not rv.get("problem_veto") and (rv.get("mean") or 0) >= 6]
    for i in near:
        wr = w / f"revise-{i}"
        wr.mkdir(parents=True, exist_ok=True)
        write_json(wr / "candidate.json", cands[i])
        write_json(wr / "review.json", reviews[i])
        shutil.copy2(w / "library.json", wr / "library.json")
        try:
            rr = codex.run(f"revise:{day}:{i}", wr, REVISE_PROMPT.format(home=config.home()), network=False)
        except Exception as e:  # noqa: BLE001
            log(f"修改 IDEA {i} 没做成：{e}")
            continue
        rev = read_json(wr / "revised.json")
        if not rr["ok"] or not rev:
            log(f"修改 IDEA {i} 没有产出：{rr.get('error')}")
            continue
        rev["revision"] = {"first_review": {k: reviews[i].get(k) for k in ("mean", "scores", "summary", "weaknesses")},
                           "changes": (read_json(wr / "response.json") or {}).get("changes")}
        try:
            rv2 = _review(rev, w / f"review-{i}-r2", w / "library.json")
        except Exception as e:  # noqa: BLE001
            log(f"修改后的 IDEA {i} 重评没做成：{e}")
            continue
        if rv2.get("verdict") != "error":
            cands[i] = rev
            reviews[i] = dict(rv2, revised=True)
            log(f"IDEA {i} 修改后重评：{reviews[i]['verdict']}（均分 {rv2.get('mean')}，原 {rev['revision']['first_review']['mean']}）")
    # 口味过滤（ADR-0039）：评审通过但不合本人口味的不告知，和没通过评审的放在一起、写明原因
    from . import taste

    ok = [{"key": i, "kind": "研究想法", "title": c.get("title"), "text": c.get("summary") or ""}
          for i, c in enumerate(cands) if (reviews.get(i) or {}).get("verdict") == "pass"]
    sc = taste.screen(ok, log=log)
    for i, s in sc.items():
        if s["like"] < taste.min_like():
            reviews[i] = dict(reviews[i], verdict="taste", taste=s)
    passed, hidden = [], []
    for i, c in enumerate(cands):
        rv = reviews.get(i) or {"verdict": "error"}
        iid = _id("I", c.get("title", str(i)))
        if rv.get("verdict") == "pass":
            passed.append(promote(c, rv, day, log))
        else:
            d = ideas_dir() / ".hidden" / iid
            d.mkdir(parents=True, exist_ok=True)
            write_json(d / "candidate.json", c)
            write_json(d / "review.json", rv)
            write_json(d / "idea.json", {"id": iid, "title": c.get("title"), "summary": c.get("summary"),
                                         "status": "rejected", "informed": False, "created_at": now_iso(), "day": day,
                                         "score": rv.get("mean"), "verdict": rv.get("verdict"), "taste": rv.get("taste"),
                                         "revised": bool(rv.get("revised")), "review_summary": rv.get("summary"),
                                         "veto": "新颖性" if rv.get("novelty_veto") else ("问题真实性" if rv.get("problem_veto") else None)})
            hidden.append(iid)
    log(f"评审通过 {len(passed)} 个，未通过 {len(hidden)} 个")
    return {"candidates": len(cands), "passed": passed, "hidden": len(hidden), "revised": len(near)}


def promote(c: dict, rv: dict, day: str, log=print, thread: str | None = None) -> dict:
    """通过评审的候选 → 正式 IDEA（写问题文档、IDEA 文档，告知本人）。"""
    iid = _id("I", c.get("title", "idea"))
    d = ideas_dir() / iid
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / "candidate.json", c)
    write_json(d / "review.json", rv)
    pid = _ensure_problem(c, d, log)
    idea = {"id": iid, "title": c.get("title"), "summary": c.get("summary"), "problem_id": pid,
            "status": "noted", "informed": True, "created_at": now_iso(), "day": day, "thread": thread,
            "score": rv.get("mean"), "history": [{"ts": now_iso(), "event": "created", "by": "thread" if thread else "ideation"}]}
    write_json(d / "idea.json", idea)
    _write_idea_doc(d, pid, log)
    return {"id": iid, "title": c.get("title"), "score": rv.get("mean")}


def _ensure_problem(c: dict, idea_dir, log) -> str:
    p = c.get("problem") or {}
    if p.get("existing_problem_id") and (problems_dir() / p["existing_problem_id"] / "problem.json").exists():
        return p["existing_problem_id"]
    pid = _id("P", p.get("title") or c.get("title", ""))
    d = problems_dir() / pid
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / "problem_input.json", p)
    prob = {"id": pid, "title": p.get("title"), "source": p.get("source"), "evidence": p.get("evidence"),
            "created_at": now_iso(), "ideas": []}
    res = codex.run(f"problem:{pid}", d, PROBLEM_PROMPT.format(skill=codex.SKILLS / "problem-doc", home=config.home()))
    prob["doc"] = (d / "problem.html").exists() and res["ok"]
    if not prob["doc"]:
        prob["doc_error"] = res.get("error") or res.get("last_message", "")[:300]
    write_json(d / "problem.json", prob)
    log(f"新问题 {pid}：{p.get('title')}（文档{'已生成' if prob['doc'] else '生成失败'}）")
    return pid


def _write_idea_doc(d, problem_id, log):
    ph = problems_dir() / problem_id / "problem.html"
    res = codex.run(f"idea-doc:{d.name}", d, IDEA_PROMPT.format(skill=codex.SKILLS / "idea-explainer", home=config.home(),
                                                                problem_html=ph))
    it = read_json(d / "idea.json")
    it["doc"] = (d / "idea.html").exists() and res["ok"]
    if not it["doc"]:
        it["doc_error"] = res.get("error")
    write_json(d / "idea.json", it)
    pj = read_json(problems_dir() / problem_id / "problem.json")
    if pj is not None and it["id"] not in pj.setdefault("ideas", []):
        pj["ideas"].append(it["id"])
        write_json(problems_dir() / problem_id / "problem.json", pj)


# ---------------- 本人通过 agent 做的操作 ----------------


def set_status(iid: str, status: str, note: str | None = None, link: str | None = None) -> dict:
    d = ideas_dir() / iid
    it = read_json(d / "idea.json")
    if not it:
        raise SystemExit(f"没有这个 IDEA：{iid}")
    it["status"] = status
    if note:
        it["note"] = note
    if link:
        it["claim"] = link
    it.setdefault("history", []).append({"ts": now_iso(), "event": status, "note": note, "link": link,
                                         "by": wbstore._actor()})
    write_json(d / "idea.json", it)
    wbstore.journal(f"idea.{status}", idea=iid, note=note, link=link)
    return it


def delete(iid: str, reason: str) -> None:
    """只有本人明确说删除时调用（ADR-0024）。移到 .deleted/，可恢复。"""
    d = ideas_dir() / iid
    if not d.exists():
        raise SystemExit(f"没有这个 IDEA：{iid}")
    dst = ideas_dir() / ".deleted" / iid
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d), str(dst))
    write_json(dst / "deleted.json", {"at": now_iso(), "by": wbstore._actor(), "reason": reason})
    wbstore.journal("idea.delete", idea=iid, reason=reason)
