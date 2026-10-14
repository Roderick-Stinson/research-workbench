"""批量任务模型（ADR-0028）：凭据在 $WB_HOME/secrets/llm.env，支持 OpenAI 兼容接口与 Google Gemini 原生接口。

每次调用记一行用量到 $WB_HOME/reports/llm_usage.jsonl（时间、任务、模型、token、耗时、是否成功），方便看花了多少。
"""
from __future__ import annotations

import json
import os
import re
import time

from .. import config
from ..util import now_iso
from . import http


class LLMNotConfigured(RuntimeError):
    pass


class LLMPaused(RuntimeError):
    """额度用完或预算到顶：在 until 之前不再发任何请求（ADR-0031）。"""

    def __init__(self, reason: str, until: float):
        super().__init__(f"{reason}，预计 {_fmt(until)} 后恢复")
        self.reason, self.until = reason, until


def _fmt(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


# ---------------- 额度：熔断 + 预算（ADR-0031） ----------------

QUOTA_RX = re.compile(r"quota|rate.?limit|usage.?limit|exceed|insufficient|too many requests|额度|余额|限额|超出|上限", re.I)
DEFAULT_BUDGET = {
    # 阈值按 GPT-Load 管理端给出的真实剩余百分比判断（ADR-0032），不再用 token 估算
    "backfill_min_5h_remaining": 40,   # 5h 剩余低于 40% 时回填停下，留给每日流程
    "backfill_min_7d_remaining": 35,   # 7d 剩余低于 35% 时回填停下
    "daily_min_remaining": 1,          # 每日流程：剩余低于 1% 就当作用完
    "probe_minutes": 30,               # 额度接口不可用、又撞到限额时，多久试探一次
}


def budget_cfg() -> dict:
    b = dict(DEFAULT_BUDGET)
    b.update(config.load().get("llm_budget") or {})
    return b


def _block_path():
    return config.home() / "reports" / "llm_block.json"


def block_state() -> dict | None:
    try:
        st = json.loads(_block_path().read_text())
    except (OSError, ValueError):
        return None
    return st if st.get("until", 0) > time.time() else None


def _set_block(reason: str, until: float, detail: str = ""):
    p = _block_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    from . import quota
    snap = quota.snapshot(max_age=0)
    st = {"reason": reason, "until": until, "until_text": _fmt(until), "since": now_iso(), "detail": detail[:500],
          "quota": [{"id": c["id"], "status": c["status"], "cooldowns": c["cooldowns"],
                     "windows": {k: {"remaining": v["remaining"], "reset_at": v["reset_at"]} for k, v in c["windows"].items()}}
                    for c in (snap or {}).get("credentials", [])]}
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    # 记一笔撞到限额的时刻与当时的额度快照
    with open(config.home() / "reports" / "llm_limits.jsonl", "a") as f:
        f.write(json.dumps(st, ensure_ascii=False) + "\n")


def _clear_block():
    try:
        _block_path().unlink()
    except OSError:
        pass


_usage_cache: dict = {}


def usage_windows() -> dict:
    """本工作台在最近 5h / 7d 里用掉的 token（入 + 出），来自 llm_usage.jsonl。"""
    p = config.home() / "reports" / "llm_usage.jsonl"
    try:
        mt = p.stat().st_mtime_ns
    except OSError:
        return {"5h": 0, "7d": 0}
    if _usage_cache.get("mt") == mt:
        return _usage_cache["v"]
    now = time.time()
    u5 = u7 = 0
    from ..util import parse_iso
    with open(p) as f:
        for line in f:
            try:
                r = json.loads(line)
                t = parse_iso(r["ts"])
            except (ValueError, KeyError):
                continue
            n = (r.get("in") or 0) + (r.get("out") or 0)
            if now - t <= 7 * 86400:
                u7 += n
                if now - t <= 5 * 3600:
                    u5 += n
    v = {"5h": u5, "7d": u7}
    _usage_cache.update(mt=mt, v=v)
    return v


def check_budget(kind: str, model: str | None = None) -> None:
    """发请求之前：① 熔断中直接停；② 读 GPT-Load 的真实额度：用完 / 冷却 / 回填阈值不够就停到重置时间。"""
    st = block_state()
    if st:
        raise LLMPaused(st["reason"], st["until"])
    from . import quota
    model = model or creds().get("WB_LLM_MODEL") or ""
    snap = quota.snapshot()
    if not snap:
        return  # 额度接口不可用：只靠"撞到限额错误就熔断"
    b = budget_cfg()
    p = quota.pool(snap, model, b["daily_min_remaining"], b["daily_min_remaining"])
    if not p["usable"]:
        raise LLMPaused(f"{model} 没有可用凭据（{'；'.join(p['reasons'])}）", p["next_at"] or time.time() + 1800)
    if kind == "backfill":
        pb = quota.pool(snap, model, b["backfill_min_5h_remaining"], b["backfill_min_7d_remaining"])
        if not pb["usable"]:
            raise LLMPaused(f"回填暂停：没有凭据的剩余额度高于回填阈值（{'；'.join(pb['reasons'])}）",
                            pb["next_at"] or time.time() + 1800)


def _quota_reset(model: str) -> float | None:
    """撞到限额时，用管理端的数据算"最早有凭据恢复"的时间。"""
    from . import quota
    snap = quota.snapshot(max_age=0)
    if not snap:
        return None
    p = quota.pool(snap, model)
    return None if p["usable"] else p["next_at"]


def _reset_time(err_text: str, headers=None) -> float | None:
    ra = (headers or {}).get("Retry-After") if headers else None
    if ra and str(ra).isdigit():
        return time.time() + int(ra)
    m = re.search(r"(?:try again|retry|reset)[^0-9]{0,20}in\s+(\d+)\s*(s|sec|m|min|h|hour)", err_text, re.I)
    if m:
        n = int(m.group(1))
        return time.time() + n * {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600}[m.group(2).lower()]
    return None


def creds() -> dict:
    p = config.home() / "secrets" / "llm.env"
    env = {}
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("WB_LLM_BASE_URL", "WB_LLM_API_KEY", "WB_LLM_MODEL", "WB_LLM_PROTOCOL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def configured() -> bool:
    c = creds()
    return (c.get("WB_LLM_PROTOCOL") == "fake") or bool(c.get("WB_LLM_API_KEY") and c.get("WB_LLM_MODEL"))


def _log(rec: dict):
    p = config.home() / "reports" / "llm_usage.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def chat(system: str, user: str, task: str, json_mode: bool = True, max_tokens: int = 8192,
         temperature: float = 0.2, retries: int = 2, kind: str = "daily"):
    """返回模型输出（json_mode 时解析成 dict）。kind=backfill 时受回填预算约束。"""
    c = creds()
    if not configured():
        raise LLMNotConfigured(f"还没有配置批量任务模型：请填写 {config.home() / 'secrets' / 'llm.env'}")
    proto = (c.get("WB_LLM_PROTOCOL") or "openai").lower()
    model = c.get("WB_LLM_MODEL") or "fake"
    if proto == "fake":  # 仅供测试：不调用任何外部模型
        return _fake(task, user)
    last = None
    for attempt in range(retries + 1):
        check_budget(kind, model)
        t0 = time.time()
        usage = {}
        try:
            if proto == "gemini":
                base = (c.get("WB_LLM_BASE_URL") or "https://generativelanguage.googleapis.com").rstrip("/")
                body = {"systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"role": "user", "parts": [{"text": user}]}],
                        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
                if json_mode:
                    body["generationConfig"]["responseMimeType"] = "application/json"
                data = http.get(f"{base}/v1beta/models/{model}:generateContent",
                                headers={"Content-Type": "application/json", "x-goog-api-key": c["WB_LLM_API_KEY"]},
                                data=json.dumps(body).encode(), ttl=0, timeout=300, retries=2, cache=False,
                                retry_status=(500, 502, 503, 504))
                text = "".join(p.get("text", "") for p in
                               ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts", []))
                um = data.get("usageMetadata") or {}
                usage = {"in": um.get("promptTokenCount"), "out": um.get("candidatesTokenCount")}
            else:
                base = (c.get("WB_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
                if not base.endswith("/v1") and "/v1/" not in base + "/":
                    base += "/v1"
                body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                data = http.get(f"{base}/chat/completions",
                                headers={"Content-Type": "application/json",
                                         "Authorization": f"Bearer {c['WB_LLM_API_KEY']}"},
                                data=json.dumps(body).encode(), ttl=0, timeout=300, retries=2, cache=False,
                                retry_status=(500, 502, 503, 504))
                text = data["choices"][0]["message"]["content"] or ""
                um = data.get("usage") or {}
                usage = {"in": um.get("prompt_tokens"), "out": um.get("completion_tokens")}
            if block_state() is None and _block_path().exists():
                _clear_block()  # 试探成功：额度恢复了
            out = parse_json(text) if json_mode else text
            _log({"ts": now_iso(), "task": task, "model": model, **usage, "s": round(time.time() - t0, 1), "ok": True})
            return out
        except (LLMNotConfigured, LLMPaused):
            raise
        except http.HttpError as e:
            last = e
            txt = str(e)
            _log({"ts": now_iso(), "task": task, "model": model, "s": round(time.time() - t0, 1), "ok": False,
                  "error": txt[:300]})
            if e.code in (402, 403, 429) or QUOTA_RX.search(txt):
                until = _reset_time(txt) or _quota_reset(model) or time.time() + budget_cfg()["probe_minutes"] * 60
                _set_block(f"批量模型额度用完（HTTP {e.code}）", until, txt)
                raise LLMPaused(f"批量模型额度用完（HTTP {e.code}）", until) from None
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001  模型 / 网络 / 解析错误都重试
            last = e
            _log({"ts": now_iso(), "task": task, "model": model, **usage, "s": round(time.time() - t0, 1),
                  "ok": False, "error": str(e)[:300]})
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    raise last


def _fake(task: str, user: str):
    """离线测试用的假模型（WB_LLM_PROTOCOL=fake）：输出结构正确、内容是占位符。"""
    import hashlib
    if task in ("relevance", "map-assign"):
        items = []
        for blk in re.findall(r"^\[(\d+)\] (.*)$", user, re.M):
            i, title = blk
            rel = bool(re.search(r"multi-?agent|agentic|workflow|orchestrat|topolog", title, re.I))
            items.append({"i": int(i), "relevant": rel, "score": 0.9 if rel else 0.1, "reason": "（假模型）关键词规则"})
        return {"items": items}
    if task == "quickread":
        title = re.search(r"标题：(.*)", user).group(1)
        h = int(hashlib.sha1(title.encode()).hexdigest(), 16)
        return {"title_zh": f"（测试）{title[:40]}", "one_liner": "假模型生成的占位一句话。",
                "quickread": {"problem": "占位", "method": "占位", "results": "占位", "limitations": "占位"},
                "setup": {"tasks": ["GSM8K"], "backbones": ["gpt-4o-mini"], "metrics": ["accuracy"], "baselines": ["AFlow"]},
                "results": [{"method": "Ours", "is_proposed": True, "task": "GSM8K", "metric": "accuracy",
                             "backbone": "gpt-4o-mini", "value": 80 + h % 15, "unit": "%", "where": "Table 1"},
                            {"method": "CoT", "is_proposed": False, "task": "GSM8K", "metric": "Acc.",
                             "backbone": "GPT-4o-mini", "value": 78, "unit": "%", "where": "Table 1"}],
                "eval_sets": [{"task": "GSM8K", "split": "test", "n_eval": 1319 if h % 2 else 500,
                               "full_size": None, "sampled": None, "how": None, "where": "Sec 4"}],
                "facets": {"optimizes": [["通信拓扑", "workflow 代码 / 算子", "prompt"][h % 3]],
                           "search": [["LLM 直接生成", "MCTS / 树搜索", "强化学习"][h % 3]], "granularity": ["任务级（一个任务一个架构）"],
                           "kind": ["方法"]},
                "quality": {"novelty": 5 + h % 5, "rigor": 5 + h % 4, "significance": 5 + h % 5, "reason": "假模型"},
                "code_url": None, "venue_claim": None}
    if task == "dataset-classify":
        return {"items": [{"i": int(i), "name": n.strip(), "category": "other", "aggregate": False}
                          for i, n in re.findall(r"^\[(\d+)\] (.*)$", user, re.M)]}
    if task == "evalsets":
        names = re.search(r"需要核对的数据集：(.*)", user).group(1).split("；")
        return {"eval_sets": [{"task": n, "split": "test", "n_eval": 100, "full_size": None, "sampled": True,
                               "how": "（假模型）随机抽样", "where": "Sec 4"} for n in names]}
    if task == "namecheck":
        items = []
        for i, n in re.findall(r"^\[(\d+)\] (.*?)  ——", user, re.M):
            items.append({"i": int(i), "name": None if n.startswith("Bogus") else ("MANGO" if n == "MASS" else n),
                          "is_proposed": n in ("Ours", "MASS")})
        return {"proposed": ["Ours", "MANGO"], "items": items}
    if task == "taste":
        return {"items": [{"i": int(i), "like": 0.1 if "Recursive" in t else 0.9, "accept": 3 if "Weak" in t else 7, "reason": "（假模型）"}
                          for i, t in re.findall(r"^\[(\d+)\] (.*)$", user, re.M)]}
    if task == "taste-screen":
        return {"items": [{"i": int(i), "like": 0.1 if "token" in t.lower() else 0.9, "reason": "（假模型）"}
                          for i, t in re.findall(r"^\[(\d+)\]（.*?）(.*)$", user, re.M)]}
    if task == "themes-propose":
        return {"themes": [{"id": "topology", "name": "拓扑", "desc": "通信拓扑"}, {"id": "workflow", "name": "工作流", "desc": "工作流搜索"}],
                "changes": "（假模型）"}
    if task == "themes-assign":
        return {"items": [{"i": int(i), "themes": ["topology" if "topolog" in t.lower() else "workflow"]}
                          for i, t in re.findall(r"^\[(\d+)\] (.*)$", user, re.M)]}
    if task == "map-overview":
        ids_ = re.findall(r'"id": "([^"]+)"', user)
        return {"overview": "（假模型）概述", "eras": [{"period": "2025", "title": "（假模型）", "shift": "（假模型）", "landmarks": ids_[:2] + ["bogus-id"],
                                                        "notes": {}}],
                "trend_notes": [{"theme": "topology", "note": "（假模型）"}],
                "insights": [{"title": "（假模型）", "detail": "（假模型）", "evidence": ids_[:1]}]}
    if task == "field-suggest":
        return {"description": "（假模型）新的方向描述", "subtopics": ["子主题 A"], "arxiv_keywords": ["multi-agent", "agentic"],
                "taste": {"likes": ["（假模型）实验扎实"], "dislikes": ["（假模型）只有 prompt 增量"]},
                "changes": ["（假模型）根据本人提供的论文补充了描述"]}
    return {"narrative": "（假模型）占位"}


def parse_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise
