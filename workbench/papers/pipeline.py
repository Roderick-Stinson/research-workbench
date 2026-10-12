"""每日流程（ADR-0026 / ADR-0027）：一日一期，当天可更新同一期。

期号文件 reports/daily/<day>.json：
  steps     每一步的状态 / 起止时间 / 统计 / 错误（失败的步骤会出现在推送的告警里）
  picks     精选 [{pid, rank, score…}]
  new       本期新收录（属于方向）的论文
  repos     新仓库
  progress  已收录论文的新进展（被录用、代码放出、讨论升温）
  pushed    推送记录

wb daily run    抓取 → 判定 → 速读 → 关注度 → 精选 → 新仓库 → 精读 → 领域地图 → 构思 → 热点 / 周报 / 月报
wb daily push   发本期第一版到 Telegram（只发一次；之后的更新不再刷屏，除非 --force）
wb backfill     首次回填：引用 baseline 的论文 + arXiv 近 12 个月（只做判定 + 速读，不进精选）
"""
from __future__ import annotations

import datetime as _dt
import time
import traceback

from .. import config
from ..util import now_iso, read_json, write_json
from . import field as fieldmod
from . import intake, llm, notify, select, sources, store

def issue_path(day: str):
    return config.home() / "reports" / "daily" / f"{day}.json"


def load_issue(day: str) -> dict:
    return read_json(issue_path(day)) or {"day": day, "created_at": now_iso(), "steps": {}, "picks": [], "new": [],
                                          "repos": [], "progress": [], "pushed": []}


def save_issue(iss: dict) -> None:
    iss["updated_at"] = now_iso()
    write_json(issue_path(iss["day"]), iss)


class Log:
    def __init__(self, day):
        self.p = config.home() / "reports" / "daily" / f"{day}.log"
        self.p.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg):
        line = f"{now_iso()} {msg}"
        print(line, flush=True)
        with open(self.p, "a") as f:
            f.write(line + "\n")


def _step(iss: dict, name: str, fn, log):
    st = {"status": "running", "started": now_iso()}
    iss["steps"][name] = st
    save_issue(iss)
    t0 = time.time()
    try:
        stats = fn() or {}
        st.update(status="ok", stats=stats)
    except llm.LLMNotConfigured as e:
        st.update(status="blocked", error=str(e))
        log(f"[{name}] 被阻塞：{e}")
    except llm.LLMPaused as e:
        st.update(status="paused", error=str(e), until=e.until)
        log(f"[{name}] 暂停：{e}（恢复后由 wb daily resume 自动补跑）")
    except Exception as e:  # noqa: BLE001  一步失败不影响其它步骤
        st.update(status="failed", error=f"{type(e).__name__}: {e}"[:600], trace=traceback.format_exc()[-2000:])
        log(f"[{name}] 失败：{e}")
    st["ended"] = now_iso()
    st["seconds"] = round(time.time() - t0, 1)
    save_issue(iss)
    return st


# ---------------- 各步骤 ----------------


