"""外部 HTTP：本地缓存 + 按站点限速 + 429/5xx 退避重试。

cron 里没有代理环境变量，所以代理从 config.json 的 "proxy" 读（wb install 时写入）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config

UA = "workbench/0.2 (research paper tracker; mailto:{mail})"
# 站点最小请求间隔（秒）
MIN_INTERVAL = {
    "export.arxiv.org": 5.0, "arxiv.org": 1.0, "api.semanticscholar.org": 3.5, "api.openalex.org": 0.2,
    "api.github.com": 6.5, "hn.algolia.com": 0.5, "huggingface.co": 0.5, "dblp.org": 1.5,
}
_last: dict = {}
_lock = threading.Lock()


class HttpError(Exception):
    def __init__(self, code, url, body=""):
        super().__init__(f"HTTP {code} {url} {body[:200]}")
        self.code = code


def _opener():
    proxy = config.load().get("proxy") or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _throttle(host: str):
    gap = MIN_INTERVAL.get(host, 0.3)
    with _lock:
        now = time.time()
        wait = _last.get(host, 0) + gap - now
        _last[host] = max(now, _last.get(host, 0) + gap)
    if wait > 0:
        time.sleep(wait)


def _cache_path(key: str):
    h = hashlib.sha1(key.encode()).hexdigest()
    return config.home() / "cache" / "http" / h[:2] / f"{h}.json"


def get(url: str, params: dict | None = None, headers: dict | None = None, ttl: float = 6 * 3600,
        as_json: bool = True, timeout: float = 60, retries: int = 4, data: bytes | None = None, cache: bool = True,
        retry_status: tuple = (429, 500, 502, 503, 504)):
    """GET（或带 data 的 POST）。ttl 秒内命中缓存直接返回；ttl=0 不读缓存。
    cache=False：既不读也不写缓存（URL 或内容带凭据时必须用，例如 Telegram）。"""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    key = url + (hashlib.sha1(data).hexdigest() if data else "")
    cp = _cache_path(key)
    if cache and ttl and cp.exists() and time.time() - cp.stat().st_mtime < ttl:
        try:
            body = json.loads(cp.read_text())["body"]
            return json.loads(body) if as_json else body
        except (ValueError, KeyError, OSError):
            pass
    host = urllib.parse.urlparse(url).hostname or ""
    mail = config.load().get("contact_email") or "workbench@example.com"
    h = {"User-Agent": UA.format(mail=mail), "Accept": "application/json" if as_json else "*/*"}
    h.update(headers or {})
    op = _opener()
    delay = 5.0
    last_err = None
    for attempt in range(retries + 1):
        _throttle(host)
        try:
            req = urllib.request.Request(url, headers=h, data=data)
            with op.open(req, timeout=timeout) as r:
                raw = r.read()
            body = raw.decode("utf-8", "replace")
            if cache:
                cp.parent.mkdir(parents=True, exist_ok=True)
                tmp = cp.with_suffix(".tmp")
                tmp.write_text(json.dumps({"url": url, "t": time.time(), "body": body}))
                os.replace(tmp, cp)
            return json.loads(body) if as_json else body
        except urllib.error.HTTPError as e:
            last_err = HttpError(e.code, url if cache else "(url hidden)",
                                 e.read().decode("utf-8", "replace") if e.fp else "")
            if e.code in retry_status and attempt < retries:
                ra = e.headers.get("Retry-After") if e.headers else None
                # 429：被限流，等得更久（arXiv 要求低频访问）
                wait = float(ra) if ra and ra.isdigit() else (delay * 6 if e.code == 429 else delay)
                time.sleep(min(wait, 600))
                delay = min(delay * 2, 120)
                continue
            raise last_err
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise
    raise last_err  # pragma: no cover


def get_bytes(url: str, timeout: float = 120, max_bytes: int = 60 << 20) -> bytes:
    """下载二进制（PDF）。不缓存到 http cache（由调用方存到论文目录）。"""
    host = urllib.parse.urlparse(url).hostname or ""
    _throttle(host)
    mail = config.load().get("contact_email") or "workbench@example.com"
    req = urllib.request.Request(url, headers={"User-Agent": UA.format(mail=mail)})
    with _opener().open(req, timeout=timeout) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"文件超过 {max_bytes >> 20}MB：{url}")
    return data
