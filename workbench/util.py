"""小工具：时间、原子写 JSON、脱敏、ID。"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import tempfile
from pathlib import Path

TZ = _dt.datetime.now().astimezone().tzinfo


def now_iso(ts: float | None = None) -> str:
    t = _dt.datetime.fromtimestamp(ts if ts is not None else _dt.datetime.now().timestamp(), TZ)
    return t.isoformat(timespec="seconds")


def parse_iso(s: str) -> float:
    s = s.replace("Z", "+00:00")
    return _dt.datetime.fromisoformat(s).timestamp()


def write_json(path: Path, obj) -> None:
    """原子写：先写临时文件再 rename，读者永远看不到写了一半的 JSON。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path, limit: int | None = None) -> str:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
            if limit is not None and n >= limit:
                break
    return h.hexdigest()


def fingerprint(path) -> dict:
    """大文件（模型、数据）的廉价指纹：大小 + mtime + 头尾各 1MB 的哈希。只用来发现"被改过"。"""
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if st.st_size > (2 << 20):
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return {"size": st.st_size, "mtime": now_iso(st.st_mtime), "quick_sha256": h.hexdigest()[:16]}


def shell_join(argv) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def new_run_id(start_ts: float, pid: int) -> str:
    t = _dt.datetime.fromtimestamp(start_ts, TZ)
    suffix = hashlib.sha1(f"{pid}-{start_ts}".encode()).hexdigest()[:3]
    return "R" + t.strftime("%Y%m%d-%H%M%S") + "-" + suffix


# ---------------- 脱敏（ADR-0003：环境变量里的 key 要脱敏） ----------------

SECRET_KEY_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASS|PASSWD|PASSWORD|AUTH|CRED|COOKIE|PRIVATE|SESSION_ID|SIGNATURE|DSN|WEBHOOK)",
    re.I,
)
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|sk-ant-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|"
    r"hf_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|"
    r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}|\d{8,10}:[A-Za-z0-9_\-]{30,})"
)
# 体积大又没有复现价值的变量，直接丢掉
NOISY_ENV = {"LS_COLORS", "LSCOLORS", "PS1", "PROMPT", "RPROMPT", "ZLS_COLORS", "TERMCAP", "_"}
NOISY_ENV_PREFIX = ("VSCODE_", "TMUX_PLUGIN", "ZSH_", "P9K_", "POWERLEVEL", "STARSHIP_", "ITERM")

REDACTED = "<redacted>"


def redact_value(v: str) -> str:
    return SECRET_VALUE_RE.sub(REDACTED, v)


def redact_env(env: dict) -> dict:
    out = {}
    for k, v in sorted(env.items()):
        if k in NOISY_ENV or k.startswith(NOISY_ENV_PREFIX):
            continue
        if SECRET_KEY_RE.search(k) and not k.endswith(("_PATH", "_DIR", "_FILE", "_HOME")):
            out[k] = REDACTED
        else:
            out[k] = redact_value(v)[:2000]
    return out


def redact_argv(argv: list) -> list:
    """命令行里也可能带 key（--api_key sk-xxx），一并脱敏。"""
    out = []
    hide_next = False
    for a in argv:
        if hide_next:
            out.append(REDACTED)
            hide_next = False
            continue
        m = re.match(r"^(--?[\w\-.]+)=(.*)$", a)
        if m and SECRET_KEY_RE.search(m.group(1)) and not re.search(r"(path|dir|file)$", m.group(1), re.I):
            out.append(f"{m.group(1)}={REDACTED}")
            continue
        if re.match(r"^--?[\w\-.]+$", a) and SECRET_KEY_RE.search(a) and not re.search(r"(path|dir|file)$", a, re.I):
            out.append(a)
            hide_next = True
            continue
        out.append(redact_value(a))
    return out


def human_bytes(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def human_duration(s) -> str:
    if s is None:
        return "-"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"
