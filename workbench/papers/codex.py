"""codex 执行器（ADR-0020）：每个任务一次 `codex exec`，工作目录限定在任务自己的目录。

配置（config.json 的 "codex"）：
  bin        codex 可执行文件（cron 里没有 nvm 的 PATH，安装时写入绝对路径）
  model      不填就用 ~/.codex/config.toml 里的默认模型
  reasoning  推理强度（不填用默认）
  extra_args 其它参数（例如换成第三方 API 的 provider：["-c", "model_provider=\"xxx\""]）
  parallel   并行数
每次调用记一行到 reports/codex_usage.jsonl（任务、耗时、退出码、失败原因），用来观察订阅额度够不够。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .. import config
from ..util import now_iso
from .llm import LLMPaused

REPO = Path(__file__).resolve().parent.parent.parent
SKILLS = REPO / "agents" / "skills"


def cfg() -> dict:
    c = dict(config.load().get("codex") or {})
    c.setdefault("bin", shutil.which("codex") or "codex")
    c.setdefault("parallel", 3)
    c.setdefault("timeout_min", 60)
    return c


def _log(rec: dict):
    p = config.home() / "reports" / "codex_usage.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _block_path() -> Path:
    return config.home() / "reports" / "codex_block.json"


def block_state() -> dict | None:
    try:
        st = json.loads(_block_path().read_text())
    except (OSError, ValueError):
        return None
    return st if st.get("until", 0) > time.time() else None


def _reset_from_log(tail: str) -> float:
    """codex 的提示形如 "try again at 3:16 PM"；拿不到就 1 小时后再试。"""
    import datetime as _dt
    import re
    m = re.search(r"try again at (\d{1,2}):(\d{2})\s*([AP]M)?", tail, re.I)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if m.group(3) and m.group(3).upper() == "PM" and h < 12:
            h += 12
        if m.group(3) and m.group(3).upper() == "AM" and h == 12:
            h = 0
        now = _dt.datetime.now()
        t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if t <= now:
            t += _dt.timedelta(days=1)
        return t.timestamp() + 120
    return time.time() + 3600


def run(task: str, workdir: Path, prompt: str, timeout_min: float | None = None, network: bool = False) -> dict:
    """在 workdir 里跑一次 codex exec。返回 {ok, code, seconds, last_message, log}。
    订阅额度用完时写 codex_block.json 并抛 LLMPaused；熔断期间不再启动 codex。"""
    st = block_state()
    if st:
        raise LLMPaused("codex 订阅额度用完", st["until"])
    c = cfg()
    workdir.mkdir(parents=True, exist_ok=True)
    last = workdir / ".codex-last.txt"
    args = [c["bin"], "exec", "--skip-git-repo-check", "--ephemeral", "-C", str(workdir), "-o", str(last)]
    if network:
        # 需要联网检索时（构思 / 审稿查新）：放开沙箱的网络
        args += ["-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
    else:
        args += ["-s", "workspace-write"]
    if c.get("model"):
        args += ["-m", c["model"]]
    if c.get("reasoning"):
        args += ["-c", f'model_reasoning_effort="{c["reasoning"]}"']
    args += list(c.get("extra_args") or [])
    args.append("-")
    env = dict(os.environ)
    bindir = str(Path(c["bin"]).parent)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    proxy = config.load().get("proxy")
    if proxy:
        for k in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
            env.setdefault(k, proxy)
        env.setdefault("no_proxy", "localhost,127.0.0.1")
    t0 = time.time()
    logf = workdir / ".codex.log"
    try:
        with open(logf, "w") as lf:
            r = subprocess.run(args, input=prompt, text=True, stdout=lf, stderr=subprocess.STDOUT, env=env,
                               timeout=(timeout_min or c["timeout_min"]) * 60)
        code = r.returncode
        err = None
    except subprocess.TimeoutExpired:
        code, err = -1, "超时"
    except OSError as e:
        code, err = -2, f"无法启动 codex：{e}"
    secs = round(time.time() - t0, 1)
    tail = ""
    try:
        tail = logf.read_text(errors="replace")[-3000:]
    except OSError:
        pass
    if code != 0 and not err:
        low = tail.lower()
        err = "额度用完 / 限流" if ("rate limit" in low or "usage limit" in low or "quota" in low) else f"退出码 {code}"
    out = {"ok": code == 0, "code": code, "seconds": secs, "error": err,
           "last_message": last.read_text(errors="replace") if last.exists() else "", "log": str(logf)}
    _log({"ts": now_iso(), "task": task, "dir": str(workdir), "seconds": secs, "code": code, "error": err})
    if err == "额度用完 / 限流":
        until = _reset_from_log(tail)
        _block_path().write_text(json.dumps({"reason": "codex 订阅额度用完", "until": until,
                                             "until_text": time.strftime("%m-%d %H:%M", time.localtime(until)),
                                             "since": now_iso()}, ensure_ascii=False))
        raise LLMPaused("codex 订阅额度用完", until)
    return out
