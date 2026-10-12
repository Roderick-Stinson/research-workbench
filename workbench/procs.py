"""读 /proc 和 nvidia-smi。只读，不碰任何进程。"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _boot_time() -> float:
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    return float(line.split()[1])
    except OSError:
        pass
    return 0.0


BOOT_TIME = _boot_time()


@dataclass
class Proc:
    pid: int
    ppid: int
    pgrp: int
    uid: int
    pstart: int  # /proc/<pid>/stat 第 22 列（开机后的 tick 数）；和 pid 一起唯一标识一个进程
    state: str
    argv: list = field(default_factory=list)
    exe: str = ""
    cwd: str = ""

    @property
    def key(self) -> str:
        return f"{self.pid}:{self.pstart}"

    @property
    def start_ts(self) -> float:
        return BOOT_TIME + self.pstart / CLK_TCK

    @property
    def cmd(self) -> str:
        return " ".join(self.argv)


def read_stat(pid: int):
    """返回 (ppid, pgrp, state, starttime)。comm 里可能有空格和括号，按最后一个 ')' 切。"""
    with open(f"/proc/{pid}/stat") as f:
        s = f.read()
    rest = s[s.rfind(")") + 2:].split()
    return int(rest[1]), int(rest[2]), rest[0], int(rest[19])


def read_proc(pid: int, need_detail: bool = True) -> Proc | None:
    try:
        st = os.stat(f"/proc/{pid}")
        ppid, pgrp, state, pstart = read_stat(pid)
        p = Proc(pid=pid, ppid=ppid, pgrp=pgrp, uid=st.st_uid, pstart=pstart, state=state)
        if need_detail:
            fill_detail(p)
        return p
    except (OSError, ValueError, IndexError):
        return None


def fill_detail(p: Proc) -> None:
    try:
        with open(f"/proc/{p.pid}/cmdline", "rb") as f:
            raw = f.read()
        p.argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    except OSError:
        p.argv = []
    try:
        p.exe = os.readlink(f"/proc/{p.pid}/exe")
    except OSError:
        p.exe = ""
    try:
        p.cwd = os.readlink(f"/proc/{p.pid}/cwd")
    except OSError:
        p.cwd = ""


def list_pids():
    for name in os.listdir("/proc"):
        if name.isdigit():
            yield int(name)


def list_user_procs(uid: int) -> dict:
    """当前用户的全部进程（只读 stat 和 owner，细节按需再读）。"""
    out = {}
    for pid in list_pids():
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        p = read_proc(pid, need_detail=False)
        if p is not None:
            out[pid] = p
    return out


def read_environ(pid: int) -> dict:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def ancestors(pid: int, limit: int = 30) -> list:
    """父进程链（不含自己），每项 {pid, cmd}。"""
    chain = []
    cur = pid
    for _ in range(limit):
        try:
            ppid = read_stat(cur)[0]
        except (OSError, ValueError, IndexError):
            break
        if ppid <= 1:
            break
        p = read_proc(ppid)
        if p is None:
            break
        chain.append({"pid": p.pid, "cmd": p.cmd[:500], "exe": p.exe})
        cur = ppid
    return chain


O_ACCMODE = 0o3


def open_files(pid: int) -> list:
    """进程当前打开的普通文件：[(path, mode)]，mode 为 'r' / 'w' / 'rw'。"""
    out = []
    base = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(base)
    except OSError:
        return out
    for fd in fds:
        try:
            target = os.readlink(f"{base}/{fd}")
        except OSError:
            continue
        if not target.startswith("/"):
            continue
        if target.endswith(" (deleted)"):
            continue
        mode = "r"
        try:
            with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                for line in f:
                    if line.startswith("flags:"):
                        flags = int(line.split()[1], 8)
                        acc = flags & O_ACCMODE
                        mode = {0: "r", 1: "w", 2: "rw"}.get(acc, "r")
                        break
        except (OSError, ValueError):
            pass
        out.append((target, mode, fd))
    return out


def mapped_files(pid: int) -> set:
    """mmap 进来的文件（safetensors 权重通常是 mmap，不在 fd 里）。"""
    out = set()
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                parts = line.split(None, 5)
                if len(parts) == 6 and parts[5].startswith("/"):
                    out.add(parts[5].strip())
    except OSError:
        pass
    return out


def exit_code_if_zombie(pid: int):
    """僵尸进程的退出码（/proc/<pid>/stat 第 52 列）。拿不到就返回 None。"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        rest = s[s.rfind(")") + 2:].split()
        if rest[0] != "Z" or len(rest) < 50:
            return None
        status = int(rest[49])
        if status & 0x7F:
            return -(status & 0x7F)  # 被信号杀死
        return (status >> 8) & 0xFF
    except (OSError, ValueError, IndexError):
        return None


# ---------------- GPU ----------------


def _smi(args, timeout=10) -> str:
    try:
        r = subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def gpu_apps() -> dict:
    """{pid: [(gpu_uuid, used_mem_mb)]}"""
    out = {}
    for line in _smi(["--query-compute-apps=pid,gpu_uuid,used_memory", "--format=csv,noheader,nounits"]).splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            mem = float(parts[2]) if parts[2] not in ("", "[N/A]") else 0.0
        except ValueError:
            continue
        out.setdefault(pid, []).append((parts[1], mem))
    return out


def gpu_devices() -> dict:
    """{uuid: {index, name, util, mem_used, mem_total}}"""
    out = {}
    q = "index,uuid,name,utilization.gpu,memory.used,memory.total,driver_version"
    for line in _smi([f"--query-gpu={q}", "--format=csv,noheader,nounits"]).splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != 7:
            continue

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None

        out[parts[1]] = {
            "index": int(parts[0]), "name": parts[2], "util": num(parts[3]),
            "mem_used": num(parts[4]), "mem_total": num(parts[5]), "driver": parts[6],
        }
    return out
