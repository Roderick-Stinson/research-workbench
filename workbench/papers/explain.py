"""精读（ADR-0019，P2）：每篇精选论文按 paper-explainer 写 8 步 HTML。

执行者（field.json 的 explainer_engine，ADR-0030）：
- gemini（默认）：批量模型分两次填模板（hero + Step 1–4，Step 5–8），参考文献由工作台按模型给出的原文参考列表统一生成，保证引用闭合；
- codex：工作目录 papers/<pid>/explain/ 里由 codex 按 skill 完整执行（能跑脚本作图，但占订阅额度）。
产出 → 校验（无占位符、引用闭合）→ papers/<pid>/explainer.html。
"""
from __future__ import annotations

import concurrent.futures as cf
import html as _html
import json
import re
import shutil

from ..util import now_iso
from . import codex, fulltext, http, llm, store
from . import field as fieldmod

PROMPT = """你在实验工作台里为一篇论文写精读文档。严格按照 skill 文件 {skill}/SKILL.md 的要求完成（$SKILL_DIR = {skill}）。

输入（都在当前目录，不需要联网）：
- paper.pdf：论文 PDF
- fulltext.txt：全文文本（{basis}），公式和表格比 PDF 抽取更完整，优先读它
- quickread.json：之前的速读（可参考，但以原文为准）

要求：
1. 读完全文（含 References 与附录中的实验设置），按 skill 的 8 步写，正文中文。
2. 模板：{skill}/assets/template.html。**最终文件写到 ./explainer.html**（不是 outputs/ 目录）。
3. 图里的数字只能来自原文表格 / 正文；不确定就不画、不写。引用只能来自原文参考文献。
4. 按 skill 的质检清单自检：不能留 {{{{...}}}} 占位符；所有 #ref-n 引用都有定义。
5. 中间文件放 ./work/，完成后可以删除。
完成后只回复一行：DONE 或 FAILED: 原因。
"""


def check_html(path) -> list:
    s = path.read_text(errors="replace")
    problems = []
    ph = re.findall(r"\{\{[A-Z0-9_]+\}\}", s)
    if ph:
        problems.append(f"残留占位符 {sorted(set(ph))[:5]}")
    cites = set(re.findall(r'href="#(ref-\d+)"', s))
    defs = set(re.findall(r'id="(ref-\d+)"', s))
    if cites - defs:
        problems.append(f"引用没有定义 {sorted(cites - defs)[:5]}")
    if len(s) < 8000:
        problems.append("内容过短")
    return problems


SYS_OUTLINE = """你是严谨的论文精读写作者。下面是精读 skill 的完整要求：

<skill>
{skill}
</skill>

这一轮只做准备工作（不写正文）：hero 的各字段、全文提纲、参考文献列表。只依据原文，不能编。
输出 JSON：{{"PAPER_SHORT_TITLE": "", "VENUE_YEAR": "", "PAPER_FULL_TITLE": "", "AUTHORS": "", "TLDR": "",
 "PROBLEM_ONE_LINE": "", "ROOT_CAUSE_ONE_LINE": "", "DIRECT_FIX_ONE_LINE": "", "IDEA_ONE_LINE": "",
 "outline": {{"P": "", "R": "", "F": "", "I": "", "categories": ["Step 2 的分类及代表工作（写 ref 编号）"],
              "claims": ["C1 …"], "experiments": ["E1 设置 → 证明 C?，关键数字（表号）"], "formulas": ["核心公式及原文编号"]}},
 "refs": [{{"n": 1, "text": "作者, 标题, 会议/年份（照抄原文参考文献）", "url": "arXiv/DOI 链接或空"}}]}}
refs 收录正文里会引用到的参考文献（通常 15–40 条），编号从 1 连续。"""

