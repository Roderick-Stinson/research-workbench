"""由 docs/MANUAL.md 生成单文件 HTML 说明书 docs/manual.html（只用标准库，只支持说明书里用到的 Markdown 子集）。

  python3 scripts/build_manual.py           # 重新生成 docs/manual.html
  python3 scripts/build_manual.py --check   # CI 用：HTML 和 MANUAL.md 不一致就失败

每次改动都要同步改 docs/MANUAL.md 并重新生成（MAINTAINING.md）。样式在 docs/manual.css。
"""
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, OUT, CSS_FILE = ROOT / "docs" / "MANUAL.md", ROOT / "docs" / "manual.html", ROOT / "docs" / "manual.css"
GH = "https://github.com/Roderick-Stinson/research-workbench/blob/main/"
text = SRC.read_text(encoding="utf-8")
lines = text.split("\n")
_ver = re.search(r"适用版本：(v[\d.]+)（([\d-]+)）", text)
VERSION, VDATE = (_ver.group(1), _ver.group(2)) if _ver else ("", "")


def _url(url: str) -> str:
    if url.startswith("../"):
        return GH + url[3:]
    if not url.startswith(("http", "#")):
        return GH + "docs/" + url
    return url


def inline(s: str) -> str:
    links = []

    def keep(m):
        url = _url(m.group(2))
        ext = ' target="_blank" rel="noopener"' if url.startswith("http") else ""
        links.append(f'<a href="{html.escape(url)}"{ext}>{inline(m.group(1))}</a>')
        return f"\x00{len(links) - 1}\x00"

    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", keep, s)
    return re.sub("\x00(\\d+)\x00", lambda m: links[int(m.group(1))], _inline(s))


def _inline(s: str) -> str:
    parts = re.split(r"(`[^`]+`)", s)
    res = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            res.append(f"<code>{html.escape(p[1:-1])}</code>")
            continue
        p = html.escape(p, quote=False)

        res.append(p)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", "".join(res))


def slug(t: str) -> str:
    m2 = re.match(r"(\d+)\.(\d+)", t)
    if m2:
        return f"s{m2.group(1)}-{m2.group(2)}"
    m = re.match(r"(\d+)\.", t)
    if m:
        return f"s{m.group(1)}"
    if "之前" in t:
        return "ver-pre"
    v = re.match(r"(v[\d.]+|未发布)", t)
    if v:
        return "ver-" + v.group(1).replace(".", "-").replace("未发布", "unreleased")
    return "h-" + re.sub(r"[^\w]+", "-", t).strip("-")[:40]


body, toc = ['<section class="lead" id="top">'], []
i = 0
in_versions = False
open_card = False
title = ""


def close_card():
    global open_card
    if open_card:
        body.append("</article>")
        open_card = False


# 跳过手写目录
skip = False
while i < len(lines):
    ln = lines[i]
    if ln.startswith("## 目录"):
        skip = True
        i += 1
        continue
    if skip:
        if ln.startswith("## ") or ln.strip() == "---":
            skip = False
        else:
            i += 1
            continue
    if ln.startswith("# "):
        title = ln[2:].strip()
        i += 1
        continue
    if ln.strip() == "---":
        i += 1
        continue
    if ln.startswith("## "):
        close_card()
        t = ln[3:].strip()
        sid = slug(t)
        body.append("</section>")
        in_versions = t.startswith("21.")
        num = re.match(r"(\d+)\.\s*(.*)", t)
        label = num.group(2) if num else t
        toc.append((sid, num.group(1) if num else "", label, []))
        hnum = f'<span class="num">{num.group(1)}</span>' if num else ""
        body.append(f'<section id="{sid}"{" class=versions" if in_versions else ""}><h2>{hnum}{inline(label)}</h2>')
        i += 1
        continue
    if ln.startswith("### "):
        close_card()
        t = ln[4:].strip()
        sid = slug(t)
        if in_versions:
            m = re.match(r"(v[\d.]+|未发布|v0\.1\.0 之前)\s*(（[^）]*）)?[：:]?\s*(.*)", t)
            tag, date, rest = (m.group(1), (m.group(2) or "").strip("（）"), m.group(3)) if m else (t, "", "")
            toc[-1][3].append((sid, tag))
            cls = " unreleased" if tag == "未发布" else (" pre" if "之前" in tag else "")
            body.append(f'<article class="card{cls}" id="{sid}"><header><span class="tag">{html.escape(tag)}</span>'
                        f'<span class="date">{html.escape(date)}</span><h3>{inline(rest)}</h3></header>')
            open_card = True
        else:
            body.append(f'<h3 id="{sid}">{inline(t)}</h3>')
        i += 1
        continue
    if ln.startswith("```"):
        lang = ln[3:].strip()
        buf = []
        i += 1
        while i < len(lines) and not lines[i].startswith("```"):
            buf.append(lines[i])
            i += 1
        i += 1
        body.append(f'<pre class="code{" diagram" if not lang or lang == "text" else ""}"><code>{html.escape(chr(10).join(buf))}</code></pre>')
        continue
    if ln.startswith(">"):
        buf = []
        while i < len(lines) and lines[i].startswith(">"):
            buf.append(lines[i].lstrip(">").strip())
            i += 1
        body.append("<blockquote>" + "<br>".join(inline(b) for b in buf if b) + "</blockquote>")
        continue
    if ln.startswith("|"):
        rows = []
        while i < len(lines) and lines[i].startswith("|"):
            rows.append(lines[i])
            i += 1
        cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
        head, data = cells[0], [c for c in cells[2:]]
        t = ['<div class="tbl"><table><thead><tr>' + "".join(f"<th>{inline(h)}</th>" for h in head) + "</tr></thead><tbody>"]
        for r in data:
            cs = [inline(c) for c in r]
            if in_versions and re.fullmatch(r"v[\d.]+|未发布", r[0]):
                cs[0] = f'<a href="#{slug(r[0])}"><code>{r[0]}</code></a>'
            t.append("<tr>" + "".join(f"<td>{c}</td>" for c in cs) + "</tr>")
        t.append("</tbody></table></div>")
        body.append("".join(t))
        continue
    if re.match(r"^\s*(- |\d+\. )", ln):
        # 列表（支持按缩进嵌套）
        stack = []  # (indent, tag)
        out_l = []
        while i < len(lines) and (re.match(r"^\s*(- |\d+\. )", lines[i]) or (lines[i].startswith("  ") and lines[i].strip() and stack)):
            l2 = lines[i]
            m = re.match(r"^(\s*)(- |\d+\. )(.*)", l2)
            if not m:  # 续行
                out_l[-1] = out_l[-1] + " " + inline(l2.strip())
                i += 1
                continue
            ind, mark, text = len(m.group(1)), m.group(2), m.group(3)
            tag = "ol" if mark[0].isdigit() else "ul"
            if not stack or ind > stack[-1][0]:
                out_l.append(f"<{tag}>")
                stack.append((ind, tag))
            else:
                while stack and ind < stack[-1][0]:
                    out_l.append(f"</li></{stack.pop()[1]}>")
                out_l.append("</li>")
            out_l.append(f"<li>{inline(text)}")
            i += 1
        while stack:
            out_l.append(f"</li></{stack.pop()[1]}>")
        body.append("".join(out_l))
        continue
    if not ln.strip():
        i += 1
        continue
    buf = []
    while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||```|>|\s*- |\s*\d+\. |---)", lines[i]):
        buf.append(lines[i].strip())
        i += 1
    p = inline(" ".join(buf))
    p = re.sub(r"^<strong>(起因|改了什么|现在的效果|同时修了三个问题|同时修了)(（[^）]*）)?：</strong>",
               lambda m: f'<strong class="lbl lbl-{["起因", "改了什么", "现在的效果"].index(m.group(1)) if m.group(1) in ["起因", "改了什么", "现在的效果"] else 3}">{m.group(1)}{m.group(2) or ""}</strong>', p)
    body.append(f"<p>{p}</p>")
close_card()
body.append("</section>")

nav = []
for sid, num, label, subs in toc:
    nav.append(f'<li><a href="#{sid}"><span class="n">{num}</span>{html.escape(re.sub(r"[*`]", "", label))}</a>')
    if subs:
        nav.append('<ul class="sub">' + "".join(f'<li><a href="#{s}">{html.escape(t)}</a></li>' for s, t in subs) + "</ul>")
    nav.append("</li>")

