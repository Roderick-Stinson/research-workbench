"""全文：优先 arXiv HTML（表格保留得好），没有就下 PDF 用 pdftotext 抽；都失败退回摘要。"""
from __future__ import annotations

import re
import subprocess
import tempfile
from html.parser import HTMLParser

from . import http, store

SKIP_TAGS = {"script", "style", "nav", "header", "footer", "button", "svg", "math"}
BLOCK_TAGS = {"p", "div", "section", "h1", "h2", "h3", "h4", "h5", "li", "tr", "table", "figcaption", "caption", "br",
              "article", "blockquote", "pre"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag == "math":
            pass
        if tag in BLOCK_TAGS:
            self.out.append("\n")
        if tag in ("td", "th"):
            self.out.append(" | ")
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.out.append(f"[{alt}]")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        if tag in BLOCK_TAGS:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)

    def text(self):
        t = "".join(self.out)
        t = re.sub(r"[ \t\xa0]+", " ", t)
        t = re.sub(r"\n\s*\n+", "\n\n", t)
        return t.strip()


def html_to_text(html: str) -> str:
    # MathML 的 annotation 里有 LaTeX，保留 alttext 更可读
    html = re.sub(r'<math[^>]*alttext="([^"]*)"[^>]*>.*?</math>', lambda m: f" ${m.group(1)}$ ", html, flags=re.S)
    p = _Text()
    p.feed(html)
    return p.text()


def fetch(meta: dict, max_chars: int = 180_000) -> tuple:
    """返回 (text, basis)。basis: arxiv-html / pdf / abstract。结果缓存到 fulltext.txt。"""
    d = store.pdir(meta["id"])
    cache = d / "fulltext.txt"
    if cache.exists() and cache.stat().st_size > 2000:
        text = cache.read_text(errors="replace")
        basis = (d / "fulltext.basis").read_text().strip() if (d / "fulltext.basis").exists() else "cached"
        return text[:max_chars], basis
    text, basis = "", "abstract"
    if meta.get("arxiv"):
        try:
            html = http.get(f"https://arxiv.org/html/{meta['arxiv']}", as_json=False, ttl=30 * 86400, timeout=120, retries=1)
            if "ltx_document" in html or "ltx_page_main" in html:
                text, basis = html_to_text(html), "arxiv-html"
        except Exception:
            pass
    pdf_url = meta.get("pdf_url") or (f"https://arxiv.org/pdf/{meta['arxiv']}" if meta.get("arxiv") else None)
    if len(text) < 5000 and pdf_url:
        try:
            pdf = http.get_bytes(pdf_url)
            pdf_path = d / "paper.pdf"
            d.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(pdf)
            with tempfile.NamedTemporaryFile(suffix=".txt") as tf:
                subprocess.run(["pdftotext", "-layout", str(pdf_path), tf.name], check=True, timeout=120,
                               capture_output=True)
                t2 = open(tf.name, errors="replace").read()
            if len(t2) > len(text):
                text, basis = t2, "pdf"
        except Exception:
            pass
    if len(text) < 2000:
        text = f"{meta.get('title', '')}\n\n{meta.get('abstract', '')}"
        basis = "abstract"
    else:
        d.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
        (d / "fulltext.basis").write_text(basis)
    return text[:max_chars], basis