SYS_STEPS = """你是严谨的论文精读写作者。下面是精读 skill 的完整要求（写法、排版硬性规则、各 Step 的要求、HTML 组件）：

<skill>
{skill}
</skill>

全文提纲（各步必须与之一致，保持 P→R→F→I 一条线）：
<outline>{outline}</outline>
参考文献（引用只能用这些编号，写成 <a class="cite" href="#ref-n">[n]</a>）：
<refs>{refs}</refs>

这一轮写 **Step {steps}** 的 HTML 片段，严格按 skill 对这些 Step 的要求。
- 数字、结论只能来自原文；图用内联 SVG（viewBox，不写死宽度，用 skill 列出的 svg class），数据图注明 Tab./Fig. 编号。
- **直接输出 HTML，不要 JSON、不要代码块**。每一步以单独一行的标记开头：<!--STEP n-->，后面是这一步的内容（不含 section 标签和步骤标题）。
"""


def _refs_html(refs: list) -> str:
    items = []
    for r in sorted(refs, key=lambda x: int(x.get("n", 0))):
        t = _html.escape(r.get("text") or "")
        u = (r.get("url") or "").strip()
        link = f' <a href="{_html.escape(u)}" target="_blank">链接</a>' if u.startswith("http") else ""
        items.append(f'<li id="ref-{int(r["n"])}">{t}{link}</li>')
    return '<ol class="refs">' + "".join(items) + "</ol>"