def harvest(day: str, iss: dict, log, lookback_days: int = 4) -> dict:
    f = fieldmod.field()
    index = store.Index()
    d = _dt.date.fromisoformat(day)
    start = _dt.datetime.combine(d - _dt.timedelta(days=lookback_days), _dt.time(0, 0))
    end = _dt.datetime.combine(d, _dt.time(23, 59))
    stats = {"arxiv": 0, "hf": 0, "citations": 0, "new": 0}
    new_ids = set(iss.get("candidates") or [])

    old_before = (d - _dt.timedelta(days=lookback_days + 30)).isoformat()

    def put(rec, src):
        pid, new = store.upsert(index, rec, src, day=day)
        if new:
            stats["new"] += 1
            new_ids.add(pid)
            pub = (rec.get("published") or "")[:10]
            if pub and pub < old_before:  # 通过引用等途径才发现的旧论文：进库，但不算本期新论文
                m = store.load(pid)
                m["backfill"] = True
                store.save(m)
        return pid

    for r in sources.arxiv_search(f["arxiv_keywords"], f["arxiv_categories"], start, end):
        put(r, {"name": "arxiv", "ref": r["arxiv"], "terms": r.get("matched_terms")})
        stats["arxiv"] += 1
    for k in range(1, lookback_days + 1):
        hd = (d - _dt.timedelta(days=k)).isoformat()
        for r in sources.hf_daily(hd):
            pid = put(r, {"name": "hf", "ref": hd})
            m = store.load(pid)
            att = m.setdefault("attention", {})
            att["hf_upvotes"] = max(att.get("hf_upvotes") or 0, r.get("hf_upvotes") or 0)
            store.save(m)
            stats["hf"] += 1
    # 种子论文本身也要在库里（它们的数字是结果表里最重要的基线）
    missing = [s["arxiv"] for s in fieldmod.seeds() if not index.find(arxiv=s["arxiv"])]
    for r in sources.arxiv_by_ids(missing) if missing else []:
        pid, _ = store.upsert(index, r, {"name": "seed", "ref": r["arxiv"]}, day=day)
        m = store.load(pid)
        m["backfill"] = True  # 不进当期精选
        m["intake"] = {"state": "relevant", "relevance": 1.0, "reason": "种子论文（本人的 baseline）", "judged_at": now_iso()}
        store.save(m)
        new_ids.add(pid)
        stats["seeds"] = stats.get("seeds", 0) + 1
    for s in fieldmod.seeds():
        try:
            rows = sources.seed_citations(s["arxiv"])
        except Exception as e:  # noqa: BLE001  S2 限速时跳过，明天再试
            log(f"引用 {s['arxiv']} 抓取失败：{e}")
            continue
        for r in rows:
            put(r, {"name": "cites-seed", "ref": s["arxiv"]})
            stats["citations"] += 1
    iss["candidates"] = sorted(new_ids)
    log(f"抓取：arXiv {stats['arxiv']}、HF {stats['hf']}、引用种子 {stats['citations']}，新增 {stats['new']} 篇")
    return stats


def judge(day: str, iss: dict, log) -> dict:
    pids = [p for p in iss.get("candidates") or [] if (store.load(p) or {}).get("intake", {}).get("state") == "candidate"]
    n = intake.run_relevance(pids, log=log)
    iss["new"] = sorted({p for p in iss.get("candidates") or []
                         if (store.load(p) or {}).get("intake", {}).get("state") == "relevant"
                         and not (store.load(p) or {}).get("backfill")})
    return {"judged": len(pids), "relevant": n}


def read(day: str, iss: dict, log) -> dict:
    cands = iss.get("candidates") or []
    fresh = [p for p in cands if not (store.load(p) or {}).get("backfill")]
    old = [p for p in cands if (store.load(p) or {}).get("backfill")]
    res = intake.run_quickreads(fresh, log=log)          # 本期新论文：每日额度
    try:                                                   # 顺带发现的旧论文：走回填预算，不挤占每日流程
        res2 = intake.run_quickreads(old, log=log, kind="backfill")
        res["ok"] += res2["ok"]
        res["failed"] += res2["failed"]
    except llm.LLMPaused as e:
        log(f"旧论文速读暂停（回填预算）：{e}，由回填续做")
    return {"ok": res["ok"], "failed": len(res["failed"]), "failures": res["failed"][:10]}


def attention(day: str, iss: dict, log) -> dict:
    """近 7 天收录的、属于方向的论文：HF 点赞 + HN 讨论 + 录用会议。"""
    d = _dt.date.fromisoformat(day)
    recent = [m for m in store.all_papers() if m.get("intake", {}).get("state") == "relevant" and not m.get("backfill")
              and (m.get("discovered_day") or "") >= (d - _dt.timedelta(days=7)).isoformat()]
    venues = sources.s2_batch([m["arxiv"] for m in recent if m.get("arxiv") and not m.get("venue")])
    n = 0
    for m in recent:
        m = store.load(m["id"])
        att = m.setdefault("attention", {})
        if m.get("arxiv"):
            hp = sources.hf_paper(m["arxiv"])
            att["hf_upvotes"] = max(att.get("hf_upvotes") or 0, hp.get("upvotes") or 0)
            if hp.get("github") and hp["github"] not in m.setdefault("code_urls", []):
                m["code_urls"].append(hp["github"])
                iss.setdefault("progress", []).append({"pid": m["id"], "at": now_iso(), "kind": "code",
                                                       "text": f"代码：{hp['github']}"})
        att.update(sources.hn_attention(m.get("arxiv"), m.get("title", "")))
        att["sources_n"] = len({s.get("name") for s in m.get("sources", [])}) + bool(att.get("hn_posts")) + \
            bool(att.get("hf_upvotes"))
        att["updated"] = now_iso()
        v = (venues.get(m.get("arxiv")) or {}).get("venue")
        if v and not m.get("venue"):
            m["venue"] = v
            iss.setdefault("progress", []).append({"pid": m["id"], "at": now_iso(), "kind": "venue",
                                                   "text": f"录用：{v}"})
        store.save(m)
        n += 1
    log(f"关注度 / 录用信息更新 {n} 篇")
    return {"updated": n}


