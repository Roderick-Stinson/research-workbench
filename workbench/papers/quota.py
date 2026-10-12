"""真实额度（ADR-0032）：从 GPT-Load 管理端读分组的 5h / 7d 剩余、重置时间、模型冷却。

凭据：$WB_HOME/secrets/quota.env（WB_QUOTA_BASE_URL / WB_QUOTA_AUTH_KEY / WB_QUOTA_GROUP_ID）。
接口返回里有账号邮箱和代理地址：这里只取额度相关字段，原始响应不落盘、不进日志。
"""
from __future__ import annotations

import time

from .. import config
from . import http

_cache: dict = {}


def creds() -> dict:
    p = config.home() / "secrets" / "quota.env"
    env = {}
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def configured() -> bool:
    c = creds()
    return bool(c.get("WB_QUOTA_AUTH_KEY") and c.get("WB_QUOTA_BASE_URL"))


def snapshot(max_age: float = 60) -> dict | None:
    """{status, windows: {id: {label, remaining, used, reset_at, window_seconds}}, cooldowns: {model: until}, observed_at, fresh}。
    取不到返回 None（调用方退回"撞到错误才熔断"）。结果在进程内缓存 max_age 秒。"""
    if not configured():
        return None
    if _cache.get("t") and time.time() - _cache["t"] < max_age:
        return _cache["v"]
    c = creds()
    url = f"{c['WB_QUOTA_BASE_URL'].rstrip('/')}/api/modern/groups/{c.get('WB_QUOTA_GROUP_ID', '5')}/credentials"
    try:
        r = http.get(url, {"page": 1, "page_size": 100}, headers={"Authorization": f"Bearer {c['WB_QUOTA_AUTH_KEY']}"},
                     ttl=0, cache=False, timeout=20, retries=1, retry_status=(500, 502, 503, 504))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(r, dict) or r.get("code") != 0:
        return None
    items = (r.get("data") or {}).get("items") or []
    out = {"status": None, "windows": {}, "cooldowns": {}, "observed_at": None, "fresh": False, "credentials": len(items)}
    for it in items:  # 分组里可能有多个账号：取"最宽松"的那个（任一可用即可用）
        st = it.get("effective_status")
        if out["status"] != "available":
            out["status"] = st
        for cd in it.get("model_cooldowns") or []:
            u = (cd.get("cooldown_until_ms") or 0) / 1000
            if u > time.time():
                out["cooldowns"][cd.get("model")] = min(out["cooldowns"].get(cd.get("model"), u), u)
        obs = it.get("observation") or {}
        snap = obs.get("snapshot") or {}
        out["fresh"] = out["fresh"] or obs.get("state") == "fresh"
        out["observed_at"] = max(out["observed_at"] or 0, (obs.get("observed_at_ms") or 0) / 1000) or None
        for w in snap.get("quota_windows") or []:
            wid = w.get("id")
            cur = out["windows"].get(wid)
            rec = {"label": w.get("label"), "remaining": float(w.get("remaining") or 0), "used": w.get("used"),
                   "reset_at": (w.get("reset_at_ms") or 0) / 1000, "window_seconds": w.get("window_seconds")}
            if cur is None or rec["remaining"] > cur["remaining"]:
                out["windows"][wid] = rec
    _cache.update(t=time.time(), v=out)
    return out


def family(model: str) -> str:
    """模型属于哪组额度窗口：gemini-* → gemini；其它（Claude / GPT）→ 3p。"""
    return "gemini" if (model or "").lower().startswith("gemini") else "3p"


def windows_for(snap: dict, model: str) -> tuple:
    f = family(model)
    w = snap["windows"]
    return w.get(f"{f}-5h"), w.get(f"{f}-weekly") or w.get(f"{f}-7d")


def describe(snap: dict | None, model: str) -> str:
    if not snap:
        return "额度接口不可用（未配置 quota.env 或请求失败）"
    w5, w7 = windows_for(snap, model)

    def one(w):
        if not w:
            return "-"
        left = max(0, w["reset_at"] - time.time())
        return f"剩 {w['remaining']:.0f}%（{int(left // 3600)}h{int(left % 3600 // 60)}m 后重置）"
    cd = snap["cooldowns"].get(model)
    extra = f"；模型冷却到 {time.strftime('%H:%M', time.localtime(cd))}" if cd else ""
    return f"{family(model)} 5h {one(w5)}，7d {one(w7)}；账号 {snap['status']}{extra}"