def explain_gemini(pid: str) -> dict:
    """先出提纲与参考文献，再 8 步并行各写一段（每次调用输出小，避开网关超时），最后由工作台装进模板。"""
    m = store.load(pid)
    d = store.pdir(pid)
    text, basis = fulltext.fetch(m, max_chars=400_000)
    skill = (codex.SKILLS / "paper-explainer" / "SKILL.md").read_text()
    tpl = (codex.SKILLS / "paper-explainer" / "assets" / "template.html").read_text()
    qr = store.load_quickread(pid) or {}
    user = (f"论文：{m.get('title')}\n作者：{', '.join(m.get('authors') or [])}\n链接：{m.get('url')}\n"
            f"会议（如已知）：{m.get('venue') or qr.get('venue_claim') or ''}\n\n=== 全文（{basis}）===\n{text}")
    a = llm.chat(SYS_OUTLINE.format(skill=skill), user, task="explain-outline", max_tokens=12000, temperature=0.2)
    refs = [r for r in a.get("refs") or [] if isinstance(r, dict) and str(r.get("n", "")).isdigit()]
    outline = json.dumps(a.get("outline") or {}, ensure_ascii=False)
    refs_s = json.dumps(refs, ensure_ascii=False)

    groups = [(1, 2, 3), (4, 5, 6), (7, 8)]  # 4 次调用：提纲 + 3 组（每次输出不大，避开网关超时；全文只重复发 3 次）

    def run_group(g):
        txt = llm.chat(SYS_STEPS.format(skill=skill, outline=outline, refs=refs_s, steps="、".join(map(str, g))),
                       user, task=f"explain-s{''.join(map(str, g))}", json_mode=False, max_tokens=24000,
                       temperature=0.3)
        txt = re.sub(r"^```(?:html)?\s*|\s*```\s*$", "", txt.strip())
        parts = re.split(r"<!--\s*STEP\s*(\d)\s*-->", txt)
        got = {int(parts[i]): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
        missing = [n for n in g if not got.get(n)]
        if missing:
            raise ValueError(f"Step {missing} 没有产出")
        return got

    vals = dict(a)
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for got in ex.map(run_group, groups):
            for n, h in got.items():
                vals[f"STEP{n}_CONTENT"] = h
    vals["PAPER_URL"] = m.get("url") or (f"https://arxiv.org/abs/{m['arxiv']}" if m.get("arxiv") else "#")
    vals["REFERENCES"] = _refs_html(refs)
    plain = {"PAPER_SHORT_TITLE", "VENUE_YEAR", "PAPER_FULL_TITLE", "AUTHORS", "PAPER_URL", "TLDR",
             "PROBLEM_ONE_LINE", "ROOT_CAUSE_ONE_LINE", "DIRECT_FIX_ONE_LINE", "IDEA_ONE_LINE"}

    def fill(mt):
        k = mt.group(1)
        v = vals.get(k)
        if v is None:
            return mt.group(0)
        return _html.escape(str(v)) if k in plain else str(v)

    out_html = re.sub(r"\{\{([A-Z0-9_]+)\}\}", fill, tpl)
    out = d / "explainer.gemini.html"
    out.write_text(out_html)
    rec = {"at": now_iso(), "engine": "gemini", "basis": basis, "ok": False}
    probs = check_html(out)
    rec["problems"] = probs
    if not any(p.startswith(("残留占位符", "内容过短")) for p in probs):
        shutil.copy2(out, d / "explainer.html")
        rec["ok"] = True
    else:
        rec["error"] = "; ".join(probs)
    m = store.load(pid)
    m["explainer"] = rec
    store.save(m)
    return rec


def engine() -> str:
    return (fieldmod.field().get("explainer_engine") or "gemini").lower()


def explain_one(pid: str) -> dict:
    if engine() == "gemini":
        return explain_gemini(pid)
    return explain_codex(pid)


def explain_codex(pid: str) -> dict:
    m = store.load(pid)
    d = store.pdir(pid)
    w = d / "explain"
    if w.exists():
        shutil.rmtree(w)
    w.mkdir(parents=True)
    text, basis = fulltext.fetch(m, max_chars=10 ** 7)
    (w / "fulltext.txt").write_text(text)
    pdf = d / "paper.pdf"
    if not pdf.exists() and (m.get("pdf_url") or m.get("arxiv")):
        try:
            pdf.write_bytes(http.get_bytes(m.get("pdf_url") or f"https://arxiv.org/pdf/{m['arxiv']}"))
        except Exception:
            pass
    if pdf.exists():
        shutil.copy2(pdf, w / "paper.pdf")
    (w / "quickread.json").write_text(json.dumps(store.load_quickread(pid) or {}, ensure_ascii=False, indent=2))
    prompt = PROMPT.format(skill=codex.SKILLS / "paper-explainer", basis=basis)
    res = codex.run(f"explain:{pid}", w, prompt)
    out = w / "explainer.html"
    rec = {"at": now_iso(), "seconds": res["seconds"], "ok": False}
    if res["ok"] and out.exists():
        probs = check_html(out)
        rec["problems"] = probs
        if not any(p.startswith(("残留占位符", "内容过短")) for p in probs):
            shutil.copy2(out, d / "explainer.html")
            rec["ok"] = True
    else:
        rec["error"] = res["error"] or "没有产出 explainer.html"
    m = store.load(pid)
    m["explainer"] = rec
    store.save(m)
    return rec


def run(pids: list, log=print, retries: int = 1) -> dict:
    todo = [p for p in pids if not (store.pdir(p) / "explainer.html").exists()]
    ok, failed = [], []
    with cf.ThreadPoolExecutor(max_workers=3 if engine() == "gemini" else codex.cfg()["parallel"]) as ex:
        futs = {ex.submit(explain_one, p): p for p in todo}
        for fu in cf.as_completed(futs):
            p = futs[fu]
            try:
                rec = fu.result()
            except llm.LLMPaused:
                for f2 in futs:
                    f2.cancel()
                log(f"精读完成 {len(ok)} 篇后暂停（额度）")
                raise
            except Exception as e:  # noqa: BLE001
                rec = {"ok": False, "error": str(e)[:300]}
            if not rec["ok"] and retries:
                try:
                    rec = explain_one(p)
                except llm.LLMPaused:
                    raise
                except Exception as e:  # noqa: BLE001
                    rec = {"ok": False, "error": str(e)[:300]}
            (ok if rec["ok"] else failed).append({"pid": p, **rec})
    log(f"精读完成 {len(ok)} 篇，失败 {len(failed)} 篇")
    return {"ok": len(ok), "failed": [{"pid": f["pid"], "error": f.get("error") or f.get("problems")} for f in failed]}