CSS = CSS_FILE.read_text(encoding="utf-8")
JS = """
const links=[...document.querySelectorAll('nav.toc > ol > li > a')];
const secs=links.map(a=>document.querySelector(a.getAttribute('href')));
const io=new IntersectionObserver(es=>{es.forEach(e=>{if(e.isIntersecting){links.forEach(a=>a.classList.toggle('on',a.getAttribute('href')==='#'+e.target.id));}})},{rootMargin:'-20% 0px -70% 0px'});
secs.forEach(s=>s&&io.observe(s));
document.querySelectorAll('nav.toc a').forEach(a=>a.addEventListener('click',()=>{const d=document.querySelector('details.mtoc');if(d)d.open=false;}));
const tb=document.getElementById('theme');
function cur(){return document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');}
function setT(t){document.documentElement.dataset.theme=t;try{localStorage.setItem('wb-manual-theme',t)}catch(e){};tb.textContent=t==='dark'?'☀︎':'☾';}
try{const s=localStorage.getItem('wb-manual-theme');if(s)document.documentElement.dataset.theme=s;}catch(e){}
tb.textContent=cur()==='dark'?'☀︎':'☾';
tb.addEventListener('click',()=>setT(cur()==='dark'?'light':'dark'));
"""
toc_html = '<ol>' + "".join(nav) + '</ol>'
doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>研究工作台说明书</title>
<style>{CSS}</style>
</head>
<body>
<header class="top">
  <div class="brand">研究工作台 · 使用说明书</div>
  <div class="meta"><span class="pill">{VERSION}</span><span>{VDATE}</span>
  <a href="https://github.com/Roderick-Stinson/research-workbench" target="_blank" rel="noopener">GitHub</a>
  <button id="theme" aria-label="切换深浅色"></button></div>
</header>
<div class="layout">
<aside><nav class="toc desk">{toc_html}</nav></aside>
<main>
<details class="mtoc"><summary>目录</summary><nav class="toc">{toc_html}</nav></details>
{chr(10).join(body)}
<footer>由 <code>docs/MANUAL.md</code> 生成 · 术语见 <a href="{GH}CONTEXT.md" target="_blank" rel="noopener">CONTEXT.md</a> · 设计决策见 <a href="{GH}docs/adr" target="_blank" rel="noopener">docs/adr</a></footer>
</main>
</div>
<script>{JS}</script>
</body>
</html>"""
if "--check" in sys.argv:
    if not OUT.exists() or OUT.read_text(encoding="utf-8") != doc:
        print("docs/manual.html 和 docs/MANUAL.md 不一致：运行 python3 scripts/build_manual.py 重新生成后提交")
        sys.exit(1)
    print("docs/manual.html 是最新的")
else:
    OUT.write_text(doc, encoding="utf-8")
    print(f"已生成 {OUT.relative_to(ROOT)}：{len(toc)} 节，{len(doc) // 1024} KB")