def choose(day: str, iss: dict, log) -> dict:
    from . import taste

    already = [p["pid"] for p in iss.get("picks") or []]
    room = fieldmod.cfg_selection().get("max_per_day", 10) - len(already)
    new = taste.rerank(select.pick(day, already, pool=max(30, 3 * room)), room, log=log) if room > 0 else []
    for i, c in enumerate(new, start=len(already) + 1):
        c["rank"] = i
        c["picked_at"] = now_iso()
        iss["picks"].append(c)
        m = store.load(c["pid"])
        m["picked"] = {"day": day, "rank": i, "score": c["total"]}
        store.save(m)
    log(f"精选 {len(new)} 篇（本期共 {len(iss['picks'])} 篇）")
    return {"picked": len(new), "total": len(iss["picks"])}


def repos(day: str, iss: dict, log) -> dict:
    since = (_dt.date.fromisoformat(day) - _dt.timedelta(days=3)).isoformat()
    cands = sources.github_new_repos(["multi-agent", "agentic workflow", "agent orchestration", "multi-agent framework"],
                                     since)
    if not cands:
        iss["repos"] = []
        return {"repos": 0}
    metas = [{"id": c["url"], "title": c["name"], "abstract": c["description"]} for c in cands[:40]]
    res = intake.judge_relevance(metas)
    keep = [c for c in cands[:40] if (res.get(c["url"]) or {}).get("relevant")]
    iss["repos"] = keep[:10]
    log(f"新仓库 {len(cands)} 个，属于方向 {len(keep)} 个")
    return {"repos": len(keep)}


def explain_picks(day: str, iss: dict, log) -> dict:
    from . import explain

    return explain.run([p["pid"] for p in iss.get("picks") or []], log=log)


def results_table(day: str, iss: dict, log) -> dict:
    """结果表（ADR-0034、0035）：归类新数据集名、补当期论文的评测规模与方法名核对、核实代码是否开源、重建索引。
    老论文的评测规模与方法名核对由 resume 按回填预算补。"""
    from . import codecheck, datasets, namecheck, results

    out = {"datasets": datasets.classify(log=log)}
    todo = datasets.pending(iss.get("candidates") or [])
    if todo:
        out["eval_sets"] = datasets.fill(todo, log=log)["ok"]
    new = set(iss.get("candidates") or [])
    todo = [p for p in namecheck.pending() if p in new]
    if todo:
        out["names"] = namecheck.fill(todo, log=log)["ok"]
    out["code"] = codecheck.run(limit=300, log=log)  # 新论文 + 到期复查（没放代码的每周查一次）
    out.update(results.rebuild_index(log=log))
    return out


def field_map(day: str, iss: dict, log) -> dict:
    from . import fieldmap

    n_rel = sum(1 for m in store.all_papers() if m.get("intake", {}).get("state") == "relevant")
    if n_rel < 20:
        return {"skipped": f"论文库只有 {n_rel} 篇，至少 20 篇再生成地图"}
    if fieldmap.due_for_rebuild(day):
        return fieldmap.rebuild(log=log)
    new = [p for p in iss.get("candidates") or [] if (store.load(p) or {}).get("intake", {}).get("state") == "relevant"]
    return fieldmap.assign_new(new, log=log)


def ideation(day: str, iss: dict, log) -> dict:
    from . import ideate

    res = ideate.run(day, log=log)
    iss["ideas"] = (iss.get("ideas") or []) + (res.get("passed") or [])
    return dict({k: v for k, v in res.items() if k != "passed"}, passed=len(res.get("passed") or []))


def periodic_reports(day: str, iss: dict, log) -> dict:
    from . import reports

    out = {"hot": len(reports.hot(day)["items"])}
    d = _dt.date.fromisoformat(day)
    if d.weekday() == 0:
        out["weekly"] = reports.build("weekly")["key"]
    if d.day == 1:
        out["monthly"] = reports.build("monthly")["key"]
    log(f"报告：{out}")
    return out


