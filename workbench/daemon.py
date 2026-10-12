"""wbd：后台自动抓现场（ADR-0009）。

由 cron 每分钟用 flock 拉起一次；已经在跑就直接退出。所以挂了最多 1 分钟就会自己恢复。

每个 tick（默认 2 秒）：
1. 扫当前用户的进程，找"候选"：python 进程且 cwd / 脚本在 watch_roots 下，或任何用 GPU 的进程；
2. 候选活过一个 tick 就立刻拍代码快照（短命的 `python -c` 之类不浪费快照）；
3. 用了 GPU 或活过 promote_after_seconds → 升级为 Run，写 runs/<ID>/run.json，导出 requirements.txt，关联对话；
4. 运行中：采样 GPU、记录写出的文件和读入的大文件；
5. 主进程结束：补记退出信息、复制日志和产物（只读）、提取候选指标。

原则：只读别人的进程，绝不 attach / 发信号 / 改实验的任何东西；出错只写日志，不影响实验。
"""
from __future__ import annotations

import concurrent.futures as cf
import fnmatch
import json
import os
import re
import shutil
import signal
import stat
import sys
import threading
import time
import traceback
from pathlib import Path

from . import __version__, config, envcap, procs, sessions, snapshot, store
from .util import fingerprint, new_run_id, now_iso, read_json, redact_argv, redact_env, shell_join, write_json

LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    d = store.daemon_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / "daemon.log"
    with LOG_LOCK:
        try:
            if p.exists() and p.stat().st_size > 10 * 1024 * 1024:
                os.replace(p, d / "daemon.log.1")
        except OSError:
            pass
        with open(p, "a") as f:
            f.write(f"{now_iso()} {msg}\n")


