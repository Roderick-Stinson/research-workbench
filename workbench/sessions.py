"""关联 codex / Claude Code 对话（ADR-0009 第 2 条）。

1. 沿父进程链看是不是 codex / claude 启动的（tmux / nohup / setsid 会断链，所以只作参考）；
2. 在启动时间附近活跃过的会话文件里，找内容和本次命令最像的那次工具调用；
3. 认领时用 `wb context <RUN>` 取出那次调用前后的对话，供 agent 起草"目的"。
"""
from __future__ import annotations

import glob
import json
import os
import re
import threading
from collections import OrderedDict

from . import config
from .util import parse_iso

SHELLS = {"bash", "sh", "zsh", "dash", "fish", "env", "nohup", "setsid", "timeout", "time", "stdbuf", "tmux", "screen"}


def agent_kind_from_chain(chain: list) -> str | None:
    for a in chain:
        c = a.get("cmd", "") + " " + a.get("exe", "")
        if re.search(r"(^|/)codex(\s|$)|codex-linux|/\.codex/|@openai/codex", c):
            return "codex"
        if re.search(r"(^|/)claude(\s|$)|ccd-cli|/\.claude/|claude-code", c):
            return "claude"
    return None


# ---------------- 解析会话文件为统一事件流 ----------------

_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_lock = threading.Lock()


def _ts(s):
    try:
        return parse_iso(s)
    except Exception:
        return None


def _norm_tool(text: str) -> str:
    """codex 的工具调用是 JS / JSON 字符串，命令被转义过：还原成普通 shell 文本再匹配。"""
    t = text.replace("\\\\\\n", " ").replace("\\\\n", " ")
    t = t.replace('\\"', '"').replace("\\n", "\n").replace("\\t", " ").replace("\\\n", " ")
    return t.replace("\\\\", "\\")


def parse_codex(path: str) -> dict:
    meta = {"agent": "codex", "file": path, "session_id": None, "cwd": None, "events": []}
    ev = meta["events"]
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = _ts(d.get("timestamp", ""))
            typ = d.get("type")
            p = d.get("payload") or {}
            pt = p.get("type")
            if typ == "session_meta":
                meta["session_id"] = meta["session_id"] or p.get("id") or p.get("session_id")
                meta["cwd"] = meta["cwd"] or p.get("cwd")
            elif typ == "turn_context" and p.get("cwd"):
                meta["cwd"] = meta["cwd"] or p.get("cwd")
            elif typ == "event_msg" and pt == "user_message":
                ev.append({"ts": t, "role": "user", "text": p.get("message", "")})
            elif typ == "event_msg" and pt == "agent_message":
                ev.append({"ts": t, "role": "assistant", "text": p.get("message", "")})
            elif typ == "response_item" and pt in ("function_call", "custom_tool_call", "local_shell_call"):
                body = p.get("input") or p.get("arguments") or json.dumps(p.get("action", {}), ensure_ascii=False)
                if not isinstance(body, str):
                    body = json.dumps(body, ensure_ascii=False)
                ev.append({"ts": t, "role": "tool", "text": _norm_tool(body[:20000]), "name": p.get("name")})
    return meta


def parse_claude(path: str) -> dict:
    meta = {"agent": "claude", "file": path, "session_id": None, "cwd": None, "events": []}
    ev = meta["events"]
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            meta["session_id"] = meta["session_id"] or d.get("sessionId")
            if d.get("cwd") and not meta["cwd"]:
                meta["cwd"] = d["cwd"]
            if d.get("isSidechain"):
                continue
            t = _ts(d.get("timestamp", "")) if d.get("timestamp") else None
            msg = d.get("message") or {}
            content = msg.get("content")
            if d.get("type") == "user":
                if isinstance(content, str):
                    ev.append({"ts": t, "role": "user", "text": content})
                elif isinstance(content, list):
                    texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                    if texts:
                        ev.append({"ts": t, "role": "user", "text": "\n".join(texts)})
            elif d.get("type") == "assistant" and isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        ev.append({"ts": t, "role": "assistant", "text": c.get("text", "")})
                    elif c.get("type") == "tool_use":
                        inp = c.get("input", {})
                        body = inp.get("command") if isinstance(inp, dict) and isinstance(inp.get("command"), str) \
                            else json.dumps(inp, ensure_ascii=False)
                        ev.append({"ts": t, "role": "tool", "text": _norm_tool(body[:20000]), "name": c.get("name")})
    return meta


def load_session(path: str) -> dict | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_size, st.st_mtime_ns)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    try:
        meta = parse_codex(path) if "/.codex/" in path or os.path.basename(path).startswith("rollout-") else parse_claude(path)
    except OSError:
        return None
    with _lock:
        _cache[key] = meta
        while len(_cache) > 24:
            _cache.popitem(last=False)
    return meta


def candidate_files(start_ts: float) -> list:
    cfg = config.load()
    out = []
    cdir = os.path.expanduser(cfg["codex_sessions"])
    out += glob.glob(os.path.join(cdir, "*", "*", "*", "rollout-*.jsonl"))
    pdir = os.path.expanduser(cfg["claude_projects"])
    out += glob.glob(os.path.join(pdir, "*", "*.jsonl"))
    res = []
    for p in out:
        try:
            mt = os.stat(p).st_mtime
        except OSError:
            continue
        # 启动那一刻会话必须是活跃的：文件在启动之后（或启动前很短时间内）还被写过
        if mt >= start_ts - 120:
            res.append((mt, p))
    res.sort(reverse=True)
    return [p for _, p in res[:40]]


