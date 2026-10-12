#!/usr/bin/env python3
"""Extract text from a paper PDF (local path or URL) into a .txt file.

Usage: python3 extract_pdf.py <pdf_path_or_url> <out_txt>
Tries pypdf (auto-installs with --user if missing). Prints page count and
the location of key sections (Abstract / Introduction / Related Work /
Method / Experiments / References) so the reader can jump around.
"""
import os, re, subprocess, sys, tempfile, urllib.request

def ensure_pypdf():
    try:
        import pypdf  # noqa
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "-q",
                        "--disable-pip-version-check", "pypdf"], check=False)
        import importlib, site
        site.addsitedir(site.getusersitepackages())  # user dir may not have existed at startup
        importlib.invalidate_caches()
    import pypdf
    return pypdf

def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    src, out = sys.argv[1], sys.argv[2]
    if src.startswith("http"):
        if "arxiv.org/abs/" in src:
            src = src.replace("/abs/", "/pdf/")
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            f.write(r.read())
        src = tmp
    pypdf = ensure_pypdf()
    reader = pypdf.PdfReader(src)
    parts = []
    for i, p in enumerate(reader.pages):
        try:
            t = p.extract_text() or ""
        except Exception as e:
            t = f"[page {i+1} extract failed: {e}]"
        parts.append(f"\n\n===== PAGE {i+1} =====\n{t}")
    text = "".join(parts)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    lines = text.splitlines()
    print(f"pages={len(reader.pages)} chars={len(text)} lines={len(lines)} -> {out}")
    pat = re.compile(r"^\s*(\d+(\.\d+)?\s+)?(abstract|introduction|related work|background|preliminar\w*|method\w*|approach|experiment\w*|evaluation|results|ablation\w*|conclusion\w*|references|appendix)\b", re.I)
    for n, l in enumerate(lines, 1):
        if pat.match(l) and len(l) < 80:
            print(f"  line {n}: {l.strip()}")

if __name__ == "__main__":
    main()
