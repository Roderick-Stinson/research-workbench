"""结果表用的数据集整理（ADR-0034）。

1. 名称归一 + 分类：同一个数据集的不同写法（"GPQA-Diamond" / "GPQA Diamond"）合并，不同版本（AIME 2024 / AIME 2025、
   SWE-bench / SWE-bench Verified）分开；分到 数学 / 代码生成 / 问答 / 知识与综合推理 / 智能体 / 其它。
   常见的写在 KNOWN 里（含官方全量规模），其余由模型归类一次，存 registry/datasets.json，以后只归类新出现的名字。
2. 评测规模：每篇论文在每个数据集上实际评测了多少题、是全量还是抽样 / 子集、怎么选的。
   从原文抽取，原文没写就是"未注明"，**不可以猜**。新论文在速读里一起抽（eval_sets 字段），老论文由 fill() 补。
"""
from __future__ import annotations

import concurrent.futures as cf
import re

from .. import store as wbstore
from ..util import now_iso, read_json, write_json
from . import fulltext, llm, store

CATEGORIES = [("math", "数学"), ("code", "代码生成"), ("qa", "问答"), ("knowledge", "知识与综合推理"),
              ("agent", "智能体 / 交互环境"), ("other", "其它")]
CAT_NAMES = dict(CATEGORIES)

# (名称, 分类, 官方全量 (划分, 题数) 或 None, 别名)。全量只写确定的；不确定就 None，页面上显示"以论文为准"。
KNOWN = [
    ("GSM8K", "math", ("test", 1319), ["gsm-8k"]),
    ("MATH", "math", ("test", 5000), ["competition math", "math dataset"]),
    ("MATH-500", "math", ("test", 500), ["math500"]),
    ("GSM-Hard", "math", ("test", 1319), ["gsmhard"]),
    ("MultiArith", "math", ("全部", 600), []),
    ("SVAMP", "math", ("全部", 1000), []),
    ("AQuA", "math", ("test", 254), ["aqua-rat", "aquarat"]),
    ("AIME 2024", "math", ("全部", 30), ["aime24", "aime 24", "aime-2024"]),
    ("AIME 2025", "math", ("全部", 30), ["aime25", "aime 25", "aime-2025"]),
    ("AIME", "math", None, []),
    ("AMC 2023", "math", ("全部", 40), ["amc23", "amc 23"]),
    ("HumanEval", "code", ("test", 164), ["human eval", "openai humaneval"]),
    ("MBPP", "code", None, []),
    ("LiveCodeBench", "code", None, ["lcb"]),
    ("BigCodeBench", "code", None, []),
    ("APPS", "code", None, []),
    ("DS-1000", "code", ("全部", 1000), ["ds1000"]),
    ("HotpotQA", "qa", ("dev", 7405), ["hotpot qa"]),
    ("DROP", "qa", ("dev", 9536), []),
    ("MuSiQue", "qa", None, []),
    ("TriviaQA", "qa", None, []),
    ("2WikiMultihopQA", "qa", None, ["2wiki", "2wikimqa", "2wikimultihop"]),
    ("Bamboogle", "qa", ("全部", 125), []),
    ("MMLU", "knowledge", ("test", 14042), []),
    ("MMLU-Pro", "knowledge", ("test", 12032), ["mmlupro"]),
    ("GPQA", "knowledge", ("main", 448), []),
    ("GPQA Diamond", "knowledge", ("diamond", 198), ["gpqa-d", "gpqa (diamond)"]),
    ("BBH", "knowledge", None, ["big-bench hard", "bigbench hard"]),
    ("ARC-Challenge", "knowledge", ("test", 1172), ["arc-c", "arc challenge"]),
    ("MedQA", "knowledge", None, []),
    ("HLE", "knowledge", None, ["humanity's last exam", "humanitys last exam"]),
    ("GAIA", "agent", None, []),
    ("ALFWorld", "agent", None, []),
    ("WebShop", "agent", None, []),
    ("SWE-bench", "agent", ("test", 2294), []),
    ("SWE-bench Verified", "agent", ("test", 500), ["swe-bench-verified", "swebench verified"]),
    ("SWE-bench Lite", "agent", ("test", 300), []),
    ("WebArena", "agent", ("全部", 812), []),
    ("Terminal-Bench", "agent", None, []),
    ("BrowseComp", "agent", ("全部", 1266), []),
]


def key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


_KNOWN = {}
for _n, _c, _f, _al in KNOWN:
    for _a in [_n] + _al:
        _KNOWN[key(_a)] = {"name": _n, "category": _c, "full": {"split": _f[0], "n": _f[1]} if _f else None}
FULL = {v["name"]: v["full"] for v in _KNOWN.values()}
AGG = re.compile(r"^(avg|average|mean|overall|all|total|macroavg|microavg)", re.I)


def registry_path():
    return wbstore.registry_dir() / "datasets.json"


