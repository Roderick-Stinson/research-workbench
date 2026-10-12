"""领域地图（ADR-0021，P3）：多维度 + 一个主分类，写成 Related Work 文体；每天归类、每周最多重组一次、版本全留。

map/v<N>.json   一个版本：主分类（每类：名称、核心思路、代表工作、这一类没考虑到什么、成员）+ 相对上一版的变化
map/current     指向最新版本号
重组由 codex 完成（读整个论文库的速读摘要）；每日归类由批量模型完成（只把新论文放进已有类别）。
"""
from __future__ import annotations

import datetime as _dt
import json

from .. import config
from ..util import now_iso, read_json, write_json
from . import codex, llm, store
from . import field as fieldmod


def map_dir():
    return config.home() / "map"


def current_version() -> int:
    p = map_dir() / "current"
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return 0


def load(version: int | None = None) -> dict | None:
    v = version or current_version()
    return read_json(map_dir() / f"v{v}.json") if v else None


def versions() -> list:
    d = map_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("v*.json"), key=lambda p: int(p.stem[1:])):
        m = read_json(p) or {}
        out.append({"version": int(p.stem[1:]), "created_at": m.get("created_at"), "papers": m.get("n_papers"),
                    "categories": len((m.get("primary") or {}).get("categories") or [])})
    return out


def _save_new(m: dict) -> int:
    v = current_version() + 1
    m["version"] = v
    m["created_at"] = now_iso()
    write_json(map_dir() / f"v{v}.json", m)
    (map_dir() / "current").write_text(str(v))
    return v


def _library_digest() -> list:
    out = []
    for m in store.all_papers():
        if m.get("intake", {}).get("state") != "relevant":
            continue
        qr = m.get("_qr") or {}
        out.append({"id": m["id"], "title": m.get("title"), "title_zh": qr.get("title_zh"),
                    "published": m.get("published"), "venue": m.get("venue"), "seed": bool(m.get("seed")),
                    "one_liner": qr.get("one_liner"), "method": (qr.get("quickread") or {}).get("method"),
                    "limitations": (qr.get("quickread") or {}).get("limitations"), "facets": qr.get("facets")})
    return out


REBUILD_PROMPT = """你在为研究方向「{name}」整理一份 Related Work 式的领域地图（ADR-0021）。

输入（当前目录）：
- library.json：论文库里属于该方向的全部论文（id、标题、一句话、方法、局限、维度标签）
- previous_map.json：上一版地图（没有就是第一版）
- field.json：方向描述与维度定义

要求：
1. 主分类按维度「{primary_label}」切分（可以细化、合并原有取值），通常 4–8 类；每类写：
   - name：类名；idea：这一类的核心思路（1–2 句）；
   - representative：2–5 篇代表工作（只能用 library.json 里的 id）；
   - members：属于这一类的全部论文 id（每篇只归一个主类，跨类的按主要贡献归）；
   - missing：这一类**没考虑到什么**（要具体，最好能指出 2 篇以上论文共有的局限，引用其 id）；
   - prose：一段 Related Work 文体的中文叙述（3–6 句），提到代表工作时用 [id] 标注。
2. 与上一版相比如果重组了（拆 / 合 / 改名），在 changes 里说明；没有大变化就尽量保持类别稳定。
3. 只能依据输入里的信息，不要编造论文或结论。
输出：把 JSON 写到 ./map.json，结构：
{{"primary": {{"facet": "{primary_key}", "categories": [{{"id": "c1", "name": "", "idea": "", "representative": [], "members": [], "missing": "", "prose": ""}}]}},
  "overview": "整个领域现状的一段话（3–5 句）", "changes": "相对上一版的变化"}}
完成后只回复 DONE 或 FAILED: 原因。
"""


def rebuild(log=print) -> dict:
    f = fieldmod.field()
    pk = next((k for k, d in f["facets"].items() if d.get("primary")), "optimizes")
    w = map_dir() / "work"
    w.mkdir(parents=True, exist_ok=True)
    lib = _library_digest()
    (w / "library.json").write_text(json.dumps(lib, ensure_ascii=False))
    (w / "previous_map.json").write_text(json.dumps(load() or {}, ensure_ascii=False))
    (w / "field.json").write_text(json.dumps(f, ensure_ascii=False))
    (w / "map.json").unlink(missing_ok=True)
    res = codex.run("map:rebuild", w, REBUILD_PROMPT.format(name=f["name"], primary_label=f["facets"][pk]["label"],
                                                             primary_key=pk))
    if not res["ok"] or not (w / "map.json").exists():
        raise RuntimeError(f"领域地图重组失败：{res.get('error') or '没有产出 map.json'}")
    m = json.loads((w / "map.json").read_text())
    known = {p["id"] for p in lib}
    for c in m["primary"]["categories"]:  # 只保留库里真实存在的 id
        c["members"] = [x for x in c.get("members") or [] if x in known]
        c["representative"] = [x for x in c.get("representative") or [] if x in known]
    m["n_papers"] = len(lib)
    m["kind"] = "rebuild"
    v = _save_new(m)
    log(f"领域地图重组为 v{v}（{len(m['primary']['categories'])} 类，{len(lib)} 篇）")
    return {"version": v, "categories": len(m["primary"]["categories"])}


ASSIGN_SYS = """把新论文归入已有的主分类。只能选给出的类别 id；实在都不合适写 "other"。
输出 JSON：{"items": [{"id": "论文 id", "category": "类别 id", "reason": "一句话"}]}"""


def assign_new(pids: list, log=print) -> dict:
    """每日归类：新论文放进当前版本的类别里（生成一个新版本，kind=assign）。"""
    cur = load()
    if not cur:
        return {"skipped": "还没有地图（等第一次重组）"}
    have = {x for c in cur["primary"]["categories"] for x in c.get("members") or []}
    todo = [store.load(p) for p in pids if p not in have]
    todo = [m for m in todo if m and m.get("intake", {}).get("state") == "relevant"]
    if not todo:
        return {"assigned": 0}
    cats = [{"id": c["id"], "name": c["name"], "idea": c["idea"]} for c in cur["primary"]["categories"]]
    items = [{"id": m["id"], "title": m["title"], "one_liner": (store.load_quickread(m["id"]) or {}).get("one_liner"),
              "abstract": (m.get("abstract") or "")[:800]} for m in todo]
    res = llm.chat(ASSIGN_SYS, json.dumps({"categories": cats, "papers": items}, ensure_ascii=False), task="map-assign")
    new = json.loads(json.dumps(cur))
    by = {c["id"]: c for c in new["primary"]["categories"]}
    n = 0
    for it in res.get("items") or []:
        c = by.get(it.get("category"))
        if c and it.get("id") in {m["id"] for m in todo}:
            c.setdefault("members", []).append(it["id"])
            n += 1
    new["kind"] = "assign"
    new["changes"] = f"每日归类：新增 {n} 篇（{_dt.date.today().isoformat()}）"
    new["n_papers"] = (cur.get("n_papers") or 0) + n
    v = _save_new(new)
    log(f"领域地图 v{v}：归类 {n} 篇")
    return {"version": v, "assigned": n}


def due_for_rebuild(day: str) -> bool:
    """每周最多重组一次：没有地图，或上次重组在 7 天前。"""
    last = None
    for v in reversed(versions()):
        m = load(v["version"]) or {}
        if m.get("kind") == "rebuild":
            last = m.get("created_at")
            break
    if not last:
        return True
    return (_dt.date.fromisoformat(day) - _dt.date.fromisoformat(last[:10])).days >= 7