# ---------------- 匹配 ----------------

_TOKEN_SPLIT = re.compile(r"[\s'\"=,;|&<>()\[\]{}]+")
_BORING = {"python", "python3", "-u", "-m", "nohup", "bash", "sh", "&&", "cd", "exec", "env", "-c", "true", "1", "0", "2"}


def tokens_of(cmd: str) -> list:
    toks = []
    for t in _TOKEN_SPLIT.split(cmd):
        if not t or t in _BORING or len(t) < 3:
            continue
        b = os.path.basename(t.rstrip("/"))
        if b.startswith("python"):
            continue
        toks.append(t)
        if "/" in t and b and b != t and len(b) >= 3:
            toks.append(b)
    return list(dict.fromkeys(toks))


_VAR = r"(?:\d+|\$\{?\w+\}?|\$\(\(.*?\)\))"


def _token_in(t: str, text: str) -> bool:
    if t in text:
        return True
    # 循环里启动的：命令里写的是 $s / ${seed}，进程参数里是 0 / 1 / 2
    if any(ch.isdigit() for ch in t):
        rx = re.sub(r"\\?\d+", lambda _m: _VAR, re.escape(t))
        try:
            return re.search(rx, text) is not None
        except re.error:
            return False
    return False


def _score(tokens: list, text: str) -> float:
    if not tokens:
        return 0.0
    hit = sum(1 for t in tokens if _token_in(t, text))
    return hit / len(tokens)


def link(start_ts: float, argv: list, cwd: str, chain: list) -> dict | None:
    """为一个 Run 找启动它的会话。返回 {agent, file, session_id, call_ts, call_text, score, confidence, via}。"""
    agent = agent_kind_from_chain(chain)
    cmds = [" ".join(argv)]
    for a in chain:
        exe = os.path.basename(a.get("exe", ""))
        first = os.path.basename(a.get("cmd", "").split(" ")[0]) if a.get("cmd") else ""
        if agent_kind_from_chain([a]):
            break  # 到 agent 本身就停
        if exe in SHELLS and first in SHELLS and len(a.get("cmd", "").split()) <= 1:
            continue
        cmds.append(a.get("cmd", ""))
    tok_sets = [tokens_of(c) for c in cmds if c]
    tok_sets = [t for t in tok_sets if t]
    if not tok_sets:
        return None

    best = None
    for path in candidate_files(start_ts):
        if agent == "codex" and "/.claude/" in path:
            continue
        if agent == "claude" and "/.codex/" in path:
            continue
        meta = load_session(path)
        if not meta:
            continue
        cwd_bonus = 0.1 if meta.get("cwd") and cwd and (cwd.startswith(meta["cwd"]) or meta["cwd"].startswith(cwd)) else 0.0
        for i, e in enumerate(meta["events"]):
            if e["role"] != "tool" or e["ts"] is None:
                continue
            dt = start_ts - e["ts"]
            if dt < -60 or dt > config.load()["session_search_hours"] * 3600:
                continue
            s = max(_score(ts, e["text"]) for ts in tok_sets)
            if s < 0.5:
                continue
            # 时间越近越好：1 分钟内不扣分，之后每小时扣 0.05
            total = s + cwd_bonus - max(0.0, dt - 60) / 3600 * 0.05
            if best is None or total > best["score"]:
                best = {
                    "agent": meta["agent"], "file": path, "session_id": meta.get("session_id"),
                    "session_cwd": meta.get("cwd"), "event_index": i, "call_ts": e["ts"],
                    "call_text": e["text"][:2000], "score": round(total, 3), "token_hit": round(s, 3),
                }
    if best is None:
        return {"agent": agent, "confidence": "none", "via": "chain" if agent else None} if agent else None
    conf = "high" if best["token_hit"] >= 0.8 and (agent == best["agent"] or agent is None) else "medium"
    if best["token_hit"] < 0.65:
        conf = "low"
    best["confidence"] = conf
    best["via"] = "chain+content" if agent else "content"
    return best


def context(link_info: dict, before: int = 12, after: int = 4) -> list:
    """取出启动调用前后的对话片段：前面 before 条 user/assistant 消息 + 调用本身 + 后面 after 条。"""
    if not link_info or not link_info.get("file"):
        return []
    meta = load_session(link_info["file"])
    if not meta:
        return []
    evs = meta["events"]
    idx = link_info.get("event_index")
    if idx is None or idx >= len(evs):
        ts = link_info.get("call_ts") or 0
        idx = min(range(len(evs)), key=lambda i: abs((evs[i]["ts"] or 0) - ts)) if evs else 0
    out = []
    j = idx - 1
    while j >= 0 and len(out) < before:
        if evs[j]["role"] in ("user", "assistant"):
            out.append(evs[j])
        j -= 1
    out.reverse()
    out.append(dict(evs[idx], launch=True))
    k = idx + 1
    n = 0
    while k < len(evs) and n < after:
        if evs[k]["role"] in ("user", "assistant"):
            out.append(evs[k])
            n += 1
        k += 1
    return out