def registry() -> dict:
    return read_json(registry_path(), {}) or {}


def lookup(raw: str | None, reg: dict | None = None) -> dict:
    """原始数据集名 → {name, category, aggregate, full, pending}。pending：还没归类（暂按原名、分到其它）。"""
    k = key(raw)
    if not k:
        return {"name": "?", "category": "other", "aggregate": True, "full": None}
    if k in _KNOWN:
        return dict(_KNOWN[k], aggregate=False)
    reg = registry() if reg is None else reg
    hit = (reg.get("names") or {}).get(k)
    if hit:
        kn = _KNOWN.get(key(hit.get("name")))  # 模型归到了某个 KNOWN 数据集
        return {"name": kn["name"] if kn else hit.get("name") or raw.strip(), "category": kn["category"] if kn else hit.get("category") or "other",
                "aggregate": bool(hit.get("aggregate")), "full": kn["full"] if kn else None}
    return {"name": raw.strip(), "category": "other", "aggregate": bool(AGG.match(k)), "full": None, "pending": True}


SYS_CLASSIFY = """你在整理多篇论文实验表格里的"数据集 / benchmark"名称。对每个原始名称：
1. name：规范名。同一个数据集的不同写法合并成一个名字（优先用下面"已有规范名"里的写法）；
   但不同版本 / 年份 / 子集要分开（AIME 2024 ≠ AIME 2025，GPQA ≠ GPQA Diamond，SWE-bench ≠ SWE-bench Verified，MATH ≠ MATH-500）。
2. category：math（数学）/ code（函数级代码生成）/ qa（问答、阅读理解、多跳检索问答）/ knowledge（知识与综合推理：MMLU、GPQA、BBH、医学法律考试等多选题）
   / agent（智能体与交互环境：网页、具身、工具、软件工程仓库级任务、终端）/ other（其它）。
3. aggregate：如果它不是一个数据集，而是"平均 / 总体 / 多个数据集的合计"或表格里的分组名，填 true。
不认识的名字也要给出最合理的分类，name 保持原样即可。
输出 JSON：{"items": [{"i": 序号, "name": "", "category": "", "aggregate": false}]}，每个都要有。"""