def profile_suggest(day: str, iss: dict, log) -> dict:
    """新评价攒够 3 条且没有待确认的建议时，起草画像修改（ADR-0036）；本人确认后才写入。"""
    from . import manual, taste

    n = taste.new_since_profile()
    if manual.proposal_path().exists():
        return {"pending_proposal": True, "new_ratings": n}
    if n < 3:
        return {"new_ratings": n}
    prop = manual.suggest(log=log)
    iss["profile_proposal"] = {"at": prop["created_at"], "ratings": n, "changes": prop.get("changes")}
    log(f"根据 {n} 条新评价起草了画像修改，等本人确认")
    return {"proposed": True, "new_ratings": n}


STEPS = [("harvest", harvest), ("judge", judge), ("read", read), ("results", results_table), ("attention", attention), ("choose", choose),
         ("repos", repos), ("explain", explain_picks), ("map", field_map), ("ideate", ideation),
         ("reports", periodic_reports), ("profile", profile_suggest)]


def run(day: str | None = None, only: list | None = None) -> dict:
    day = day or store.today()
    log = Log(day)
    iss = load_issue(day)
    log(f"=== 每日流程 {day} 开始 ===")
    for name, fn in STEPS:
        if only and name not in only:
            continue
        _step(iss, name, lambda fn=fn: fn(day, iss, log), log)
    log(f"=== 每日流程 {day} 结束 ===")
    return iss


# ---------------- 推送 ----------------


STEP_NAMES = {"harvest": "抓取", "judge": "相关性判定", "read": "速读", "results": "结果表", "attention": "关注度", "choose": "精选",
              "repos": "新仓库", "explain": "精读", "map": "领域地图", "ideate": "构思", "reports": "报告", "profile": "画像"}


def render_telegram(iss: dict, update: bool = False) -> str:
    url = (config.load().get("panel_url") or "").rstrip("/")
    e = notify.esc
    lines = [f"<b>{'🔄 更新 · ' if update else ''}📚 {e(iss['day'])} · 多智能体架构优化</b>"]
    picks = iss.get("picks") or []
    if picks:
        rp = (iss.get("replaced_picks") or [])[-1:] if update else []
        lines.append(f"今日精选 {len(picks)} 篇（按质量{'与你的口味' if any(p.get('like') is not None for p in picks) else ''}，最多 10 篇）"
                     + (f"\n已按你的要求重新精选{('：' + e(rp[0]['reason'])) if rp[0].get('reason') else ''}" if rp else ""))
        for p in picks:
            m = store.load(p["pid"]) or {}
            qr = store.load_quickread(p["pid"]) or {}
            title = qr.get("title_zh") or m.get("title")
            link = f"{url}/paper/{p['pid']}" if url else m.get("url")
            meta = " · ".join(x for x in [m.get("venue"), f"Q{p['quality']:.1f}" if p.get("quality") else None,
                                          "有代码" if m.get("code_urls") else None] if x)
            why = f"\n👍 按你的评价：{e(p['like_reason'])}" if p.get("like_reason") else ""
            lines.append(f"{p['rank']}. <a href=\"{e(link)}\">{e(title)}</a>\n"
                         f"{e(qr.get('one_liner') or '')}\n<i>{e(meta)}</i>{why}")
    else:
        lines.append("今天没有达到质量门槛的新论文。")
    news = [p for p in iss.get("new") or [] if p not in {x["pid"] for x in picks}]
    if news:
        lines.append(f"另有 {len(news)} 篇新收录（已速读）：{url}/issue/{iss['day']}")
    if iss.get("ideas"):
        lines.append(f"<b>💡 通过评审的 IDEA {len(iss['ideas'])} 个</b>")
        for it in iss["ideas"]:
            lines.append(f"• <a href=\"{e(url)}/idea/{e(it['id'])}\">{e(it.get('title'))}</a>（均分 {it.get('score')}）")
    if iss.get("repos"):
        lines.append("新仓库：" + "，".join(f"<a href=\"{e(r['url'])}\">{e(r['name'])}</a>⭐{r['stars']}"
                                         for r in iss["repos"][:5]))
    steps = iss.get("steps") or {}
    paused = {k: v for k, v in steps.items() if v.get("status") == "paused"}
    bad = {k: v for k, v in steps.items() if v.get("status") in ("failed", "blocked")}
    if paused:
        lines.append("<b>⏸ 暂停中（额度）</b>，恢复后自动补跑并更新本期：\n" + "\n".join(
            f"• {e(STEP_NAMES.get(k, k))}：{e(v.get('error', ''))[:160]}" for k, v in paused.items()))
    if bad:
        lines.append("<b>⚠️ 有步骤没成功</b>\n" + "\n".join(
            f"• {e(STEP_NAMES.get(k, k))}：{e(v.get('error', ''))[:200]}" for k, v in bad.items()))
    if iss.get("profile_proposal"):
        pp = iss["profile_proposal"]
        lines.append(f"<b>🧭 根据你的 {pp.get('ratings')} 条新评价起草了画像修改</b>，确认后才生效（对 agent 说「看看画像修改建议」）：\n"
                     + "\n".join(f"• {e(c)}" for c in (pp.get("changes") or [])[:5]))
    lines.append(f"<a href=\"{e(url)}/issue/{e(iss['day'])}\">在面板查看本期</a>")
    return "\n\n".join(lines)


