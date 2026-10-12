"""核实论文的代码是否真的开源了（ADR-0035）。

论文 / HF / arXiv 评论里给出代码链接，不等于代码放出来了：常见的是空仓库、只有 README（"code coming soon"）、链接 404。
对每个链接：
- GitHub：查仓库是否存在、是否为空、根目录（或链接指向的子目录）里有没有代码文件；
- 其它站点（HF、GitLab、项目主页）：只查能否打开，标"可访问（未核实内容）"。
结果存在论文目录的 code_check.json，结果表和论文页据此标注。已开源的每 30 天复查，其余每 7 天复查（可能后来才放出）。

GitHub API 优先用本机已登录的 gh（每小时 5000 次），没有 gh 时匿名访问（每小时 60 次，用完就停，下次再补）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse

from ..util import now_iso, read_json, write_json
from . import http, store

STATUS = {
    "open": "已开源",
    "readme_only": "只有 README（代码未放出）",
    "empty": "空仓库",
    "dead": "链接失效",
    "reachable": "可访问（未核实内容）",
    "anonymous": "匿名审稿仓库（未公开）",
    "none": "没有代码链接",
    "unknown": "暂时无法核实",
}
CODE_EXT = re.compile(r"\.(py|ipynb|js|ts|go|rs|java|cpp|cc|c|h|sh|jl|scala|rb|r)$", re.I)
CODE_DIRS = {"src", "lib", "scripts", "code", "agents", "workflows", "examples", "experiments", "train", "eval", "evaluation"}
NON_CODE = re.compile(r"^(readme|license|licence|\.gitignore|\.gitattributes|\.github|docs?|assets?|images?|imgs?|figures?|figs?|static|"
                      r"citation|cff|.*\.(md|txt|png|jpe?g|gif|svg|pdf|bib))", re.I)
RECHECK_DAYS = {"open": 30, "reachable": 30}


def path(pid: str):
    return store.pdir(pid) / "code_check.json"


def load(pid: str) -> dict | None:
    return read_json(path(pid))


def _gh_bin():
    return shutil.which("gh") or next((p for p in (os.path.expanduser("~/.local/bin/gh"), "/usr/bin/gh", "/usr/local/bin/gh")
                                       if os.path.exists(p)), None)


class _RateLimited(Exception):
    pass


def _gh(endpoint: str, _retry: int = 1):
    """GitHub API GET：返回 (status, json)。status 404 表示不存在。"""
    gh = _gh_bin()
    if gh:
        r = subprocess.run([gh, "api", "-i", endpoint], capture_output=True, text=True, timeout=60)
        out = r.stdout
        m = re.match(r"HTTP/[\d.]+ (\d+)", out)
        code = int(m.group(1)) if m else (404 if "Not Found" in (r.stderr + out) else 0)
        body = out.split("\r\n\r\n", 1)[-1] if "\r\n\r\n" in out else out.split("\n\n", 1)[-1]
        if code == 403 and "rate limit" in body.lower():
            raise _RateLimited()
        if code == 0 and _retry:  # 偶发的空输出（网络抖动）：重试一次
            time.sleep(3)
            return _gh(endpoint, _retry - 1)
        try:
            return code, json.loads(body) if body.strip() else None
        except ValueError:
            return code, None
    try:
        return 200, http.get(f"https://api.github.com/{endpoint}", ttl=0, cache=False, timeout=30, retries=1,
                             retry_status=(500, 502, 503, 504))
    except http.HttpError as e:
        if e.code == 403:
            raise _RateLimited() from None
        return e.code, None


def _gh_target(url: str):
    """github.com/owner/repo[/tree/branch/sub/path] → (owner, repo, branch, subpath)。"""
    u = urllib.parse.urlparse(url)
    if (u.hostname or "").lower() not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    branch = sub = None
    if len(parts) >= 4 and parts[2] in ("tree", "blob"):
        branch, sub = parts[3], "/".join(parts[4:]) or None
    return owner, repo, branch, sub


def check_url(url: str, _depth: int = 0) -> dict:
    t = _gh_target(url)
    if not t:
        if "anonymous.4open.science" in url:
            return {"status": "anonymous", "detail": "匿名审稿仓库，录用后才会公开"}
        try:
            html = http.get(url, as_json=False, ttl=0, cache=False, timeout=30, retries=1)
        except http.HttpError as e:
            return {"status": "dead" if e.code in (404, 410) else "unknown", "detail": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            return {"status": "unknown", "detail": str(e)[:120]}
        # 项目主页：顺着页面上的 GitHub 仓库链接去核实
        if not _depth:
            repos = list(dict.fromkeys(m.group(0).rstrip(".,;)\"'") for m in
                                       re.finditer(r"https?://github\.com/[\w.-]+/[\w.-]+", html or "")))
            repos = [r for r in repos if not re.search(r"github\.com/[\w.-]+/[\w.-]+\.github\.io$", r)]
            if repos:
                r = check_url(repos[0], _depth + 1)
                return dict(r, via=url, url_checked=repos[0])
        return {"status": "reachable"}
    owner, repo, branch, sub = t
    code, info = _gh(f"repos/{owner}/{repo}")
    if code == 404:
        return {"status": "dead", "detail": "仓库不存在或已设为私有"}
    if code != 200 or not isinstance(info, dict):
        return {"status": "unknown", "detail": f"GitHub API {code}"}
    meta = {"repo": f"{owner}/{repo}", "stars": info.get("stargazers_count"), "pushed_at": info.get("pushed_at"),
            "archived": info.get("archived"), "default_branch": info.get("default_branch")}
    if not info.get("size"):
        return dict(meta, status="empty")
    ep = f"repos/{owner}/{repo}/contents/{urllib.parse.quote(sub)}" if sub else f"repos/{owner}/{repo}/contents"
    if branch:
        ep += f"?ref={urllib.parse.quote(branch)}"
    code, items = _gh(ep)
    if code == 404 and sub:
        return dict(meta, status="dead", detail=f"子目录 {sub} 不存在")
    if code != 200 or not isinstance(items, list):
        return dict(meta, status="unknown", detail=f"GitHub API {code}")
    names = [(i.get("name") or "", i.get("type")) for i in items]
    has_code = any(CODE_EXT.search(n) for n, _ in names) or any(t == "dir" and n.lower() in CODE_DIRS for n, t in names) or \
        any(t == "dir" and not NON_CODE.match(n) for n, t in names)
    only_docs = all(NON_CODE.match(n) for n, _ in names)
    meta["files"] = [n for n, _ in names][:30]
    if has_code and not only_docs:
        return dict(meta, status="open")
    return dict(meta, status="readme_only")


def candidates(pid: str) -> list:
    """候选链接：论文正文 / arXiv 评论里给的在前，HF 关联的在后。"""
    m = store.load(pid) or {}
    qr = store.load_quickread(pid) or {}
    urls = []
    for u in [qr.get("code_url")] + list(m.get("code_urls") or []):
        if u and isinstance(u, str) and u.startswith("http"):
            u = u.rstrip(".,;)")
            if u not in urls:
                urls.append(u)
    return urls


def check(pid: str) -> dict:
    urls = candidates(pid)
    res = {"checked_at": now_iso(), "ts": time.time(), "links": []}
    if not urls:
        res["status"] = "none"
    for u in urls[:3]:
        r = dict(check_url(u), url=u)
        res["links"].append(r)
        if r["status"] == "open":
            break
    if urls:
        order = ["open", "reachable", "anonymous", "readme_only", "empty", "unknown", "dead"]
        best = min(res["links"], key=lambda r: order.index(r["status"]))
        res.update(status=best["status"], url=best.get("url_checked") or best["url"], repo=best.get("repo"), stars=best.get("stars"))
    write_json(path(pid), res)
    return res


def due(pid: str) -> bool:
    c = load(pid)
    if not c:
        return True
    if c.get("status") == "none" and candidates(pid):
        return True  # 后来有了链接
    return time.time() - (c.get("ts") or 0) > RECHECK_DAYS.get(c.get("status"), 7) * 86400


def run(pids: list | None = None, limit: int = 400, log=print) -> dict:
    """核实论文库里（有结果抽取的、精选过的优先）论文的代码。"""
    if pids is None:
        ms = [m for m in store.all_papers() if m.get("intake", {}).get("state") == "relevant"]
        ms.sort(key=lambda m: (not (m.get("_qr") or {}).get("results"), m.get("published") or ""), reverse=False)
        pids = [m["id"] for m in ms]
    todo = [p for p in pids if due(p)][:limit]
    stats = {}
    for i, p in enumerate(todo):
        try:
            st = check(p)["status"]
        except _RateLimited:
            log(f"代码核实：GitHub 限流，已核实 {i} 篇，剩下的下次再补")
            break
        except Exception as e:  # noqa: BLE001
            st = "unknown"
            log(f"代码核实 {p} 出错：{e}")
        stats[st] = stats.get(st, 0) + 1
    log(f"代码核实：{sum(stats.values())} 篇 {stats}")
    return stats
