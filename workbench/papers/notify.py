"""Telegram 推送（ADR-0016）。凭据只从 telegram.env 读取，不复制、不打印。"""
from __future__ import annotations

import html
import urllib.parse
from pathlib import Path

from .. import config
from . import http



def _creds() -> dict:
    te = config.load().get("telegram_env")
    if not te:
        return {}
    p = Path(te).expanduser()
    env = {}
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def configured() -> bool:
    c = _creds()
    return bool(c.get("TELEGRAM_BOT_TOKEN") and c.get("TELEGRAM_CHAT_ID"))


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def send(text_html: str) -> list:
    """发一条 HTML 消息，超过 4000 字自动按段落拆分。返回 message_id 列表。"""
    c = _creds()
    if not (c.get("TELEGRAM_BOT_TOKEN") and c.get("TELEGRAM_CHAT_ID")):
        raise RuntimeError("Telegram 未配置（telegram.env 缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）")
    parts, cur = [], ""
    for para in text_html.split("\n\n"):
        if len(cur) + len(para) + 2 > 3900 and cur:
            parts.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + para
    if cur:
        parts.append(cur)
    ids = []
    url = f"https://api.telegram.org/bot{c['TELEGRAM_BOT_TOKEN']}/sendMessage"
    for p in parts:
        body = urllib.parse.urlencode({"chat_id": c["TELEGRAM_CHAT_ID"], "text": p, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        try:
            r = http.get(url, data=body, ttl=0, timeout=60, retries=2, cache=False,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        except http.HttpError as e:
            # 不把带 token 的 URL 打出去
            raise RuntimeError(f"Telegram 发送失败：HTTP {e.code}") from None
        ids.append((r.get("result") or {}).get("message_id"))
    return ids
