"""代码快照（ADR-0003 / ADR-0012）。

把工作区（含未提交、untracked 的文件）存进一个**集中的** bare 仓库 `$WB_HOME/snapshots.git`：
- 用 GIT_WORK_TREE=<root> + 每个 root 一个持久 index，不碰 root 自己的 .git、不切分支、不动 stash；
- 持久 index 让 git 只重新哈希改过的文件，第二次以后的快照通常 < 1 秒；
- git 自动去重，同一份代码跑 100 个种子只存一份。

快照范围：
- root 是 git 仓库：用户仓库跟踪的文件 + 未被 .gitignore 忽略的 untracked 文件；
- root 不是 git 仓库：遍历目录，跳过结果 / 数据 / 环境目录，只收代码和配置类扩展名；
- 两种情况都跳过大文件（只记路径 + 大小 + mtime）和明显的权重 / 数据格式。
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import os
import subprocess
import tarfile
import time
from pathlib import Path

from . import config
from .util import now_iso

GIT_IDENT = {
    "GIT_AUTHOR_NAME": "workbench", "GIT_AUTHOR_EMAIL": "workbench@localhost",
    "GIT_COMMITTER_NAME": "workbench", "GIT_COMMITTER_EMAIL": "workbench@localhost",
}


def repo_path() -> Path:
    return config.home() / "snapshots.git"


def _git(args, env=None, input=None, cwd=None, check=True, timeout=600) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.pop("GIT_DIR", None)
    e.pop("GIT_WORK_TREE", None)
    e.pop("GIT_INDEX_FILE", None)
    e.update(GIT_IDENT)
    e["GIT_LITERAL_PATHSPECS"] = "1"
    e["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        e.update(env)
    r = subprocess.run(["git", *args], env=e, input=input, cwd=cwd, capture_output=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} 失败：{r.stderr.decode('utf-8', 'replace')[-800:]}")
    return r


def ensure_repo() -> Path:
    rp = repo_path()
    if not (rp / "HEAD").exists():
        rp.parent.mkdir(parents=True, exist_ok=True)
        _git(["init", "--bare", "-q", str(rp)])
        _git(["--git-dir", str(rp), "config", "gc.auto", "0"])  # 我们自己决定什么时候 gc
        _git(["--git-dir", str(rp), "config", "core.bigFileThreshold", "20m"])
    if not (rp / "wb-index").is_dir():  # 面板以只读方式挂载时不能 mkdir
        (rp / "wb-index").mkdir(exist_ok=True)
    return rp


# ---------------- 选 root ----------------


def git_toplevel(path: str) -> str | None:
    """向上找最近的 .git（目录或文件），找不到返回 None。纯文件系统操作，不跑 git。"""
    p = Path(path)
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return str(d)
        if str(d) == "/":
            break
    return None


def under(path: str, root: str) -> bool:
    path = os.path.normpath(path)
    root = os.path.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def choose_root(path: str, watch_roots: list) -> tuple:
    """返回 (root, kind)。kind: git / dir / files。

    - 最近的 git 仓库（不越过 watch root）；
    - 否则 watch root 下的第一级目录（如 <watch_root>/baseline），按 dir 模式遍历；
    - 路径就是 watch root 本身时，只能收零散文件（files 模式）。
    """
    wr = next((w for w in watch_roots if under(path, w)), None)
    top = git_toplevel(path)
    if top and (wr is None or under(top, wr)) and os.path.normpath(top) != os.path.normpath(wr or "/nonexistent"):
        return top, "git"
    if wr is None:
        d = path if os.path.isdir(path) else os.path.dirname(path)
        return d, "dir"
    rel = os.path.relpath(path, wr)
    if rel == "." or rel.startswith(".."):
        return wr, "files"
    first = rel.split(os.sep)[0]
    cand = os.path.join(wr, first)
    if os.path.isdir(cand):
        return cand, "dir"
    return wr, "files"


# ---------------- 列文件 ----------------


def _excluded(rel: str, patterns: list) -> bool:
    name = os.path.basename(rel)
    for pat in patterns:
        if pat.endswith("/"):
            d = pat[:-1]
            if f"/{d}/" in f"/{rel}" or rel.startswith(d + "/"):
                return True
        elif fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
            return True
    return False


def _is_code(name: str, exts: list) -> bool:
    for e in exts:
        if e.startswith("."):
            if name.endswith(e):
                return True
        elif name == e:
            return True
    return False


def list_files_git(root: str, cfg: dict) -> tuple:
    """git 仓库：用户跟踪的 + untracked 未忽略的。都是只读的 git 命令（--no-optional-locks）。"""
    base = ["--no-optional-locks", "-C", root]
    tracked = _git([*base, "ls-files", "-z", "--cached"], check=False).stdout.split(b"\0")
    others = _git([*base, "ls-files", "-z", "--others", "--exclude-standard"], check=False).stdout.split(b"\0")
    tracked = [t.decode("utf-8", "surrogateescape") for t in tracked if t]
    others = [t.decode("utf-8", "surrogateescape") for t in others if t]
    note = None
    if len(others) > cfg["snapshot_max_files"]:
        note = f"untracked 文件 {len(others)} 个，超过上限，只收代码/配置类扩展名"
        others = [o for o in others if _is_code(os.path.basename(o), cfg["snapshot_code_exts"])]
    return tracked + others, note, len(others)


def list_files_dir(root: str, cfg: dict) -> tuple:
    prune = set(cfg["snapshot_prune_dirs"])
    exts = cfg["snapshot_code_exts"]
    out, nested = [], []
    note = None
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d in prune or d.endswith(".egg-info") or os.path.islink(full):
                continue
            if os.path.exists(os.path.join(full, ".git")):
                nested.append(os.path.normpath(os.path.join(rel_dir, d)))
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        for fn in filenames:
            if _is_code(fn, exts):
                out.append(os.path.normpath(os.path.join(rel_dir, fn)))
        if len(out) > cfg["snapshot_max_files"]:
            note = f"文件数超过上限 {cfg['snapshot_max_files']}，快照被截断"
            break
    return out, nested, note


# ---------------- 拍快照 ----------------


def _index_for(root: str) -> Path:
    h = hashlib.sha1(os.path.normpath(root).encode()).hexdigest()[:16]
    return ensure_repo() / "wb-index" / h


def snapshot(root: str, kind: str, extra_files: list | None = None, message: str = "") -> dict:
    """给 root 拍一个快照，返回描述快照的 dict（还没建 ref，见 pin()）。"""
    cfg = config.load()
    t0 = time.time()
    rp = ensure_repo()
    max_bytes = int(cfg["snapshot_max_file_mb"] * 1024 * 1024)
    info = {"root": root, "kind": kind, "taken_at": now_iso()}

    nested = []
    note = None
    if kind == "git":
        files, note, n_untracked = list_files_git(root, cfg)
        head = _git(["--no-optional-locks", "-C", root, "rev-parse", "HEAD"], check=False).stdout.decode().strip()
        branch = _git(["--no-optional-locks", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"], check=False).stdout.decode().strip()
        status = _git(["--no-optional-locks", "-C", root, "status", "--porcelain", "-uno"], check=False).stdout.decode()
        info["git"] = {"head": head or None, "branch": branch or None, "dirty_tracked": bool(status.strip())}
        if head:
            # 当时相对 HEAD 的未提交改动（面板直接展示；untracked 文件在快照里）
            patch = _git(["--no-optional-locks", "-C", root, "diff", "--no-ext-diff", "--no-color", "HEAD"],
                         check=False).stdout
            info["_patch"] = patch[:500_000].decode("utf-8", "replace")
            info["git"]["patch_bytes"] = len(patch)
        info["git"]["untracked"] = n_untracked
        subs = _git(["--no-optional-locks", "-C", root, "ls-files", "-s"], check=False).stdout.decode("utf-8", "replace")
        nested = [line.split("\t", 1)[1] for line in subs.splitlines() if line.startswith("160000")]
    elif kind == "dir":
        files, nested, note = list_files_dir(root, cfg)
    else:
        files = []
    for ef in extra_files or []:
        rel = os.path.relpath(ef, root)
        if not rel.startswith("..") and os.path.isfile(ef):
            files.append(rel)

    # 过滤：排除模式 + 大文件
    keep, skipped = [], []
    patterns = cfg["snapshot_exclude_patterns"]
    seen = set()
    for rel in files:
        if rel in seen:
            continue
        seen.add(rel)
        full = os.path.join(root, rel)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if not (os.path.isfile(full) or os.path.islink(full)):
            continue
        if _excluded(rel, patterns):
            if st.st_size > max_bytes:
                skipped.append({"path": rel, "size": st.st_size, "mtime": now_iso(st.st_mtime), "why": "excluded"})
            continue
        if st.st_size > max_bytes:
            skipped.append({"path": rel, "size": st.st_size, "mtime": now_iso(st.st_mtime), "why": "too_large"})
            continue
        keep.append(rel)

    env = {"GIT_DIR": str(rp), "GIT_WORK_TREE": root, "GIT_INDEX_FILE": str(_index_for(root))}
    # 同步持久 index：删掉这次不在列表里的路径，再 add 列表（git 用 stat 缓存跳过没改的文件）
    old = _git(["ls-files", "-z"], env=env, check=False).stdout.split(b"\0")
    old = {o.decode("utf-8", "surrogateescape") for o in old if o}
    gone = old - set(keep)
    if gone:
        _git(["update-index", "--force-remove", "-z", "--stdin"], env=env,
             input=b"\0".join(g.encode("utf-8", "surrogateescape") for g in gone) + b"\0")
    if keep:
        # 分批，避免参数过长；--pathspec-from-file 需要 git >= 2.25
        payload = b"\0".join(k.encode("utf-8", "surrogateescape") for k in keep) + b"\0"
        r = _git(["add", "-f", "--ignore-errors", "--pathspec-from-file=-", "--pathspec-file-nul"],
                 env=env, input=payload, check=False)
        if r.returncode not in (0, 1):
            raise RuntimeError(r.stderr.decode("utf-8", "replace")[-800:])
    tree = _git(["write-tree"], env=env).stdout.decode().strip()
    msg = (message or f"snapshot {root}").encode()
    commit = _git(["commit-tree", tree], env=env, input=msg).stdout.decode().strip()

    info.update({
        "commit": commit, "tree": tree, "files": len(keep), "nested_repos": [],
        "skipped": skipped[:300], "skipped_count": len(skipped),
        "took_s": round(time.time() - t0, 2),
    })
    if note:
        info["note"] = note
    for n in nested[:50]:
        nd = os.path.join(root, n)
        head = _git(["--no-optional-locks", "-C", nd, "rev-parse", "HEAD"], check=False).stdout.decode().strip()
        st = _git(["--no-optional-locks", "-C", nd, "status", "--porcelain"], check=False).stdout.decode()
        info["nested_repos"].append({"path": n, "head": head or None, "dirty": bool(st.strip())})
    return info


def pin(run_id: str, snaps: list) -> None:
    """Run 升级后给快照建 ref，防止被 gc。"""
    rp = ensure_repo()
    for i, s in enumerate(snaps):
        if s.get("commit"):
            _git(["--git-dir", str(rp), "update-ref", f"refs/wb/{run_id}/{i}", s["commit"]])


def unpin(run_id: str, n: int = 20) -> None:
    rp = ensure_repo()
    for i in range(n):
        _git(["--git-dir", str(rp), "update-ref", "-d", f"refs/wb/{run_id}/{i}"], check=False)


# ---------------- 读快照 ----------------


def extract(commit: str, dest: str) -> int:
    """把快照内容解到 dest（不含 .git）。返回文件数。"""
    rp = ensure_repo()
    r = _git(["--git-dir", str(rp), "archive", "--format=tar", commit])
    n = 0
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                continue
            tf.extract(m, dest)
            n += 1
    return n


def diff(a: str, b: str, stat_only: bool = False, paths: list | None = None) -> str:
    rp = ensure_repo()
    args = ["--git-dir", str(rp), "diff", "--no-color", "--find-renames"]
    if stat_only:
        args.append("--stat=200")
    args += [a, b]
    if paths:
        args += ["--", *paths]
    return _git(args, check=False).stdout.decode("utf-8", "replace")


def diff_names(a: str, b: str) -> list:
    rp = ensure_repo()
    out = _git(["--git-dir", str(rp), "diff", "--name-only", "-z", a, b], check=False).stdout
    return [x.decode("utf-8", "replace") for x in out.split(b"\0") if x]


def diff_against_live(commit: str, root: str, stat_only: bool = False) -> str:
    """快照 vs 现在的工作区：先给现在的工作区拍一个临时快照（不建 ref），再 diff。"""
    kind = "git" if os.path.exists(os.path.join(root, ".git")) else "dir"
    now = snapshot(root, kind, message="live (temporary)")
    return diff(commit, now["commit"], stat_only=stat_only)


def show_file(commit: str, path: str) -> bytes | None:
    rp = ensure_repo()
    r = _git(["--git-dir", str(rp), "show", f"{commit}:{path}"], check=False)
    return r.stdout if r.returncode == 0 else None


def ls_tree(commit: str) -> list:
    rp = ensure_repo()
    r = _git(["--git-dir", str(rp), "ls-tree", "-r", "-l", "--full-tree", commit], check=False)
    out = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        meta, path = line.split("\t", 1)
        parts = meta.split()
        out.append({"path": path, "sha": parts[2], "size": int(parts[3]) if parts[3].isdigit() else 0})
    return out
