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
    """{credentials: [{id, status, cooldowns: {model: until}, windows: {id: {...}}}], observed_at, fresh}。
    一个分组可以有多个凭据，GPT-Load 会跳过冷却 / 用完的凭据（2026-10-12 实测），所以判断要按凭据来，见 pool()。
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
    now = time.time()
    out = {"credentials": [], "observed_at": None, "fresh": False}
    for it in (r.get("data") or {}).get("items") or []:
        obs = it.get("observation") or {}
        snap = obs.get("snapshot") or {}
        out["fresh"] = out["fresh"] or obs.get("state") == "fresh"
        out["observed_at"] = max(out["observed_at"] or 0, (obs.get("observed_at_ms") or 0) / 1000) or None
        cred = {"id": it.get("credential_id"), "status": it.get("effective_status"), "cooldowns": {}, "windows": {}}
        for cd in it.get("model_cooldowns") or []:
            u = (cd.get("cooldown_until_ms") or 0) / 1000
            if u > now:
                cred["cooldowns"][cd.get("model")] = u
        for w in snap.get("quota_windows") or []:
            cred["windows"][w.get("id")] = {"label": w.get("label"), "remaining": float(w.get("remaining") or 0),
                                            "reset_at": (w.get("reset_at_ms") or 0) / 1000,
                                            "window_seconds": w.get("window_seconds")}
        out["credentials"].append(cred)
    _cache.update(t=time.time(), v=out)
    return out


def family(model: str) -> str:
    """模型属于哪组额度窗口：gemini-* → gemini；其它（Claude / GPT）→ 3p。"""
    return "gemini" if (model or "").lower().startswith("gemini") else "3p"


def _fam_windows(cred: dict, model: str) -> tuple:
    f = family(model)
    w = cred["windows"]
    return w.get(f"{f}-5h"), w.get(f"{f}-weekly") or w.get(f"{f}-7d")


def pool(snap: dict, model: str, min_5h: float = 1, min_7d: float = 1) -> dict:
    """这个模型现在能用哪些凭据：状态 available、该模型不在冷却、5h / 7d 剩余不低于阈值。
    返回 {usable: [凭据], best_5h, best_7d, next_at: 没有可用凭据时最早恢复的时间, reasons: [不可用原因]}。"""
    now = time.time()
    usable, reasons, next_at = [], [], None
    for c in snap["credentials"]:
        w5, w7 = _fam_windows(c, model)
        waits = []
        if c["status"] != "available":
            reasons.append(f"凭据 {c['id']} 状态 {c['status']}")
            waits.append(now + 1800)
        cd = c["cooldowns"].get(model)
        if cd and cd > now:
            reasons.append(f"凭据 {c['id']} 冷却到 {time.strftime('%H:%M', time.localtime(cd))}")
            waits.append(cd)
        for w, lim in ((w5, min_5h), (w7, min_7d)):
            if w and w["remaining"] < lim:
                reasons.append(f"凭据 {c['id']} {w['label']} 剩 {w['remaining']:.0f}%")
                waits.append(w["reset_at"])
        if waits:
            t = max(waits)
            next_at = t if next_at is None else min(next_at, t)
        else:
            usable.append(c)
    best = lambda i: max((x for x in (_fam_windows(c, model)[i] for c in usable) if x),  # noqa: E731
                         key=lambda x: x["remaining"], default=None)
    return {"usable": usable, "best_5h": best(0), "best_7d": best(1), "next_at": next_at, "reasons": reasons}


def windows_for(snap: dict, model: str) -> tuple:
    """可用凭据里最好的 5h / 7d 窗口（没有可用凭据时看全部）。"""
    p = pool(snap, model)
    if p["usable"]:
        return p["best_5h"], p["best_7d"]
    allw = [_fam_windows(c, model) for c in snap["credentials"]]
    return (max((w[0] for w in allw if w[0]), key=lambda x: x["remaining"], default=None),
            max((w[1] for w in allw if w[1]), key=lambda x: x["remaining"], default=None))


def describe(snap: dict | None, model: str) -> str:
    if not snap:
        return "额度接口不可用（未配置 quota.env 或请求失败）"
    p = pool(snap, model)

    def one(w):
        if not w:
            return "-"
        left = max(0, w["reset_at"] - time.time())
        return f"剩 {w['remaining']:.0f}%（{int(left // 3600)}h{int(left % 3600 // 60)}m 后重置）"
    n = len(snap["credentials"])
    if p["usable"]:
        head = f"{family(model)}：{n} 个凭据可用 {len(p['usable'])} 个，最好的 5h {one(p['best_5h'])}，7d {one(p['best_7d'])}"
    else:
        head = f"{family(model)}：{n} 个凭据都不可用，最早 {time.strftime('%H:%M', time.localtime(p['next_at']))} 恢复"
    return head + (f"；{'；'.join(p['reasons'])}" if p["reasons"] else "")