CONFIG_EXTS = (".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".conf", ".jsonnet", ".txt", ".env.example")
SKIP_ARGS_RE = re.compile(r"^(-c|-)$")
SKIP_MODULES = {"pytest", "py_compile", "json.tool", "pip", "venv", "ensurepip", "compileall", "pydoc", "timeit",
                "http.server", "site", "tabnanny", "pdb", "cProfile", "unittest", "doctest", "IPython", "ipykernel"}


def parse_entry(argv: list, cwd: str) -> dict:
    """从命令行找入口：脚本路径或 -m 模块，以及引用到的配置文件。"""
    entry = {}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-m" and i + 1 < len(argv):
            entry["module"] = argv[i + 1]
            break
        if a in ("-c", "-"):
            entry["inline"] = True
            break
        if a.startswith("-"):
            if a in ("-W", "-X") and i + 1 < len(argv):
                i += 1
            i += 1
            continue
        p = a if os.path.isabs(a) else os.path.join(cwd, a)
        entry["script"] = os.path.normpath(p)
        break
    cfgs = []
    for a in argv[1:]:
        cand = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a
        if not cand or cand.startswith("-") or len(cand) > 400:
            continue
        if cand.endswith(CONFIG_EXTS) or os.path.basename(cand).startswith(("config", "conf")):
            p = cand if os.path.isabs(cand) else os.path.join(cwd, cand)
            try:
                if os.path.isfile(p) and os.path.getsize(p) < 5 * 1024 * 1024:
                    cfgs.append(os.path.normpath(p))
            except OSError:
                pass
    entry["config_files"] = list(dict.fromkeys(cfgs))
    return entry


def detect_seeds(argv: list, env: dict, fields: list) -> dict:
    out = {}
    names = "|".join(re.escape(f) for f in fields)
    rx_eq = re.compile(rf"^-{{0,2}}(?:[\w.]*\.)?({names})=(\S+)$", re.I)
    rx_flag = re.compile(rf"^--?({names})$", re.I)
    for i, a in enumerate(argv):
        m = rx_eq.match(a)
        if m:
            out[m.group(1).lower()] = m.group(2)
            continue
        m = rx_flag.match(a)
        if m and i + 1 < len(argv):
            out[m.group(1).lower()] = argv[i + 1]
    for k in ("SEED", "PYTHONHASHSEED", "RANDOM_SEED"):
        if env.get(k):
            out[f"env:{k}"] = env[k]
    return out


def output_dir_args(argv: list, cwd: str) -> list:
    """--output_dir / --save_dir / --out 之类参数指向的目录。"""
    out = []
    rx = re.compile(r"^--?([\w\-]*(out|save|ckpt|checkpoint|result|log)[\w\-]*)(=(.*))?$", re.I)
    for i, a in enumerate(argv):
        m = rx.match(a)
        if not m:
            continue
        val = m.group(4) if m.group(3) else (argv[i + 1] if i + 1 < len(argv) else None)
        if not val or val.startswith("-"):
            continue
        p = val if os.path.isabs(val) else os.path.join(cwd, val)
        p = os.path.normpath(p)
        if os.path.isdir(p):
            out.append(p)
        elif os.path.isdir(os.path.dirname(p)):
            out.append(os.path.dirname(p))
    return list(dict.fromkeys(out))


class Daemon:
    def __init__(self, once: bool = False):
        self.cfg = config.load()
        self.uid = os.getuid()
        self.mypid = os.getpid()
        self.once = once
        self.pool = cf.ThreadPoolExecutor(max_workers=4)
        self.root_locks: dict = {}
        self.root_last: dict = {}  # root -> 最近一次快照（用于并发启动的去重）
        self.cands: dict = {}  # key -> run（内存中的候选）
        self.runs: dict = {}  # run_id -> run（运行中）
        self.member_of: dict = {}  # proc key -> run_id
        self.rejected: dict = {}  # proc key -> argv 摘要（不是候选；短时间内会复查，防 exec）
        self.pending: dict = {}  # key -> 第一次看到的时间（等一个 tick 再拍快照）
        self.tick_no = 0
        self.stop = False
        self.lock = threading.RLock()
        self.gpu_dev = {}
        self.last_disk = 0.0

    # ---------------- 判定 ----------------

    def in_watch(self, path: str) -> bool:
        return any(snapshot.under(path, w) for w in self.cfg["watch_roots"])

    def excluded(self, cmd: str) -> bool:
        return any(p in cmd for p in self.cfg["exclude_cmd_patterns"])

    def is_candidate(self, p: procs.Proc, gpu_pids: set) -> bool:
        if p.pid == self.mypid or not p.argv:
            return False
        cmd = p.cmd
        if self.excluded(cmd):
            return False
        if p.cwd and snapshot.under(p.cwd, str(store.home())):
            return False  # 工作台自己的任务（如 codex 精读时跑的作图脚本）不是实验
        uses_gpu = p.pid in gpu_pids
        py = envcap.is_python(p.exe, p.argv)
        if not py and not uses_gpu:
            return False
        if py and not uses_gpu:
            ent = parse_entry(p.argv, p.cwd)
            if ent.get("inline") or ent.get("module") in SKIP_MODULES:
                return False
            if not ent.get("script") and not ent.get("module"):
                return False  # 交互式 python
            script_in = ent.get("script") and self.in_watch(ent["script"])
            if not (self.in_watch(p.cwd) or script_in):
                return False
        return True

    def should_merge(self, child: procs.Proc, run: dict) -> bool:
        """子进程并入父 Run，还是单独成一个 Run（带 parent_run）。"""
        cmd = child.cmd
        pats = self.cfg["launcher_cmd_patterns"]
        if any(x in cmd for x in pats):
            return True
        root_cmd = " ".join(run.get("argv_raw", []))
        if any(x in root_cmd for x in pats):
            return True
        if not envcap.is_python(child.exe, child.argv):
            return True
        if child.argv[1:] == run.get("argv_raw", [])[1:]:
            return True  # fork 出来的 worker
        ent = parse_entry(child.argv, child.cwd)
        if ent.get("inline"):
            return True
        if ent.get("script") and ent.get("script") == run.get("entry", {}).get("script"):
            return True
        return False

    # ---------------- 主循环 ----------------

    def run_forever(self):
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        log(f"wbd {__version__} 启动 pid={self.mypid} home={store.home()}")
        self.restore_state()
        tick = self.cfg["tick_seconds"]
        while not self.stop:
            t0 = time.time()
            try:
                self.tick()
            except Exception:
                log("tick 出错：" + traceback.format_exc()[-2000:])
            if self.once:
                break
            time.sleep(max(0.2, tick - (time.time() - t0)))
        self.pool.shutdown(wait=True)
        self.save_state()
        log("wbd 退出")

    def tick(self):
        self.tick_no += 1
        now = time.time()
        ps = procs.list_user_procs(self.uid)
        alive = {p.key for p in ps.values()}
        gpu = procs.gpu_apps() if self.tick_no % 2 == 1 or self.once else getattr(self, "_gpu", {})
        self._gpu = gpu
        gpu_pids = set(gpu)
        key_of_pid = {p.pid: p.key for p in ps.values()}

        with self.lock:
            # 清理已经消失的进程
            for k in list(self.rejected):
                if k not in alive:
                    del self.rejected[k]
            for k in list(self.pending):
                if k not in alive:
                    del self.pending[k]
            for k in list(self.member_of):
                if k not in alive:
                    del self.member_of[k]

            for p in sorted(ps.values(), key=lambda x: x.pstart):
                k = p.key
                if k in self.member_of or k in self.cands:
                    continue
                if k in self.rejected:
                    # 年轻进程可能刚 exec 成 python；用了 GPU 的也要复查
                    if (now - p.start_ts > 120) and p.pid not in gpu_pids:
                        continue
                    if p.pid not in gpu_pids:
                        procs.fill_detail(p)
                        if " ".join(p.argv)[:300] == self.rejected[k]:
                            continue
                # 是否属于已有 Run / 候选的子进程
                owner = self.find_owner(p, ps)
                if not p.argv:
                    procs.fill_detail(p)
                if owner is not None:
                    orun = self.runs.get(owner) or next((c for c in self.cands.values() if c["id"] == owner), None)
                    if orun is not None and (self.should_merge(p, orun) or not self.is_candidate(p, gpu_pids)):
                        self.member_of[k] = owner
                        orun.setdefault("members", [])
                        if k not in orun["members"]:
                            orun["members"].append(k)
                            orun.setdefault("children", []).append(
                                {"pid": p.pid, "cmd": " ".join(redact_argv(p.argv))[:300]})
                        continue
                if not self.is_candidate(p, gpu_pids):
                    self.rejected[k] = " ".join(p.argv)[:300]
                    continue
                # 新候选：活过一个 tick 再拍快照
                if k not in self.pending and (now - p.start_ts) < self.cfg["tick_seconds"] and not self.once:
                    self.pending[k] = now
                    continue
                self.pending.pop(k, None)
                self.new_candidate(p, owner)

            # 候选：升级或丢弃
            for k, run in list(self.cands.items()):
                if k not in alive:
                    del self.cands[k]
                    continue
                pid = run["pid"]
                age = now - run["start_ts"]
                member_gpu = any(int(m.split(":")[0]) in gpu_pids for m in run.get("members", [k]))
                if run.get("_snap_done") and (member_gpu or age >= self.cfg["promote_after_seconds"]):
                    del self.cands[k]
                    if member_gpu:
                        run.setdefault("tags", []).append("gpu")
                    self.promote(run)

            # 运行中的 Run
            for rid, run in list(self.runs.items()):
                root_key = run["key"]
                p = ps.get(run["pid"])
                if p is not None and p.key == root_key and p.state == "Z":
                    code = procs.exit_code_if_zombie(p.pid)
                    if code is not None:
                        run["exit"] = {"code": code, "source": "zombie"}
                if root_key in alive and not (p is not None and p.state == "Z"):
                    self.observe(run, ps, gpu, now)
                else:
                    del self.runs[rid]
                    self.pool.submit(self.safe, self.finalize, run)

        if self.tick_no % 5 == 0 or self.once:
            self.heartbeat()
        if now - self.last_disk > 600:
            self.last_disk = now
            self.pool.submit(self.safe, self.disk_stats)
        if self.tick_no % 15 == 0:
            self.save_state()

    def find_owner(self, p: procs.Proc, ps: dict) -> str | None:
        cur = p.ppid
        for _ in range(40):
            q = ps.get(cur)
            if q is None:
                return None
            if q.key in self.member_of:
                return self.member_of[q.key]
            if q.key in self.cands:
                return self.cands[q.key]["id"]
            cur = q.ppid
        return None

    def safe(self, fn, *a):
        try:
            return fn(*a)
        except Exception:
            log(f"{fn.__name__} 出错：" + traceback.format_exc()[-3000:])

    # ---------------- 候选 → 拍快照 ----------------

    def new_candidate(self, p: procs.Proc, parent_run: str | None):
        now = time.time()
        env_raw = procs.read_environ(p.pid)
        rid = new_run_id(p.start_ts, p.pid)
        while store.run_dir(rid).exists() or any(c["id"] == rid for c in self.cands.values()):
            rid = rid + "x"
        entry = parse_entry(p.argv, p.cwd)
        chain = procs.ancestors(p.pid)
        run = {
            "id": rid, "schema": 1, "state": "candidate", "wb_version": __version__,
            "pid": p.pid, "key": p.key, "start_ts": p.start_ts, "started_at": now_iso(p.start_ts),
            "captured_at": now_iso(now), "capture_delay_s": round(now - p.start_ts, 2),
            "argv_raw": p.argv,  # 只在内存里用，落盘前删掉
            "argv": redact_argv(p.argv), "cmdline": shell_join(redact_argv(p.argv)),
            "exe": p.exe, "cwd": p.cwd, "entry": entry,
            "workdir": self.workdir_of(p.cwd, entry),
            "env": redact_env(env_raw), "ancestors": [dict(a, cmd=" ".join(redact_argv(a["cmd"].split(" ")))) for a in chain],
            "members": [p.key], "children": [], "tags": [],
            "seeds": detect_seeds(p.argv, env_raw, self.cfg["seed_fields"]),
            "user": os.environ.get("USER") or str(self.uid),
        }
        if parent_run:
            run["parent_run"] = parent_run
        if any(x in p.cmd for x in self.cfg["service_cmd_patterns"]):
            run["tags"].append("service")
        if run["capture_delay_s"] > self.cfg["late_capture_seconds"]:
            run["tags"].append("late_capture")
        run["stdio"] = self.stdio_targets(p)
        run["_env_raw_keys"] = {k: env_raw.get(k) for k in ("PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME", "PYTHONUSERBASE") if env_raw.get(k)}
        self.cands[p.key] = run
        self.member_of[p.key] = rid
        self.pool.submit(self.safe, self.take_snapshots, run)

    def workdir_of(self, cwd: str, entry: dict) -> str | None:
        path = entry.get("script") or cwd
        for w in self.cfg["watch_roots"]:
            if snapshot.under(path, w):
                rel = os.path.relpath(path, w)
                first = rel.split(os.sep)[0]
                return os.path.join(w, first) if first not in (".", "") else w
        return None

    def stdio_targets(self, p: procs.Proc) -> dict:
        out = {}
        for fd, name in ((1, "stdout"), (2, "stderr")):
            try:
                t = os.readlink(f"/proc/{p.pid}/fd/{fd}")
            except OSError:
                continue
            item = {"target": t}
            if t.startswith("/") and not t.startswith(("/dev/", "/proc/")):
                try:
                    item["offset"] = os.path.getsize(t)
                except OSError:
                    pass
            elif t.startswith("/dev/pts") or t.startswith("/dev/tty"):
                item["note"] = "输出到终端，无法事后捕获（建议重定向到日志文件）"
            elif t.startswith("pipe:"):
                tee = self.find_tee(p)
                if tee:
                    item["tee"] = tee
                    try:
                        item["offset"] = os.path.getsize(tee)
                    except OSError:
                        pass
            out[name] = item
        return out

    def find_tee(self, p: procs.Proc) -> str | None:
        """`python x.py | tee log.txt`：在同一进程组里找 tee，拿它写的文件。"""
        for q in procs.list_user_procs(self.uid).values():
            if q.pgrp != p.pgrp or q.pid == p.pid:
                continue
            procs.fill_detail(q)
            if q.argv and os.path.basename(q.argv[0]) == "tee":
                files = [a for a in q.argv[1:] if not a.startswith("-")]
                if files:
                    f = files[0]
                    return f if os.path.isabs(f) else os.path.normpath(os.path.join(q.cwd, f))
        return None

    def take_snapshots(self, run: dict):
        roots = []
        cwd = run["cwd"]
        if self.in_watch(cwd):
            roots.append(snapshot.choose_root(cwd, self.cfg["watch_roots"]))
        sc = run["entry"].get("script")
        if sc and self.in_watch(sc):
            r = snapshot.choose_root(sc, self.cfg["watch_roots"])
            if r not in roots:
                roots.insert(0, r)
        if not roots and sc:
            roots.append((os.path.dirname(sc), "dir"))
        snaps = []
        cfgs = run["entry"].get("config_files", [])
        for root, kind in roots[:3]:
            extra = [c for c in cfgs if snapshot.under(c, root)]
            if sc and snapshot.under(sc, root):
                extra.append(sc)
            if kind == "files":
                kind = "dir"
                root = os.path.dirname(sc) if sc else root
                if root in self.cfg["watch_roots"]:
                    # 根目录本身不能整个遍历：只收脚本和配置
                    snaps.append(self.snapshot_files(root, extra, run))
                    continue
            snaps.append(self.snapshot_root(root, kind, extra, run))
        # 目录里嵌套的 git 仓库（如 baseline/methods/<别人的代码>）有未提交改动时，单独再拍一份
        for s in list(snaps):
            for n in (s.get("nested_repos") or [])[:10]:
                nd = os.path.join(s["root"], n["path"])
                if n.get("dirty") and not any(x.get("root") == nd for x in snaps) and len(snaps) < 8:
                    snaps.append(self.snapshot_root(nd, "git", [], run))
        run["_patches"] = {i: sn.pop("_patch") for i, sn in enumerate(snaps) if sn.get("_patch")}
        run["snapshots"] = snaps
        # 根目录之外的配置文件：直接复制内容
        run["config_copies"] = []
        for c in cfgs:
            if not any(snapshot.under(c, s["root"]) for s in snaps if "root" in s):
                run["config_copies"].append(c)
        run["_snap_done"] = True

    def snapshot_root(self, root: str, kind: str, extra: list, run: dict) -> dict:
        lock = self.root_locks.setdefault(root, threading.Lock())
        with lock:
            last = self.root_last.get(root)
            # 并发启动去重：已有快照是在本进程启动之后拍的、且很新，就直接复用
            if last and last["_t"] >= run["start_ts"] - 0.5 and time.time() - last["_t"] < 30 and not extra:
                s = {k: v for k, v in last.items() if k != "_t"}
                s["reused"] = True
                return s
            t = time.time()
            try:
                s = snapshot.snapshot(root, kind, extra_files=extra, message=f"{run['id']} {run['cmdline'][:200]}")
            except Exception as e:
                log(f"快照失败 {root}: {e}")
                return {"root": root, "kind": kind, "error": str(e)[:500]}
            self.root_last[root] = dict(s, _t=t)
            return s

    def snapshot_files(self, root: str, files: list, run: dict) -> dict:
        try:
            return snapshot.snapshot(root, "files", extra_files=files, message=f"{run['id']} (files)")
        except Exception as e:
            return {"root": root, "kind": "files", "error": str(e)[:500]}

    # ---------------- 升级为 Run ----------------

    def promote(self, run: dict):
        run["state"] = "running"
        run["promoted_at"] = now_iso()
        rd = store.run_dir(run["id"])
        rd.mkdir(parents=True, exist_ok=True)
        try:
            snapshot.pin(run["id"], run.get("snapshots", []))
        except Exception as e:
            log(f"pin 失败 {run['id']}: {e}")
        self.copy_configs(run)
        for i, patch in (run.get("_patches") or {}).items():
            (rd / f"uncommitted-{i}.patch").write_text(patch)
        self.runs[run["id"]] = run
        self.persist(run)
        log(f"抓到 Run {run['id']} pid={run['pid']} {run['cmdline'][:160]}")
        self.pool.submit(self.safe, self.capture_env, run)
        self.pool.submit(self.safe, self.capture_session, run)

    def copy_configs(self, run: dict):
        rd = store.run_dir(run["id"])
        out = []
        for c in run.get("config_files_copied_src", run.get("config_copies", [])):
            try:
                dst = rd / "inputs" / c.lstrip("/")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(c, dst)
                out.append({"src": c, "copy": str(dst.relative_to(rd))})
            except OSError:
                pass
        run["config_copies"] = out

    def persist(self, run: dict):
        with self.lock:
            for _ in range(5):
                try:
                    r = json.loads(json.dumps({k: v for k, v in list(run.items())
                                               if not k.startswith("_") and k != "argv_raw"}, default=str))
                    break
                except RuntimeError:  # 别的线程正好在往 run 里加字段
                    time.sleep(0.01)
            else:
                return
            store.save_run(r)

    def capture_env(self, run: dict):
        rd = store.run_dir(run["id"])
        info = envcap.system_info()
        if envcap.is_python(run["exe"], run["argv"]):
            probe = envcap.probe_python(run["exe"], run.get("_env_raw_keys", {}))
            if probe:
                (rd / "requirements.txt").write_text(envcap.requirements_txt(probe))
                pk = probe.get("packages", {})
                info.update({
                    "python": probe.get("python"), "python_exe": probe.get("executable"),
                    "prefix": probe.get("prefix"), "packages": len(pk),
                    "key_packages": {n: pk[n]["version"] for n in pk if n.lower() in (
                        "torch", "transformers", "vllm", "sglang", "peft", "trl", "accelerate", "deepspeed",
                        "datasets", "openai", "anthropic", "numpy", "flash-attn", "flash_attn", "xformers", "jax")},
                })
        devs = procs.gpu_devices()
        self.gpu_dev = devs
        info["gpus"] = [{"index": d["index"], "name": d["name"], "mem_total": d["mem_total"]} for d in devs.values()]
        run["system"] = info
        self.persist(run)

    def capture_session(self, run: dict):
        try:
            li = sessions.link(run["start_ts"], run.get("argv_raw") or run["argv"], run["cwd"], run.get("ancestors", []))
        except Exception as e:
            li = {"error": str(e)[:300]}
        if li:
            li.pop("call_text", None) if li.get("confidence") == "none" else None
            if li.get("call_text"):
                li["call_text"] = " ".join(redact_argv(li["call_text"].split(" ")))[:1500]
        run["session"] = li
        self.persist(run)

    # ---------------- 运行中 ----------------

    def observe(self, run: dict, ps: dict, gpu: dict, now: float):
        members = [m for m in run.get("members", []) if int(m.split(":")[0]) in ps]
        if self.tick_no % 2 == 0:
            writes = run.setdefault("_writes", set())
            reads = run.setdefault("_reads", {})
            for m in members:
                pid = int(m.split(":")[0])
                for path, mode, _fd in procs.open_files(pid):
                    if mode in ("w", "rw"):
                        writes.add(path)
                    elif path not in reads and self.in_interest(path):
                        reads[path] = None
        if self.tick_no % 15 == 0:
            reads = run.setdefault("_reads", {})
            for m in members[:8]:
                for path in procs.mapped_files(int(m.split(":")[0])):
                    if path not in reads and self.in_interest(path) and "/site-packages/" not in path \
                            and not path.endswith(".so") and ".so." not in path:
                        reads[path] = None
        # GPU 采样：每 10 秒
        if self.tick_no % 5 == 0:
            mem = 0.0
            uuids = set()
            for m in members:
                for uuid, mm in gpu.get(int(m.split(":")[0]), []):
                    mem += mm
                    uuids.add(uuid)
            if uuids:
                if not self.gpu_dev or self.tick_no % 15 == 0:
                    self.gpu_dev = procs.gpu_devices()
                idx = sorted(self.gpu_dev.get(u, {}).get("index", -1) for u in uuids)
                util = [self.gpu_dev.get(u, {}).get("util") for u in uuids]
                rd = store.run_dir(run["id"])
                newf = not (rd / "gpu.csv").exists()
                with open(rd / "gpu.csv", "a") as f:
                    if newf:
                        f.write("time,gpus,proc_mem_mb,gpu_util\n")
                    u = [x for x in util if x is not None]
                    f.write(f"{now_iso(now)},{'|'.join(map(str, idx))},{mem:.0f},{(sum(u) / len(u)) if u else ''}\n")
                g = run.setdefault("gpu", {"devices": [], "peak_mem_mb": 0})
                g["devices"] = sorted(set(g["devices"]) | set(idx))
                g["peak_mem_mb"] = max(g["peak_mem_mb"], round(mem))
                if "gpu" not in run["tags"]:
                    run["tags"].append("gpu")
        if self.tick_no % 30 == 0:
            run["last_seen"] = now_iso(now)
            self.persist(run)

    def in_interest(self, path: str) -> bool:
        if path.startswith(tuple(self.cfg["artifact_ignore_prefixes"])):
            return False
        try:
            return os.path.isfile(path) and os.path.getsize(path) >= 10 * 1024 * 1024
        except OSError:
            return False

    def is_artifact_path(self, path: str, run: dict) -> bool:
        if path.startswith(tuple(self.cfg["artifact_ignore_prefixes"])):
            return False
        if any(x in path for x in self.cfg["artifact_ignore_patterns"]):
            return False
        if path.startswith(str(store.home())):
            return False
        logs = {v.get("target") for v in run.get("stdio", {}).values()} | {v.get("tee") for v in run.get("stdio", {}).values()}
        if path in logs:
            return False
        return True

    # ---------------- 结束 ----------------

    def finalize(self, run: dict):
        end = time.time()
        run["state"] = "ended"
        run["ended_at"] = now_iso(end)
        run["duration_s"] = round(end - run["start_ts"], 1)
        rd = store.run_dir(run["id"])
        rd.mkdir(parents=True, exist_ok=True)
        # 1) 日志
        tail = []
        for name, item in run.get("stdio", {}).items():
            src = item.get("tee") or item.get("target")
            if not src or not src.startswith("/") or src.startswith(("/dev/", "/proc/")):
                continue
            if name == "stderr" and src == (run["stdio"].get("stdout", {}).get("tee") or run["stdio"].get("stdout", {}).get("target")):
                item["same_as"] = "stdout"
                continue
            dst = rd / f"{name}.log"
            try:
                self.copy_range(src, dst, item.get("offset", 0))
                item["copy"] = dst.name
                tail = self.tail_lines(dst, 80) or tail
            except OSError as e:
                item["copy_error"] = str(e)
        run["log_tail"] = tail[-80:]
        run.setdefault("exit", {"code": None})
        run["exit"]["guess"] = self.guess_exit(run["exit"].get("code"), tail)
        # 2) 读入的大文件（模型 / 数据）：记指纹
        inputs = []
        for path in sorted(run.get("_reads", {}))[:200]:
            try:
                fp = fingerprint(path)
                inputs.append({"path": path, **fp})
            except OSError:
                pass
        run["inputs"] = inputs
        # 3) 产物
        self.collect_outputs(run, end)
        # 4) wb.log() 指标
        self.merge_spool(run)
        # 5) 候选指标：从写出的小 json 里提取数值
        run["metrics_auto"] = self.auto_metrics(run)
        self.persist(run)
        log(f"Run 结束 {run['id']} 用时 {run['duration_s']}s 产物 {run.get('outputs_summary', {}).get('files', 0)} 个")

    @staticmethod
    def copy_range(src: str, dst: Path, offset: int):
        size = os.path.getsize(src)
        if offset > size:
            offset = 0  # 日志被截断 / 重建过
        n = size - offset
        cap_head, cap_tail = 20 << 20, 100 << 20
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            fi.seek(offset)
            if n <= cap_head + cap_tail:
                shutil.copyfileobj(fi, fo, 1 << 20)
            else:
                fo.write(fi.read(cap_head))
                fo.write(f"\n\n…… [workbench] 中间省略 {n - cap_head - cap_tail} 字节 ……\n\n".encode())
                fi.seek(size - cap_tail)
                shutil.copyfileobj(fi, fo, 1 << 20)

    @staticmethod
    def tail_lines(path: Path, n: int) -> list:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                sz = f.tell()
                f.seek(max(0, sz - 64 * 1024))
                data = f.read().decode("utf-8", "replace")
            lines = data.replace("\r", "\n").splitlines()
            return [l[-500:] for l in lines if l.strip()][-n:]
        except OSError:
            return []

    @staticmethod
    def guess_exit(code, tail: list) -> str:
        if code is not None:
            if code == 0:
                return "ok"
            if code < 0 or code in (137, 143):
                return "killed"
            return "error"
        text = "\n".join(tail[-30:])
        if re.search(r"Traceback \(most recent call last\)|Error:|Exception:|CUDA out of memory|OutOfMemoryError", text):
            if re.search(r"KeyboardInterrupt", text):
                return "interrupted"
            return "error?"
        if re.search(r"\bKilled\b|OOM|oom-kill", text):
            return "killed?"
        return "ok?" if tail else "unknown"

    def collect_outputs(self, run: dict, end: float):
        cfg = self.cfg
        start = run["start_ts"] - 1
        files = set()
        for p in run.get("_writes", set()):
            if self.is_artifact_path(p, run) and os.path.isfile(p):
                files.add(p)
        # 只扫"确实写过东西的目录" + --output_dir 类参数指向的目录，避免把并发 Run 的产物算进来
        scan_dirs = {os.path.dirname(p) for p in files}
        scan_dirs |= set(output_dir_args(run.get("argv_raw") or run["argv"], run["cwd"]))
        for d in list(scan_dirs)[:50]:
            if not self.is_artifact_path(d + "/", run):
                continue
            n = 0
            for dirpath, dirnames, filenames in os.walk(d):
                dirnames[:] = [x for x in dirnames if x not in (".git", "__pycache__")]
                for fn in filenames:
                    n += 1
                    if n > 20000:
                        break
                    fp = os.path.join(dirpath, fn)
                    try:
                        st = os.lstat(fp)
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode) and start <= st.st_mtime <= end + 5 and self.is_artifact_path(fp, run):
                        files.add(fp)
                if n > 20000:
                    break
        items = []
        total = 0
        for fp in sorted(files):
            try:
                st = os.stat(fp)
            except OSError:
                continue
            items.append({"path": fp, "size": st.st_size, "mtime": now_iso(st.st_mtime), "ino": st.st_ino})
            total += st.st_size
        cap = cfg["artifact_copy_max_gb"] * (1 << 30)
        mode = "copy" if total <= cap else "hardlink"
        rd = store.run_dir(run["id"])
        if (store.load_claim(run["id"]) or {}).get("ignored"):
            mode = "none"  # 已被忽略（不是实验）：只记清单，不复制
        try:
            sv = os.statvfs(store.home())
            free = sv.f_bavail * sv.f_frsize
            if free - total < cfg["disk_warn_free_gb"] * (1 << 30):
                mode = "hardlink"  # 共用盘空间偏低：不复制，只做硬链接（零额外空间），面板会提示
                run.setdefault("tags", []).append("low_disk")
        except OSError:
            pass
        odir = rd / "outputs"
        done = 0
        for it in items:
            dst = odir / it["path"].lstrip("/")
            if mode == "none":
                it["kept"] = None
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if mode == "copy":
                    shutil.copy2(it["path"], dst)
                else:
                    os.link(it["path"], dst)
                os.chmod(dst, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                it["kept"] = mode
                done += 1
            except OSError as e:
                it["kept"] = None
                it["error"] = str(e)[:200]
        # 目录也设只读（ADR-0004：防止被覆盖；删除只能走 wb rm-artifacts）
        if odir.exists():
            for dirpath, dirnames, _ in os.walk(odir, topdown=False):
                try:
                    os.chmod(dirpath, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
                except OSError:
                    pass
        run["outputs"] = items[:2000]
        run["outputs_summary"] = {"files": len(items), "bytes": total, "mode": mode, "kept": done,
                                  "truncated": len(items) > 2000}
        if mode == "hardlink":
            run["tags"].append("artifacts_hardlinked")

    def merge_spool(self, run: dict):
        sp = store.home() / "spool"
        if not sp.exists():
            return
        rd = store.run_dir(run["id"])
        lines = []
        for m in run.get("members", []):
            f = sp / (m.replace(":", "-") + ".jsonl")
            if f.exists():
                lines += f.read_text(errors="replace").splitlines()
                try:
                    f.unlink()
                except OSError:
                    pass
        if lines:
            with open(rd / "metrics.jsonl", "a") as fo:
                fo.write("\n".join(lines) + "\n")

    def auto_metrics(self, run: dict) -> dict:
        out = {}
        rx = re.compile(r"(result|metric|eval|score|summary|acc|perf|stat|report)", re.I)
        for it in run.get("outputs", []):
            p = it["path"]
            if not p.endswith(".json") or it["size"] > 5 * 1024 * 1024 or not rx.search(os.path.basename(p)):
                continue
            try:
                data = json.loads(Path(p).read_text())
            except (OSError, ValueError):
                continue
            flat = {}

            def walk(x, prefix, depth):
                if len(flat) >= 100 or depth > 3:
                    return
                if isinstance(x, bool):
                    return
                if isinstance(x, (int, float)):
                    flat[prefix or "value"] = x
                elif isinstance(x, dict):
                    for k, v in list(x.items())[:200]:
                        walk(v, f"{prefix}.{k}" if prefix else str(k), depth + 1)

            walk(data, "", 0)
            if flat:
                out[p] = flat
            if len(out) >= 20:
                break
        return out

    # ---------------- 状态 / 心跳 ----------------

    def heartbeat(self):
        write_json(store.daemon_dir() / "heartbeat.json", {
            "ts": now_iso(), "epoch": time.time(), "pid": self.mypid, "version": __version__,
            "running": len(self.runs), "candidates": len(self.cands), "tick": self.tick_no,
        })

    def save_state(self):
        with self.lock:
            st = {"runs": {rid: {"key": r["key"], "members": r.get("members", [])} for rid, r in self.runs.items()},
                  "saved_at": now_iso()}
        write_json(store.daemon_dir() / "state.json", st)
        for r in list(self.runs.values()):
            r2 = dict(r)
            r2["writes_seen"] = sorted(r.get("_writes", set()))[:5000]
            r2["reads_seen"] = sorted(r.get("_reads", {}))[:500]
            self.persist(r2)

    def restore_state(self):
        st = read_json(store.daemon_dir() / "state.json", {}) or {}
        n = 0
        for rid in list(st.get("runs", {}))[:500]:
            run = read_json(store.run_dir(rid) / "run.json")
            if not run or run.get("state") != "running":
                continue
            run["_writes"] = set(run.pop("writes_seen", []))
            run["_reads"] = {p: None for p in run.pop("reads_seen", [])}
            run["argv_raw"] = run.get("argv", [])
            self.runs[rid] = run
            for m in run.get("members", []):
                self.member_of[m] = rid
            n += 1
        # 其它残留在 running 状态、但不在 state 里的 Run：标记为 lost（守护进程挂掉期间结束的）
        for rid in store.all_run_ids()[:2000]:
            if rid in self.runs:
                continue
            p = store.run_dir(rid) / "run.json"
            run = read_json(p)
            if run and run.get("state") == "running":
                pr = procs.read_proc(run["pid"], need_detail=False)
                if pr is not None and pr.key == run["key"]:
                    run["_writes"], run["_reads"], run["argv_raw"] = set(), {}, run.get("argv", [])
                    self.runs[rid] = run
                    self.member_of[run["key"]] = rid
                    n += 1
                else:
                    run["state"] = "ended"
                    run["ended_at"] = run.get("last_seen")
                    run.setdefault("tags", []).append("ended_while_daemon_down")
                    store.save_run(run)
        if n:
            log(f"恢复跟踪 {n} 个运行中的 Run")

    def disk_stats(self):
        out = {"ts": now_iso(), "volumes": []}
        seen = set()
        for path in [*self.cfg["watch_roots"], str(store.home())]:
            try:
                sv = os.statvfs(path)
            except OSError:
                continue
            key = (sv.f_fsid if hasattr(sv, "f_fsid") else path)
            if key in seen:
                continue
            seen.add(key)
            out["volumes"].append({"path": path, "total": sv.f_blocks * sv.f_frsize,
                                   "free": sv.f_bavail * sv.f_frsize})
        total = 0
        for dirpath, _, filenames in os.walk(store.home()):
            for fn in filenames:
                try:
                    total += os.lstat(os.path.join(dirpath, fn)).st_size
                except OSError:
                    pass
        out["workbench_bytes"] = total
        write_json(store.daemon_dir() / "disk.json", out)


def main(argv=None):
    once = "--once" in (argv or sys.argv[1:])
    d = Daemon(once=once)
    d.run_forever()
