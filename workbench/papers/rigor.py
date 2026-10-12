"""精选前的硬核检查（ADR-0037）：这篇论文有没有和领域常用方法、在常用 benchmark 上正面比较。

"领域常用方法"从论文库自己算：被至少 5 篇论文当作对比方法的方法（如 AFlow、ADAS、MaAS、G-Designer、GPTSwarm、AgentPrune），
去掉通用做法（CoT、ReAct、Debate……）、模型名（GPT-4o……）和现成的 agent 工具（Claude Code、Codex……）。
可以在画像里用 selection.standard_baselines 直接指定（覆盖自动计算）。"常用 benchmark" = 至少 5 篇论文用过的数据集。

方法类论文一个领域常用方法都没比 → 不进精选（selection.require_std_baselines，默认开）。
证据同时交给中稿评估（taste.judge），并显示在面板和推送里。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import field as fieldmod
from . import results

GENERIC = {
    "cot", "cotsc", "selfconsistency", "sc", "react", "vanilla", "baseline", "single", "singleagent", "singlellm", "base",
    "baseagent", "random", "original", "chain", "star", "tree", "completegraph", "fullgraph", "debate", "llmdebate", "mad",
    "multiagentdebate", "io", "direct", "directprompting", "zeroshot", "fewshot", "selfrefine", "reflexion", "bestmember",
    "majorityvote", "majorityvoting", "voting", "humandesigned", "manual", "default", "oracle", "llm", "agent",
}
TOOLS = {"claudecode", "codex", "opencode", "openhands", "terminus2", "terminuskira", "sweagent", "minisweagent", "aider",
         "cursor", "geminicli", "ohmypi", "pi"}
MODEL_RX = re.compile(r"^(gpt|claude|qwen|llama|deepseek|gemini|o\d|mistral|glm|kimi|grok|phi|gemma)", re.I)


def _excluded(mkey: str) -> bool:
    return mkey in GENERIC or mkey in TOOLS or bool(MODEL_RX.match(mkey))


def standards(rows: list | None = None, min_papers: int = 5) -> dict:
    """{"baselines": {mkey: 显示名}, "benchmarks": {数据集名}}。"""
    rows = rows if rows is not None else results.all_rows()
    sel = fieldmod.cfg_selection()
    cmp, names, dsp = defaultdict(set), defaultdict(Counter), defaultdict(set)
    for r in rows:
        dsp[r["dataset"]].add(r["pid"])
        if not r["is_proposed"]:
            cmp[r["mkey"]].add(r["pid"])
            names[r["mkey"]][results.row_name(r["method"])] += 1
    if sel.get("standard_baselines"):
        base = {results.m_key(n): n for n in sel["standard_baselines"]}
    else:
        base = {k: names[k].most_common(1)[0][0] for k, ps in cmp.items() if len(ps) >= min_papers and not _excluded(k)}
    bench = {d for d, ps in dsp.items() if len(ps) >= min_papers}
    return {"baselines": base, "benchmarks": bench}


def evidence(pid: str, rows: list, std: dict) -> dict:
    mine = [r for r in rows if r["pid"] == pid]
    compared = {r["mkey"] for r in mine if not r["is_proposed"]}
    std_b = sorted({std["baselines"][k] for k in compared if k in std["baselines"]})
    ds = {r["dataset"] for r in mine}
    std_ds = sorted(ds & std["benchmarks"])
    subset = [r for r in mine if r["scale"]["status"] == "subset"]
    return {"std_baselines": std_b, "n_compared": len(compared), "std_benchmarks": std_ds,
            "own_benchmarks": sorted(ds - std["benchmarks"])[:6], "subset_share": round(len(subset) / len(mine), 2) if mine else None,
            "has_ablation": any(results.ABLATION_RX.search(r["method"] or "") for r in mine), "has_results": bool(mine)}


def text(ev: dict) -> str:
    """给中稿评估看的一行证据。"""
    return (f"对比的领域常用方法：{'、'.join(ev['std_baselines']) or '无'}（共对比 {ev['n_compared']} 个方法）；"
            f"常用 benchmark：{'、'.join(ev['std_benchmarks']) or '无'}；其它 benchmark：{'、'.join(ev['own_benchmarks']) or '无'}；"
            f"子集评测占比 {ev['subset_share'] if ev['subset_share'] is not None else '?'}；有消融：{'是' if ev['has_ablation'] else '否'}")


def gate(meta: dict, qr: dict, ev: dict) -> str | None:
    """不进精选的原因；None = 可以。"""
    sel = fieldmod.cfg_selection()
    if not sel.get("require_std_baselines", True):
        return None
    is_method = "方法" in (((qr or {}).get("facets") or {}).get("kind") or [])
    if is_method and ev["has_results"] and not ev["std_baselines"]:
        return "方法论文，但没有和领域常用方法比较"
    return None
