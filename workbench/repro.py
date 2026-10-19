"""复现（ADR-0023，P5）：配置组记录论文报告的数字，和我们跑出来的均值 ± 标准差比，判断"已复现 / 复现差距"。

配置组新增字段：
  paper          关联论文（论文库 id，如 arxiv-2502.04180）
  reported       {我们的指标名: {value, task, metric, backbone, where}}  论文报告的数字（参照）
  tolerance      容差（默认 2.0，单位与指标相同；论文报告了标准差时取 max(容差, 论文 std)）
  repro_status   未开始 / 进行中 / 已复现 / 复现差距（由 wb repro check 计算，也可手动设）
"""
from __future__ import annotations

from . import stats, store
from .util import now_iso


def compare(v: dict, runs: list | None = None) -> list:
    runs = runs if runs is not None else stats.variant_runs(v["id"])
    agg = stats.aggregate(runs)
    tol = float(v.get("tolerance") or 2.0)
    out = []
    for name, ref in (v.get("reported") or {}).items():
        a = agg.get(name)
        row = {"metric": name, "reported": ref.get("value"), "where": ref.get("where"), "paper_metric": ref.get("metric"),
               "task": ref.get("task"), "backbone": ref.get("backbone"),
               "ours_mean": a["mean"] if a else None, "ours_std": a["std"] if a else None, "n": a["n"] if a else 0}
        if a and isinstance(ref.get("value"), (int, float)):
            t = max(tol, float(ref.get("std") or 0))
            row["gap"] = round(a["mean"] - ref["value"], 4)
            row["within"] = abs(row["gap"]) <= t
            row["tolerance"] = t
        out.append(row)
    return out


def status_of(v: dict, runs: list | None = None) -> str:
    """未开始 / 进行中（还有 Run 在跑）/ 已复现 / 复现差距。论文报告了、但我们没跑的指标（例如没跑的基准）不影响判断，页面上单独说明。"""
    runs = runs if runs is not None else stats.variant_runs(v["id"])
    rows = compare(v, runs)
    if not rows:
        return v.get("repro_status") or "未开始"
    measured = [r for r in rows if "within" in r]
    if any(r.get("state") == "running" for r in runs):
        return "进行中"
    if not measured:
        return "未开始"
    return "已复现" if all(r["within"] for r in measured) else "复现差距"


def overview(v: dict, runs: list) -> dict:
    """一个配置组的复现概览：Run 数、运行中、状态、容差内几项、和论文对比的各行。"""
    rows = compare(v, runs) if v.get("reported") else []
    done = [r for r in rows if "within" in r]
    return {"v": v, "n_runs": len(runs), "running": sum(1 for r in runs if r.get("state") == "running"),
            "status": status_of(v, runs) if v.get("reported") else None, "rows": rows,
            "n_unmeasured": sum(1 for r in rows if "within" not in r),
            "n_within": sum(1 for r in done if r["within"]), "n_compared": len(done),
            "worst": sorted((r for r in done if not r["within"]), key=lambda r: -abs(r["gap"]))[:3]}


def unattached(projects: list | None = None) -> list:
    """没挂到任何项目（claim）上的配置组。复现别人的论文就是这种：按约定，别人的论文不建项目（CONTEXT.md）。"""
    projects = projects if projects is not None else store.all_projects()
    used = {vid for p in projects for e in p.get("experiments", []) for vid in e.get("variants", [])}
    return [v for v in store.all_variants() if v["id"] not in used]


def cmd_repro(a):
    from .cli import out_json, table
    from .papers import results as presults

    v = store.load_variant(a.variant)
    if not v:
        raise SystemExit(f"没有这个配置组：{a.variant}")
    if a.action == "ref":
        if a.paper:
            v["paper"] = a.paper
        if a.tolerance is not None:
            v["tolerance"] = a.tolerance
        for kv in a.set or []:
            name, val = kv.split("=", 1)
            v.setdefault("reported", {})[name] = {"value": float(val), "where": a.where, "set_by": "manual",
                                                  "at": now_iso()}
        if a.from_paper:
            name = a.as_ or a.paper_metric
            hits = presults.reported_for(a.from_paper, a.task, a.paper_metric, a.backbone)
            if not hits:
                raise SystemExit("论文库里没找到对应数字（先看 wb paper show 里的抽取结果，或用 --set 手填）")
            if len(hits) > 1 and not a.pick:
                for i, h in enumerate(hits):
                    print(f"[{i}] {h.get('task')} {h.get('metric')} {h.get('backbone')} = {h.get('value')} （{h.get('where')}）")
                raise SystemExit("匹配到多条，用 --pick N 选一条")
            h = hits[a.pick or 0]
            v["paper"] = v.get("paper") or a.from_paper
            v.setdefault("reported", {})[name] = {"value": h["value"], "task": h.get("task"), "metric": h.get("metric"),
                                                  "backbone": h.get("backbone"), "where": h.get("where"),
                                                  "set_by": f"paper:{a.from_paper}", "at": now_iso()}
        v["repro_status"] = status_of(v)
        store.save_variant(v)
        store.journal("repro.ref", variant=v["id"], reported=v.get("reported"))
        print(f"{v['id']}：参照数字 {list((v.get('reported') or {}).keys())}，状态 {v['repro_status']}")
    elif a.action == "check":
        rows = compare(v)
        v["repro_status"] = status_of(v)
        store.save_variant(v)
        if a.json:
            return out_json({"status": v["repro_status"], "rows": rows})
        table([[r["metric"], r["reported"], stats.fmt_ms(r["ours_mean"], r["ours_std"]), r["n"], r.get("gap", "-"),
                "✓" if r.get("within") else ("✗" if "within" in r else "-"), r.get("where") or ""] for r in rows],
              ["指标", "论文报告", "我们（均值±std）", "n", "差", "容差内", "出处"])
        print(f"状态：{v['repro_status']}")


def register(add):
    p = add("repro", cmd_repro, "复现：ref（记录论文报告的数字）/ check（和我们的结果比）")
    p.add_argument("action", choices=["ref", "check"])
    p.add_argument("variant")
    p.add_argument("--paper", help="关联的论文库 id")
    p.add_argument("--set", action="append", help="手填：我们的指标名=论文报告值")
    p.add_argument("--where", help="手填时的出处，如 'Table 1'")
    p.add_argument("--from-paper", help="从论文库抽取的结果里取")
    p.add_argument("--task")
    p.add_argument("--paper-metric")
    p.add_argument("--backbone")
    p.add_argument("--as", dest="as_", help="在我们这边叫什么指标名")
    p.add_argument("--pick", type=int)
    p.add_argument("--tolerance", type=float)