def repick(day: str | None = None, reason: str = "", log=print) -> dict:
    """按最新的口味 / 评价重新精选本期（本人要求时）：原精选留在 replaced_picks，重新选、补精读、推送更新。"""
    day = day or store.today()
    iss = load_issue(day)
    old = iss.get("picks") or []
    for p in old:
        m = store.load(p["pid"])
        if m and (m.get("picked") or {}).get("day") == day:
            m.pop("picked", None)
            store.save(m)
    iss.setdefault("replaced_picks", []).append({"at": now_iso(), "reason": reason, "picks": old})
    iss["picks"] = []
    save_issue(iss)
    _step(iss, "choose", lambda: choose(day, iss, log), log)
    _step(iss, "explain", lambda: explain_picks(day, iss, log), log)
    keep = {p["pid"] for p in old} & {p["pid"] for p in iss["picks"]}
    log(f"重新精选：{len(iss['picks'])} 篇，其中 {len(keep)} 篇与原精选相同")
    return {"picks": [p["pid"] for p in iss["picks"]], "kept": sorted(keep),
            "dropped": [p["pid"] for p in old if p["pid"] not in keep]}


def push(day: str | None = None, force: bool = False) -> dict:
    day = day or store.today()
    iss = load_issue(day)
    if iss.get("pushed") and not force:
        return {"skipped": "本期已推送过（加 --force 重推）"}
    if not iss.get("steps"):
        text = f"<b>⚠️ {day} 的每日流程没有运行</b>\n检查 cron 与 {config.home()}/reports/daily/{day}.log"
    else:
        text = render_telegram(iss, update=bool(iss.get("pushed")))
    ids = notify.send(text)
    iss.setdefault("pushed", []).append({"at": now_iso(), "message_ids": ids, "picks": len(iss.get("picks") or [])})
    save_issue(iss)
    return {"sent": len(ids)}


def resume(log=print) -> dict:
    """每 30 分钟由 cron 调用：额度恢复后补跑暂停 / 失败的步骤，有新内容就推送更新；然后按预算续跑回填。"""
    out = {}
    try:
        llm.check_budget("daily")  # 熔断中 / 真实额度用完：什么都不做
    except llm.LLMPaused as e:
        return {"waiting": str(e)}
    today = _dt.date.fromisoformat(store.today())
    for day in [(today - _dt.timedelta(days=1)).isoformat(), today.isoformat()]:
        if not issue_path(day).exists():
            continue
        iss = load_issue(day)
        names = [n for n, _ in STEPS]
        todo = [n for n in names if (iss.get("steps") or {}).get(n, {}).get("status") in ("paused", "failed")]
        if not todo:
            continue
        first = names.index(todo[0])
        redo = [n for n in names[first:] if n != "harvest"]
        before = {n: (iss["steps"].get(n) or {}).get("status") for n in redo}
        log(f"补跑 {day}：{redo}")
        iss = run(day, redo)
        after = {n: (iss["steps"].get(n) or {}).get("status") for n in redo}
        fixed = [n for n in redo if before.get(n) != "ok" and after.get(n) == "ok"]
        out[day] = {"redo": redo, "fixed": fixed}
        if fixed and iss.get("pushed") and ("explain" in fixed or "ideate" in fixed or "choose" in fixed):
            push(day, force=True)
            out[day]["pushed_update"] = True
        if any(v == "paused" for v in after.values()):
            return out  # 又暂停了：回填也别跑
    if not llm.block_state():
        try:
            out["backfill"] = backfill(log=log)
        except llm.LLMPaused as e:
            out["backfill"] = f"暂停：{e}"
    if not llm.block_state():
        out["eval_sets"] = backfill_eval_sets(log=log)
    return out