def classify(log=print, kind: str = "daily", batch: int = 40) -> dict:
    """把还没归类的数据集名交给模型归类，增量写入 registry/datasets.json。"""
    reg = registry()
    names = reg.setdefault("names", {})
    raw = {}
    for m in store.all_papers():
        for r in (m.get("_qr") or {}).get("results") or []:
            k = key(r.get("task"))
            if k and k not in _KNOWN and k not in names and not AGG.match(k):
                raw.setdefault(k, r["task"].strip())
    todo = list(raw.items())
    done = 0
    canon = sorted({n for n, *_ in KNOWN} | {v.get("name") for v in names.values() if v.get("name")})
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        user = "已有规范名：" + "；".join(canon[:500]) + "\n\n原始名称：\n" + "\n".join(f"[{j}] {n}" for j, (_, n) in enumerate(part))
        res = llm.chat(SYS_CLASSIFY, user, task="dataset-classify", max_tokens=6000, kind=kind)
        for it in res.get("items") or []:
            try:
                k, n = part[int(it["i"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            cat = it.get("category") if it.get("category") in CAT_NAMES else "other"
            names[k] = {"raw": n, "name": (it.get("name") or n).strip(), "category": cat, "aggregate": bool(it.get("aggregate"))}
            done += 1
        reg["updated_at"] = now_iso()
        write_json(registry_path(), reg)
        canon = sorted(set(canon) | {v.get("name") for v in names.values() if v.get("name")})
    log(f"数据集归类：新归类 {done} 个，共 {len(names)} 个")
    return {"classified": done, "total": len(names)}


# ---------------- 评测规模 ----------------

EVAL_FIELD = """"eval_sets": [{"task": "数据集名（与 results 里写法一致）", "split": "test / dev / validation 或 null",
              "n_eval": 实际评测的题数或 null, "full_size": 原文给出的该划分总题数或 null,
              "sampled": true（随机抽样 / 只用了一部分 / 子集）| false（原文明确说用了完整的划分）| null（原文没说）,
              "how": "一句话：怎么选的（如'沿用 AFlow 的设定，取 MATH 中 617 道 level-5 题'），原文没写就 null", "where": "Sec 4.1 / Appendix B"}]"""

SYS_EVAL = """你在核对论文实验的评测规模。对下面列出的每个数据集，从论文原文里找：用的是哪个划分、实际评测了多少题、
是完整的划分还是抽样 / 子集、怎么选的。硬性规则：只写原文明确说的；原文没写的字段填 null，**不可以猜**，
也不要用你自己知道的数据集规模去补（full_size 只填原文里出现的数字）。
输出 JSON：{"eval_sets": [{"task": "数据集名（与列表一致）", "split": "test / dev / validation 或 null", "n_eval": 实际评测的题数或 null,
"full_size": 原文给出的该划分总题数或 null, "sampled": true|false|null, "how": "一句话：怎么选的，原文没写就 null", "where": "Sec 4.1 / Appendix B"}]}
sampled：true = 原文说随机抽样 / 只用了一部分 / 用了某个子集；false = 原文明确说用了完整的测试集 / 全部题目；没说就 null。"""

_KW = ("sample", "subset", "test set", "split", "problems", "questions", "instances", "examples", "randomly", "evaluate on",
       "benchmark", "dataset", "following", "consist", "select")


def _focus(text: str, names: list, cap: int = 60_000) -> str:
    """长全文只保留提到这些数据集或评测设置的段落（及前后一段），省额度。"""
    if len(text) <= cap:
        return text
    lines = text.split("\n")
    kws = [n.lower() for n in names if len(n) >= 3] + list(_KW)
    keep = set()
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(k in low for k in kws):
            keep.update((i - 1, i, i + 1))
    out = "\n".join(lines[i] for i in sorted(keep) if 0 <= i < len(lines))
    return out[:cap]


def _int(v):
    try:
        return int(str(v).replace(",", "").strip()) if v not in (None, "", "null") else None
    except ValueError:
        return None


def norm_eval_sets(items) -> list:
    out = []
    for it in items or []:
        if not isinstance(it, dict) or not it.get("task"):
            continue
        s = it.get("sampled")
        out.append({"task": str(it["task"]).strip(), "split": it.get("split") or None, "n_eval": _int(it.get("n_eval")),
                    "full_size": _int(it.get("full_size")), "sampled": s if isinstance(s, bool) else None,
                    "how": it.get("how") or None, "where": it.get("where") or None})
    return out


def eval_sets(pid: str, kind: str = "daily") -> list:
    """为一篇已速读的论文补抽评测规模，写回速读的 eval_sets。"""
    qr = store.load_quickread(pid) or {}
    names = sorted({r["task"].strip() for r in qr.get("results") or [] if r.get("task") and isinstance(r.get("value"), (int, float))
                    and not lookup(r["task"]).get("aggregate")})
    items = []
    if names:
        meta = store.load(pid)
        if llm.creds().get("WB_LLM_PROTOCOL") == "fake":
            text = f"{meta.get('title')}\n\n{meta.get('abstract')}"
        else:
            text, _ = fulltext.fetch(meta)
        user = f"标题：{meta.get('title')}\n需要核对的数据集：{'；'.join(names)}\n\n=== 原文 ===\n{_focus(text, names)}"
        items = norm_eval_sets(llm.chat(SYS_EVAL, user, task="evalsets", max_tokens=4000, kind=kind).get("eval_sets"))
    qr = store.load_quickread(pid) or qr
    qr["eval_sets"] = items
    qr["eval_sets_at"] = now_iso()
    store.save_quickread(pid, qr)
    return items


def pending(pids: list | None = None) -> list:
    """已速读、有抽取结果、还没有评测规模的论文（新的在前）。"""
    want = set(pids) if pids is not None else None
    ms = [m for m in store.all_papers() if m.get("intake", {}).get("state") == "relevant" and (m.get("_qr") or {}).get("results")
          and "eval_sets" not in (m.get("_qr") or {}) and (want is None or m["id"] in want)]
    ms.sort(key=lambda m: m.get("published") or "", reverse=True)
    return [m["id"] for m in ms]


def fill(pids: list, kind: str = "daily", workers: int = 6, log=print) -> dict:
    ok, failed = 0, []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(eval_sets, p, kind): p for p in pids}
        for fu in cf.as_completed(futs):
            try:
                fu.result()
                ok += 1
            except (llm.LLMNotConfigured, llm.LLMPaused):
                for f2 in futs:
                    f2.cancel()
                log(f"评测规模：补了 {ok} 篇后暂停（额度 / 预算）")
                raise
            except Exception as e:  # noqa: BLE001
                failed.append({"id": futs[fu], "error": str(e)[:300]})
    log(f"评测规模：补了 {ok} 篇，失败 {len(failed)} 篇")
    return {"ok": ok, "failed": failed}


def scale(ev: dict | None, full: dict | None) -> dict:
    """一条结果的评测规模判定：full（全量）/ subset（抽样或子集）/ unknown（原文没写清楚）。"""
    ev = ev or {}
    n, fs = ev.get("n_eval"), ev.get("full_size") or (full or {}).get("n")
    if ev.get("sampled") is True or (n and fs and n < fs):
        st = "subset"
    elif ev.get("sampled") is False or (n and fs and n >= fs):
        st = "full"
    else:
        st = "unknown"
    return {"status": st, "n": n, "full": fs, "split": ev.get("split"), "how": ev.get("how"), "where": ev.get("where")}
