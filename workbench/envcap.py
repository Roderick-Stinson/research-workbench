"""环境采集：requirements.txt（用实验进程自己的 python 解释器导出）+ 系统信息。"""
from __future__ import annotations

import glob
import json
import os
import platform
import subprocess
import threading

_PROBE = r"""
import json, sys, platform
try:
    import importlib.metadata as md
except ImportError:
    import importlib_metadata as md
pkgs = {}
for d in md.distributions():
    try:
        name = d.metadata["Name"]
    except Exception:
        continue
    if not name:
        continue
    item = {"version": d.version}
    try:
        du = d.read_text("direct_url.json")
        if du:
            j = json.loads(du)
            item["url"] = j.get("url")
            if j.get("dir_info", {}).get("editable"):
                item["editable"] = True
            if j.get("vcs_info"):
                item["vcs"] = j["vcs_info"].get("commit_id")
    except Exception:
        pass
    pkgs[name] = item
print(json.dumps({
    "python": sys.version.split()[0], "implementation": platform.python_implementation(),
    "executable": sys.executable, "prefix": sys.prefix, "base_prefix": getattr(sys, "base_prefix", sys.prefix),
    "packages": pkgs,
}))
"""

_cache: dict = {}
_lock = threading.Lock()


def _cache_key(exe: str) -> tuple:
    exe = os.path.realpath(exe)
    root = os.path.dirname(os.path.dirname(exe))
    mt = []
    for sp in glob.glob(os.path.join(root, "lib", "python*", "site-packages")):
        try:
            mt.append(os.stat(sp).st_mtime_ns)
        except OSError:
            pass
    return exe, tuple(mt)


def is_python(exe: str, argv: list) -> bool:
    names = [os.path.basename(exe)] + ([os.path.basename(argv[0])] if argv else [])
    return any(n.startswith(("python", "pypy")) for n in names)


def probe_python(exe: str, proc_env: dict) -> dict | None:
    """用实验用的解释器导出已安装的包。结果按 (解释器, site-packages mtime) 缓存。"""
    if not exe or not os.path.exists(exe):
        return None
    key = _cache_key(exe)
    with _lock:
        if key in _cache:
            return _cache[key]
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8"}
    for k in ("PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME", "PYTHONUSERBASE"):
        if proc_env.get(k):
            env[k] = proc_env[k]
    try:
        r = subprocess.run([exe, "-c", _PROBE], capture_output=True, text=True, timeout=60, env=env, cwd="/")
        data = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        data = None
    with _lock:
        _cache[key] = data
    return data


def requirements_txt(probe: dict) -> str:
    lines = [
        f"# 由 workbench 自动导出：python {probe.get('python')} @ {probe.get('executable')}",
        f"# prefix: {probe.get('prefix')}",
    ]
    editable = []
    for name, it in sorted(probe.get("packages", {}).items(), key=lambda x: x[0].lower()):
        if it.get("editable"):
            editable.append(f"# editable（代码在快照/源目录里）: {name}=={it['version']} @ {it.get('url')}")
        elif it.get("vcs"):
            lines.append(f"{name} @ {it.get('url')}@{it['vcs']}" if it.get("url") else f"{name}=={it['version']}")
        else:
            lines.append(f"{name}=={it['version']}")
    return "\n".join(lines + editable) + "\n"


def system_info() -> dict:
    info = {"hostname": platform.node(), "kernel": platform.release()}
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10)
        info["nvidia_driver"] = r.stdout.splitlines()[0].strip() if r.stdout else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        info["nvidia_driver"] = None
    try:
        r = subprocess.run(["nvcc", "--version"], capture_output=True, text=True, timeout=10)
        info["nvcc"] = r.stdout.strip().splitlines()[-1] if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        info["nvcc"] = None
    return info