def backfill_eval_sets(limit: int = 80, log=print) -> dict:
    """老论文补评测规模（ADR-0034）与方法名核对（ADR-0035），受回填预算约束；每次各最多 limit 篇，补完重建结果表索引。"""
    from . import datasets, results

    from . import namecheck

    out = {}
    for name, mod in (("eval_sets", datasets), ("names", namecheck)):
        todo = mod.pending()[:limit]
        if not todo:
            continue
        try:
            res = mod.fill(todo, kind="backfill", log=log)
            out[name] = {"ok": res["ok"], "failed": len(res["failed"]), "pending": len(mod.pending())}
        except llm.LLMPaused as e:
            out[name] = {"paused": str(e)}
            break
    if out:
        results.rebuild_index(log=log)
    return out or {"pending": 0}


def alert(text: str) -> None:
    """流程级的失败告警（cron 包装里调用）。"""
    try:
        notify.send(f"<b>⚠️ 工作台告警</b>\n{notify.esc(text)}")
    except Exception:
        pass


# ---------------- 回填 ----------------


def backfill(months: int = 12, log=print, judge_only: bool = False) -> dict:
    """首次回填（ADR-0028 第 3 条）：引用 baseline 的全部论文 + arXiv 近 N 个月关键词检索。标记 backfill，不进精选。"""
    f = fieldmod.field()
    index = store.Index()
    day = store.today()
    end = _dt.datetime.utcnow()
    start = end - _dt.timedelta(days=int(months * 30.5))
    got = []
    if not judge_only:
        # arXiv 按月切片，单词条单月不至于超过上限；已完成的月份记在 state 里，中断后可续跑
        stp = config.home() / "reports" / "backfill_state.json"
        state = read_json(stp) or {"done_months": []}
        cur = start
        while cur < end:
            nxt = min(cur + _dt.timedelta(days=31), end)
            key = f"{cur:%Y-%m-%d}"
            if key in state["done_months"]:
                cur = nxt
                continue
            try:
                rows = sources.arxiv_search(f["arxiv_keywords"], f["arxiv_categories"], cur, nxt, max_per_term=4000)
            except Exception as e:  # noqa: BLE001  限流等：这个月先跳过，下次续跑
                log(f"arXiv {cur:%Y-%m} 抓取失败（下次续跑）：{e}")
                cur = nxt
                continue
            for r in rows:
                pid, new = store.upsert(index, r, {"name": "arxiv-backfill", "ref": f"{cur:%Y-%m}"}, day=day)
                if new:
                    m = store.load(pid)
                    m["backfill"] = True
                    store.save(m)
                    got.append(pid)
            state["done_months"].append(key)
            write_json(stp, state)
            log(f"arXiv {cur:%Y-%m}：{len(rows)} 篇")
            cur = nxt
        for s in fieldmod.seeds():
            rows = sources.seed_citations(s["arxiv"])
            for r in rows:
                pid, new = store.upsert(index, r, {"name": "cites-seed", "ref": s["arxiv"]}, day=day)
                if new:
                    m = store.load(pid)
                    m["backfill"] = True
                    store.save(m)
                    got.append(pid)
            log(f"引用 {s['arxiv']}：{len(rows)} 篇")
        # 种子本身也进库
        for r in sources.arxiv_by_ids([s["arxiv"] for s in fieldmod.seeds()]):
            pid, _ = store.upsert(index, r, {"name": "seed", "ref": r["arxiv"]}, day=day)
            m = store.load(pid)
            m["seed"] = True
            m["backfill"] = True
            m["intake"] = {"state": "relevant", "relevance": 1.0, "reason": "种子论文", "judged_at": now_iso()}
            store.save(m)
    cand = [m["id"] for m in store.all_papers() if m.get("intake", {}).get("state") == "candidate"]
    log(f"待判定 {len(cand)} 篇")
    n_rel = intake.run_relevance(cand, log=log, kind="backfill")
    rel = [m["id"] for m in store.all_papers() if m.get("intake", {}).get("state") == "relevant"]
    res = intake.run_quickreads(rel, log=log, kind="backfill")
    return {"harvested": len(got), "relevant": n_rel, "quickread": res["ok"], "failed": len(res["failed"])}
