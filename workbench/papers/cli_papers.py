"""wb 的论文侧子命令（ADR-0022：面板只读，反馈全部由 agent 调用这些命令）。"""
from __future__ import annotations

import json

from .. import store as wbstore
from ..util import now_iso
from . import field as fieldmod
from . import ids, pipeline, store


def out_json(x):
    print(json.dumps(x, ensure_ascii=False, indent=2, default=str))


def resolve_pid(s: str) -> str:
    s = s.strip()
    if store.load(s):
        return s
    a = ids.arxiv_id(s)
    if a and store.load(f"arxiv-{a}"):
        return f"arxiv-{a}"
    hits = [p for p in store.all_ids() if s.lower() in p.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        nt = ids.norm_title(s)
        hits = [m["id"] for m in store.all_papers() if nt and nt in ids.norm_title(m.get("title"))]
        if len(hits) == 1:
            return hits[0]
    raise SystemExit(f"找不到论文：{s}" if not hits else f"'{s}' 匹配到多篇：{', '.join(hits[:8])}")


# ---------------- daily / backfill ----------------


def cmd_daily(a):
    if a.action == "run":
        iss = pipeline.run(a.date, [x for x in (a.steps or "").split(",") if x] or None)
        bad = {k: v["status"] for k, v in iss["steps"].items() if v["status"] != "ok"}
        print(f"完成：精选 {len(iss['picks'])} 篇，新收录 {len(iss.get('new') or [])} 篇" + (f"；未成功：{bad}" if bad else ""))
    elif a.action == "push":
        print(pipeline.push(a.date, force=a.force))
    elif a.action == "resume":
        print(pipeline.resume())
    elif a.action == "repick":
        res = pipeline.repick(a.date, reason=a.reason or "")
        if a.force and "paused" not in res:  # 没真的重排就不推送
            res["push"] = pipeline.push(a.date, force=True)
        return out_json(res)
    elif a.action == "status":
        iss = pipeline.load_issue(a.date or store.today())
        if a.json:
            return out_json(iss)
        from . import llm
        print(f"{iss['day']}  精选 {len(iss.get('picks') or [])}  新收录 {len(iss.get('new') or [])}  "
              f"已推送 {len(iss.get('pushed') or [])} 次")
        from . import quota
        st = llm.block_state()
        model = llm.creds().get("WB_LLM_MODEL") or ""
        print(f"  批量模型 {model}：{quota.describe(quota.snapshot(), model)}"
              + (f"；⏸ {st['reason']}，预计 {st['until_text']} 恢复" if st else ""))
        for k, v in (iss.get("steps") or {}).items():
            print(f"  {k:10} {v.get('status'):8} {v.get('seconds', '')}s  {v.get('error', '') or v.get('stats', '')}")


def cmd_backfill(a):
    import fcntl

    from .. import config
    lock = open(config.home() / "daemon" / "daily.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("每日流程正在运行，等它结束再回填（共用 daemon/daily.lock）")
    from . import llm
    try:
        res = pipeline.backfill(a.months, judge_only=a.judge_only)
    except llm.LLMPaused as e:
        print(f"回填暂停：{e}（进度都已保存，下次 wb backfill 从这里继续）")
        return
    print(res)


# ---------------- paper ----------------


def _row(m):
    qr = m.get("_qr") or {}
    st = m.get("_status") or {}
    flags = "".join(c for c, k in (("★", "fav"), ("✓", "read"), ("✗", "irrelevant"), ("⟳", "reproduce")) if st.get(k))
    return [m["id"], (m.get("published") or "")[:10], (m.get("picked") or {}).get("day", ""), flags,
            (qr.get("title_zh") or m.get("title") or "")[:70]]


def cmd_paper(a):
    from ..cli import table

    if a.action == "ls":
        ps = [m for m in store.all_papers() if m.get("intake", {}).get("state") == "relevant"]
        if a.picked:
            ps = [m for m in ps if m.get("picked")]
        if a.day:
            ps = [m for m in ps if (m.get("picked") or {}).get("day") == a.day or m.get("discovered_day") == a.day]
        if a.q:
            q = a.q.lower()
            ps = [m for m in ps if q in (m.get("title") or "").lower() or q in (m.get("abstract") or "").lower()
                  or q in json.dumps(m.get("_qr") or {}, ensure_ascii=False).lower()]
        for fv in a.facet or []:
            k, v = fv.split("=", 1)
            ps = [m for m in ps if any(v in x for x in ((m.get("_qr") or {}).get("facets") or {}).get(k, []))]
        ps.sort(key=lambda m: m.get("published") or "", reverse=True)
        ps = ps[: a.limit]
        if a.json:
            return out_json([dict({k: v for k, v in m.items() if not k.startswith("_")}, quickread=m.get("_qr")) for m in ps])
        table([_row(m) for m in ps], ["论文", "发表", "精选日", "标记", "标题"])
    elif a.action == "show":
        pid = resolve_pid(a.ids[0])
        m = store.load(pid)
        qr = store.load_quickread(pid) or {}
        st = store.load_status(pid)
        if a.json:
            return out_json({"meta": m, "quickread": qr, "status": st})
        print(f"{pid}  {m.get('title')}")
        print(f"  {qr.get('title_zh', '')}")
        print(f"  发表 {m.get('published')}  会议 {m.get('venue') or '-'}  代码 {', '.join(m.get('code_urls') or []) or '-'}")
        print(f"  来源 {', '.join(sorted({s['name'] for s in m.get('sources', [])}))}  引用种子 {m.get('cites_seeds') or '-'}")
        print(f"  收录 {m.get('intake')}")
        if qr:
            print(f"  一句话：{qr.get('one_liner')}")
            for k, lab in (("problem", "问题"), ("method", "方法"), ("results", "结果"), ("limitations", "局限")):
                print(f"  {lab}：{(qr.get('quickread') or {}).get(k)}")
            print(f"  标签：{qr.get('facets')}")
            print(f"  质量：{qr.get('quality')}")
        if st:
            print(f"  我的标记：{ {k: v for k, v in st.items() if k != 'history'} }")
        if (store.pdir(pid) / "explainer.html").exists():
            print(f"  精读：{store.pdir(pid) / 'explainer.html'}")
    elif a.action == "add":
        from . import manual
        if not a.ids:
            raise SystemExit("用法：wb paper add <arXiv号 | 链接 | \"标题\" | /服务器上/的.pdf>... [--seed] [--why 原因] [--explain]")
        res = manual.add(a.ids, seed=a.seed, why=a.why or "本人提供", explain=a.explain)
        return out_json(res) if a.json else None
    elif a.action == "rate":
        from . import taste
        if a.undo and a.ids:
            pid = resolve_pid(a.ids[0])
            taste.rate(pid, None, undo=True)
            print(f"{pid}：已撤销评价")
            return None
        if len(a.ids) != 2 or not a.ids[1].isdigit():
            raise SystemExit("用法：wb paper rate <论文> <1-5> [--note \"理由（尽量用本人原话）\"]   1 很差 · 2 差 · 3 一般 · 4 好 · 5 很好")
        pid = resolve_pid(a.ids[0])
        r = taste.rate(pid, int(a.ids[1]), a.note)
        m = store.load(pid) or {}
        print(f"{pid}（{(m.get('title') or '')[:60]}）：{r['score']} 分 {r['label']}" + (f"，理由：{r['note']}" if r.get("note") else ""))
        n = taste.new_since_profile()
        print(f"上次画像修改以来新增评价 {n} 条" + ("，每日流程会据此起草画像修改，等你确认" if n >= 3 else "（攒够 3 条会起草画像修改）"))
    elif a.action == "mark":
        keys = [k for k in ("read", "fav", "irrelevant", "reproduce") if getattr(a, k)]
        if not keys and not a.note:
            raise SystemExit("要标什么？--read / --fav / --irrelevant / --reproduce / --note")
        for x in a.ids:
            pid = resolve_pid(x)
            st = store.load_status(pid)
            for k in keys:
                if a.undo:
                    st.pop(k, None)
                else:
                    st[k] = now_iso()
            if a.note:
                st["note"] = a.note
            st.setdefault("history", []).append({"ts": now_iso(), "by": wbstore._actor(), "set": keys,
                                                 "undo": bool(a.undo), "note": a.note})
            store.save_status(pid, st)
            wbstore.journal("paper.mark", paper=pid, keys=keys, undo=bool(a.undo))
            print(f"{pid}：{'取消 ' if a.undo else ''}{'、'.join(keys) or '备注'}")


# ---------------- seed / field ----------------


def cmd_seed(a):
    s = fieldmod.seeds()
    if a.action == "ls":
        if a.json:
            return out_json(s)
        for x in s:
            print(f"{x['arxiv']:12} {x.get('title', '')[:80]}  （{x.get('why')}）")
    elif a.action == "add":
        aid = ids.arxiv_id(a.arxiv) or a.arxiv
        if any(x["arxiv"] == aid for x in s):
            raise SystemExit(f"{aid} 已经是种子")
        m = store.load(f"arxiv-{aid}") or {}
        s.append({"arxiv": aid, "title": m.get("title") or a.title, "why": a.why, "added_by": wbstore._actor(),
                  "added_at": now_iso()})
        fieldmod.save_seeds(s)
        wbstore.journal("seed.add", arxiv=aid, why=a.why)
        print(f"已加入种子 {aid}")
    elif a.action == "rm":
        aid = ids.arxiv_id(a.arxiv) or a.arxiv
        fieldmod.save_seeds([x for x in s if x["arxiv"] != aid])
        wbstore.journal("seed.rm", arxiv=aid)
        print(f"已移除种子 {aid}")


def cmd_field(a):
    if a.action == "init":
        if not a.kv:
            raise SystemExit("用法：wb field init <examples/fields/xxx.json 或示例名，如 mas-arch-opt>")
        print(fieldmod.init_from(a.kv[0]))
        wbstore.journal("field.init", source=a.kv[0])
        return
    if a.action in ("suggest", "apply"):
        from . import manual
        if a.action == "apply":
            return out_json(manual.apply())
        prop = manual.suggest()
        if a.json:
            return out_json(prop)
        print("建议的修改（还没写入；本人确认后执行 wb field apply）：")
        for c in prop.get("changes") or []:
            print(f"  · {c}")
        print(f"\n描述：{prop.get('description')}")
        print(f"子主题：{'、'.join(prop.get('subtopics') or [])}")
        before = set(prop["before"].get("arxiv_keywords") or [])
        after = prop.get("arxiv_keywords") or []
        print("检索词：" + "，".join(x if x in before else f"+{x}" for x in after)
              + ("；删掉：" + "，".join(sorted(before - set(after))) if before - set(after) else ""))
        return
    f = fieldmod.field()
    if a.action == "show":
        return out_json(f)
    from ..util import write_json
    hist = wbstore.registry_dir() / "field.history"
    hist.mkdir(parents=True, exist_ok=True)
    write_json(hist / f"field-{now_iso().replace(':', '')}.json", f)  # 旧版本留底
    for kv in a.kv:
        k, v = kv.split("=", 1)
        try:
            v = json.loads(v)
        except ValueError:
            pass
        cur = f
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    fieldmod.save_field(f)
    wbstore.journal("field.set", fields=a.kv)
    print("已更新研究方向配置")


# ---------------- idea / problem / map / results / report ----------------


def cmd_idea(a):
    from . import ideate
    from ..cli import table
    from ..util import read_json

    if a.action == "ls":
        its = ideate.all_ideas(include_hidden=a.hidden)
        if a.json:
            return out_json(its)
        table([[i["id"], i.get("status"), i.get("score"), (i.get("title") or "")[:60], i.get("problem_id") or "",
                "隐藏" if not i.get("informed") else ""] for i in its], ["IDEA", "状态", "均分", "标题", "问题", ""])
    elif a.action == "show":
        d = ideate.ideas_dir() / a.id
        if not d.exists():
            d = ideate.ideas_dir() / ".hidden" / a.id
        out_json({"idea": read_json(d / "idea.json"), "candidate": read_json(d / "candidate.json"),
                  "review": read_json(d / "review.json"), "doc": str(d / "idea.html") if (d / "idea.html").exists() else None})
    elif a.action == "delete":
        if not a.reason:
            raise SystemExit("删除 IDEA 只能在本人明确要求时做（ADR-0024）：用 --reason 写下本人的原话")
        ideate.delete(a.id, a.reason)
        print(f"已删除 {a.id}（移到 ideas/.deleted/，可恢复）")
    elif a.action in ("verify", "abandon", "promote", "note"):
        status = {"verify": "verifying", "abandon": "abandoned", "promote": "claim", "note": "noted"}[a.action]
        if a.action == "abandon" and not a.reason:
            raise SystemExit("放弃要写原因（--reason），以后能看到为什么没走这条路")
        it = ideate.set_status(a.id, status, note=a.reason, link=a.claim)
        print(f"{a.id} → {it['status']}" + (f"（{a.claim}）" if a.claim else ""))


def cmd_problem(a):
    from . import ideate

    ps = ideate.all_problems()
    if a.json:
        return out_json(ps)
    for p in ps:
        print(f"{p['id']}  {p.get('title')}  （{p.get('source')}，IDEA {len(p.get('ideas') or [])} 个，文档{'✓' if p.get('doc') else '✗'}）")


def cmd_map(a):
    from . import fieldmap

    if a.action == "show":
        m = fieldmap.load(a.version)
        if not m:
            raise SystemExit("还没有领域地图（每日流程会在第一次运行时生成，或 wb map rebuild）")
        if a.json:
            return out_json(m)
        print(f"领域地图 v{m['version']}（{m.get('n_papers')} 篇，{m.get('created_at')}）\n{m.get('overview', '')}\n")
        for c in m["primary"]["categories"]:
            print(f"[{c['id']}] {c['name']}（{len(c.get('members') or [])} 篇）：{c.get('idea')}")
            print(f"     没考虑到：{c.get('missing')}")
    elif a.action == "versions":
        for v in fieldmap.versions():
            print(v)
    elif a.action == "rebuild":
        print(fieldmap.rebuild(fresh=a.fresh))


def cmd_results(a):
    """wb results                   分类与数据集
    wb results <数据集>             这个数据集的全部报告（带评测规模）
    wb results refresh [--limit N]  归类新数据集名 + 补评测规模 / 方法名核对（按回填预算）+ 核实代码 + 重建索引"""
    from . import datasets, llm, results

    args = a.task or []
    if args[:1] == ["refresh"]:
        out = {"datasets": datasets.classify()}
        todo = datasets.pending()
        if a.limit:
            todo = todo[:a.limit]
        if todo:
            try:
                out["eval_sets"] = datasets.fill(todo, kind="backfill")["ok"]
            except llm.LLMPaused as e:
                out["eval_sets"] = f"暂停：{e}"
        from . import codecheck, namecheck
        todo = namecheck.pending()[:a.limit] if a.limit else namecheck.pending()
        if todo:
            try:
                out["names"] = namecheck.fill(todo, kind="backfill")["ok"]
            except llm.LLMPaused as e:
                out["names"] = f"暂停：{e}"
        out["code"] = codecheck.run(limit=a.limit or 1000)
        out.update(results.rebuild_index())
        out["pending"] = {"eval_sets": len(datasets.pending()), "names": len(namecheck.pending())}
        return out_json(out)
    rows = results.all_rows()
    if not args:
        bs = results.benchmarks(rows)
        if a.json:
            return out_json(bs)
        names = dict(datasets.CATEGORIES)
        for t, c, n in bs[:80]:
            print(f"{names.get(c, c):10} {t:28} {n} 篇")
        return None
    t = results.table(results.norm_task(" ".join(args)), rows)
    if a.json:
        return out_json(t)
    for g in t["groups"]:
        print(f"\n== {t['task']} · {g['metric']} · {g['backbone']}")
        for r in g["rows"][:30]:
            s = r["scale"]
            sz = {"full": "全量", "subset": f"子集{s['n'] or ''}", "unknown": "?"}[s["status"]]
            print(f"  {'*' if r['is_proposed'] else ' '} {r['value']:>8}  {r['method'][:30]:30}  {sz:8} 代码:{r['code']['status']:11} {r['pid']}  {r.get('where') or ''}")
    return None


def cmd_topvenue(a):
    """wb topvenue run | ls [--worth]：顶会顶刊必读（ADR-0040）。"""
    from . import topvenue

    if a.action == "run":
        return out_json(topvenue.run(discover_too=not a.no_discover))
    d = topvenue.load()
    items = d.get("worth" if a.worth else "must") or []
    if a.json:
        return out_json(items)
    for x in items:
        print(f"{(x.get('date') or '')[:7]}  {x['venue']['name']} {x['venue'].get('year') or ''}  {(x.get('title_zh') or x['title'])[:60]}  {x['pid']}")
        for w in x.get("why") or []:
            print(f"        · {w}")
    return None


def cmd_report(a):
    from . import reports

    if a.action == "build":
        r = reports.build(a.period, a.key)
        print(f"已生成 {a.period} {r['key']}：新收录 {r['n_new']}，精选 {r['n_picks']}")
    elif a.action == "hot":
        out_json(reports.hot(store.today()))
    else:
        keys = reports.list_reports(a.period)
        out_json(reports.load_report(a.period, a.key or (keys[0] if keys else "")) or {"error": "没有报告"})


def register(add):
    p = add("daily", cmd_daily, "每日论文流程：run / push / status / resume / repick（按最新口味重新精选，--force 同时推送）")
    p.add_argument("action", choices=["run", "push", "status", "resume", "repick"])
    p.add_argument("--reason", help="repick：为什么重新精选（写进推送）")
    p.add_argument("--date", help="期号日期 YYYY-MM-DD（默认今天，北京时间）")
    p.add_argument("--steps", help="只跑这些步骤，逗号分隔")
    p.add_argument("--force", action="store_true")
    p = add("backfill", cmd_backfill, "首次回填：引用 baseline 的论文 + arXiv 近 N 个月")
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--judge-only", action="store_true", help="不抓取，只对库里待判定的做判定和速读")
    p = add("paper", cmd_paper, "论文：ls / show / mark / add / rate（评价 1–5）")
    p.add_argument("action", choices=["ls", "show", "mark", "add", "rate"])
    p.add_argument("ids", nargs="*")
    p.add_argument("--picked", action="store_true")
    p.add_argument("--day")
    p.add_argument("--q")
    p.add_argument("--facet", action="append", help="维度筛选 key=value")
    p.add_argument("--limit", type=int, default=50)
    for k, h in (("read", "已读"), ("fav", "收藏"), ("irrelevant", "不相关（负样本）"), ("reproduce", "想复现")):
        p.add_argument(f"--{k}", action="store_true", help=h)
    p.add_argument("--undo", action="store_true")
    p.add_argument("--note")
    p.add_argument("--seed", action="store_true", help="add：同时设为种子")
    p.add_argument("--why", help="add：为什么提供这篇（会写进画像材料）")
    p.add_argument("--explain", action="store_true", help="add：顺带做 8 步精读")
    p = add("seed", cmd_seed, "种子论文：ls / add / rm")
    p.add_argument("action", choices=["ls", "add", "rm"])
    p.add_argument("arxiv", nargs="?")
    p.add_argument("--why", default="本人指定")
    p.add_argument("--title")
    p = add("idea", cmd_idea, "IDEA：ls / show / verify / abandon / promote / note / delete（删除只在本人要求时）")
    p.add_argument("action", choices=["ls", "show", "verify", "abandon", "promote", "note", "delete"])
    p.add_argument("id", nargs="?")
    p.add_argument("--hidden", action="store_true", help="包括没通过评审的")
    p.add_argument("--reason")
    p.add_argument("--claim", help="promote：升级成的 claim，如 mypaper/C2")
    add("problem", cmd_problem, "列出问题 Problem")
    p = add("map", cmd_map, "领域地图：show / versions / rebuild [--fresh]")
    p.add_argument("action", choices=["show", "versions", "rebuild"])
    p.add_argument("--version", type=int)
    p.add_argument("--fresh", action="store_true", help="rebuild：全面重建（重新划分类别、重选代表工作、重写没考虑到什么），不照搬上一版")
    p = add("results", cmd_results, "跨论文结果表：不给参数列数据集；<数据集> 看报告；refresh 归类 + 补评测规模 / 方法名核对 + 核实代码 + 重建索引")
    p.add_argument("task", nargs="*")
    p.add_argument("--limit", type=int, default=0)
    p = add("topvenue", cmd_topvenue, "顶会顶刊必读：run（检索 + 判定 + 录用信息 + 清单）/ ls [--worth]")
    p.add_argument("action", choices=["run", "ls"])
    p.add_argument("--worth", action="store_true", help="ls：看值得读（默认看必读）")
    p.add_argument("--no-discover", action="store_true", help="run：不重新检索，只更新录用信息和清单")
    p = add("report", cmd_report, "周报 / 月报 / 热点：build / show / hot")
    p.add_argument("action", choices=["build", "show", "hot"])
    p.add_argument("--period", choices=["weekly", "monthly"], default="weekly")
    p.add_argument("--key")
    p = add("field", cmd_field, "研究方向：init <示例> / show / set k=v / suggest（据论文起草修改）/ apply（确认写入）")
    p.add_argument("action", choices=["init", "show", "set", "suggest", "apply"])
    p.add_argument("kv", nargs="*")

