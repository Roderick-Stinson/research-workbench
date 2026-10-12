"""核对速读抽取的方法名是否就是论文里的叫法（ADR-0035）。

速读一次抽很多数字，方法名偶尔会写错（用了别的论文的方法名、把表格的行和列拼成一个名字、大小写不一致）。
对每篇有结果抽取的论文：先机械检查每个名字是否出现在原文里，再把"名字列表 + 原文相关段落"交给模型逐个核对：
原文里的准确写法、是不是本文提出的方法。核对结果存进速读的 method_check（不改原始抽取），结果表按它显示更正后的名字，
悬停可以看到原来抽取的写法。
"""
from __future__ import annotations

import concurrent.futures as cf
import re

from ..util import now_iso
from . import fulltext, llm, store

SYS = """你在核对从一篇论文的实验表格里抽取出来的"方法名"是否和论文原文一致。对列表里的每个名字：
1. name：这个方法在原文里的准确写法（照抄原文，保留大小写和连字符）。
   - 如果抽取的名字是把表格的行名和列名 / 设置拼在一起，写成"方法名（设置）"，方法名部分照抄原文；
   - 如果是本文方法的变体（消融、不同配置），写成"本文方法名（变体说明）"；
   - 如果原文里找不到对应的方法（抽错了），写 null。
2. is_proposed：是不是这篇论文自己提出的方法（含其变体）。基线、对比方法、通用做法（CoT、Self-Consistency 等）是 false。
另外给出 proposed：本文提出的方法在原文里的名字（通常在摘要或引言里，例如 "we propose MANGO"）。
只依据给出的原文，不要用你对其它论文的记忆。
输出 JSON：{"proposed": ["方法名"], "items": [{"i": 序号, "name": "原文写法或 null", "is_proposed": true|false}]}"""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _core(name: str) -> str:
    n = re.sub(r"\(.*?\)|\[.*?\]", "", name or "").strip()
    return re.sub(r"^\+\s*|\s+(w/o|w/|without|with)\b.*$", "", n, flags=re.I).strip()


def _excerpt(text: str, names: list, cap: int = 40_000) -> str:
    """摘要 + 引言（开头 12k）+ 提到这些名字的段落。"""
    head = text[:12_000]
    lines = text[12_000:].split("\n")
    keys = {_core(n).lower() for n in names if len(_core(n)) >= 3}
    keep = [ln for ln in lines if any(k in ln.lower() for k in keys) or re.search(r"\bTable \d", ln)]
    return (head + "\n...\n" + "\n".join(keep))[:cap]


def check(pid: str, kind: str = "daily") -> dict:
    qr = store.load_quickread(pid) or {}
    names = sorted({(r.get("method") or "").strip() for r in qr.get("results") or [] if r.get("method")})
    meta = store.load(pid)
    if not names:
        return {}
    if llm.creds().get("WB_LLM_PROTOCOL") == "fake":
        text = f"{meta.get('title')}\n\n{meta.get('abstract')}"
    else:
        text, _ = fulltext.fetch(meta)
    T = _norm(text)
    found = {n: bool(_norm(_core(n))) and _norm(_core(n)) in T for n in names}
    user = (f"标题：{meta.get('title')}\n\n抽取的方法名（后面注明这个名字是否原样出现在原文里）：\n"
            + "\n".join(f"[{i}] {n}  ——{'原文中出现' if found[n] else '原文中没有原样出现'}" for i, n in enumerate(names))
            + f"\n\n=== 原文（摘要、引言与相关段落）===\n{_excerpt(text, names)}")
    res = llm.chat(SYS, user, task="namecheck", max_tokens=6000, kind=kind)
    out = {"checked_at": now_iso(), "proposed": [p for p in res.get("proposed") or [] if isinstance(p, str)], "names": {}}
    for it in res.get("items") or []:
        try:
            n = names[int(it["i"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        good = it.get("name")
        out["names"][n] = {"name": good.strip() if isinstance(good, str) and good.strip() else None,
                           "is_proposed": bool(it.get("is_proposed")), "in_text": found[n]}
    qr = store.load_quickread(pid) or qr
    qr["method_check"] = out
    store.save_quickread(pid, qr)
    return out


def pending() -> list:
    ms = [m for m in store.all_papers() if m.get("intake", {}).get("state") == "relevant" and (m.get("_qr") or {}).get("results")
          and "method_check" not in (m.get("_qr") or {})]
    ms.sort(key=lambda m: m.get("published") or "", reverse=True)
    return [m["id"] for m in ms]


def fill(pids: list, kind: str = "daily", workers: int = 6, log=print) -> dict:
    ok, failed = 0, []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check, p, kind): p for p in pids}
        for fu in cf.as_completed(futs):
            try:
                fu.result()
                ok += 1
            except (llm.LLMNotConfigured, llm.LLMPaused):
                for f2 in futs:
                    f2.cancel()
                log(f"方法名核对：{ok} 篇后暂停（额度 / 预算）")
                raise
            except Exception as e:  # noqa: BLE001
                failed.append({"id": futs[fu], "error": str(e)[:300]})
    log(f"方法名核对：{ok} 篇，失败 {len(failed)} 篇")
    return {"ok": ok, "failed": failed}


def corrected(qr: dict, raw: str) -> tuple:
    """→ (显示用的名字, 是否本文方法（None = 沿用抽取）, 核对状态 unchecked / ok / renamed / missing)。"""
    c = ((qr.get("method_check") or {}).get("names") or {}).get(raw)
    if not c:
        return raw, None, "unchecked"
    if not c.get("name"):
        return raw, c.get("is_proposed"), "missing"
    return c["name"], c.get("is_proposed"), "renamed" if c["name"] != raw else "ok"
