"""还原现场（ADR-0003）：生成一个独立目录，不碰原工作区。

- root 是 git 仓库且当时的 HEAD 还在：clone 原仓库 → checkout 当时的 HEAD → 叠加快照。
  于是在还原目录里 `git status` / `git diff` 看到的，正好是当时没提交的改动。
- 否则：直接把快照解出来。
- 另外生成 requirements.txt、RESTORE.md（还原说明 + 输入文件是否变过）、rerun.sh（一键重跑）。
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from . import config, snapshot, store
from .util import REDACTED, fingerprint, human_bytes

RERUN_ENV_PREFIX = ("CUDA_VISIBLE_DEVICES", "PYTHONPATH", "PYTHONHASHSEED", "HF_", "HUGGINGFACE_", "TRANSFORMERS_",
                    "VLLM_", "SGLANG_", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "TOKENIZERS_PARALLELISM", "WANDB_MODE",
                    "WANDB_PROJECT", "NCCL_", "TORCH_", "CUBLAS_WORKSPACE_CONFIG", "SEED", "OPENAI_BASE_URL",
                    "OPENAI_API_BASE", "ANTHROPIC_BASE_URL", "MODEL", "DATASET", "TASK", "EXP_", "RUN_")


def default_dest(run_id: str) -> Path:
    base = config.load().get("restore_dir") or str(Path(config.load()["watch_roots"][0]) / "workbench-restore")
    return Path(base) / run_id


def _git(args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def restore(run_id: str, dest: str | None = None, use_git_history: bool = True) -> dict:
    run = store.load_run(run_id)
    if run is None:
        raise SystemExit(f"找不到 Run {run_id}")
    snaps = [s for s in run.get("snapshots", []) if s.get("commit")]
    if not snaps:
        raise SystemExit(f"{run_id} 没有代码快照，无法还原（{[s.get('error') for s in run.get('snapshots', [])]}）")
    dest = Path(dest) if dest else default_dest(run_id)
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"目标目录已存在且非空：{dest}（换个目录，或先自己删掉）")
    dest.mkdir(parents=True, exist_ok=True)
    report = {"dest": str(dest), "roots": []}
    multi = len(snaps) > 1
    root_map = {}
    for s in snaps:
        sub = dest / Path(s["root"]).name if multi else dest
        sub.mkdir(parents=True, exist_ok=True)
        how = "snapshot"
        head = (s.get("git") or {}).get("head")
        if use_git_history and s.get("kind") == "git" and head and \
                _git(["-C", s["root"], "cat-file", "-e", f"{head}^{{commit}}"]).returncode == 0:
            r = _git(["clone", "-q", "--no-checkout", s["root"], str(sub)])
            if r.returncode == 0 and _git(["-C", str(sub), "checkout", "-q", "--detach", head]).returncode == 0:
                head_files = set(_git(["-C", str(sub), "ls-files"]).stdout.splitlines())
                snap_files = {x["path"] for x in snapshot.ls_tree(s["commit"])}
                skipped = {x["path"] for x in s.get("skipped", [])}
                for f in head_files - snap_files - skipped:
                    try:
                        (sub / f).unlink()  # 当时在工作区里被删掉的文件
                    except OSError:
                        pass
                how = f"git clone + checkout {head[:10]} + 叠加未提交改动"
            else:
                shutil.rmtree(sub, ignore_errors=True)
                sub.mkdir(parents=True, exist_ok=True)
        n = snapshot.extract(s["commit"], str(sub))
        root_map[s["root"]] = str(sub)
        report["roots"].append({"root": s["root"], "dest": str(sub), "files": n, "how": how,
                                "skipped": s.get("skipped_count", 0)})

    rd = store.run_dir(run_id)
    if (rd / "requirements.txt").exists():
        shutil.copy2(rd / "requirements.txt", dest / "requirements.txt")
    inputs_dir = rd / "inputs"
    if inputs_dir.exists():
        shutil.copytree(inputs_dir, dest / "_inputs", dirs_exist_ok=True)

    def remap(path: str) -> str:
        for orig, new in sorted(root_map.items(), key=lambda x: -len(x[0])):
            if snapshot.under(path, orig):
                return os.path.normpath(os.path.join(new, os.path.relpath(path, orig)))
        return path

    cwd_new = remap(run["cwd"])
    argv = list(run.get("argv", []))
    argv2 = [argv[0]] + [remap(a) if a.startswith("/") else a for a in argv[1:]] if argv else []
    env_lines = []
    for k, v in sorted((run.get("env") or {}).items()):
        if k.startswith(RERUN_ENV_PREFIX):
            if v == REDACTED or REDACTED in v:
                env_lines.append(f"# export {k}=...   # 已脱敏，请自行填写")
            else:
                env_lines.append(f"export {k}={shlex.quote(remap(v) if k == 'PYTHONPATH' else v)}")
    redacted_args = any(REDACTED in a for a in argv)
    sysinfo = run.get("system") or {}
    rerun = [
        "#!/usr/bin/env bash",
        f"# 由 workbench 生成：重跑 {run_id}（原始启动时间 {run.get('started_at')}）",
        "# 默认用当时的解释器；如果它的环境已经变了，先按 RESTORE.md 重建环境，再把 PY 改成新的解释器。",
        "set -euo pipefail",
        f"PY={shlex.quote(run.get('exe') or 'python')}",
        *env_lines,
        f"cd {shlex.quote(cwd_new)}",
    ]
    if redacted_args:
        rerun.append("# 注意：命令行里有已脱敏的参数（<redacted>），请先替换成真实值")
    rerun.append('exec "$PY" ' + " ".join(shlex.quote(a) for a in argv2[1:]))
    (dest / "rerun.sh").write_text("\n".join(rerun) + "\n")
    os.chmod(dest / "rerun.sh", 0o755)

    # 输入文件现在还是不是当时那份
    input_checks = []
    for it in run.get("inputs", []):
        p = it["path"]
        try:
            fp = fingerprint(p)
            same = fp["size"] == it.get("size") and fp["quick_sha256"] == it.get("quick_sha256")
            input_checks.append((p, "未变" if same else "⚠ 已改变", it.get("size")))
        except OSError:
            input_checks.append((p, "⚠ 已不存在", it.get("size")))

    md = [f"# 还原：{run_id}", "",
          f"- 原始命令：`{run.get('cmdline')}`",
          f"- 原始 cwd：`{run['cwd']}` → 还原后：`{cwd_new}`",
          f"- 启动时间：{run.get('started_at')}（抓取延迟 {run.get('capture_delay_s')}s）",
          f"- 解释器：`{run.get('exe')}`，python {sysinfo.get('python', '?')}，prefix `{sysinfo.get('prefix', '?')}`",
          ""]
    claim = run.get("claim") or {}
    if claim:
        md += [f"- 配置组：{claim.get('variant')}；目的：{claim.get('purpose')}；结论：{claim.get('conclusion')}", ""]
    md += ["## 代码", ""]
    for r in report["roots"]:
        md.append(f"- `{r['root']}` → `{r['dest']}`：{r['files']} 个文件（{r['how']}）；未进快照的大文件 {r['skipped']} 个")
    if any("late_capture" in t for t in run.get("tags", [])):
        md.append(f"- ⚠ 这次是迟到抓取（启动 {run.get('capture_delay_s')}s 后才拍快照），代码可能和启动时略有出入")
    md += ["", "## 环境", "",
           "当时的包列表在 `requirements.txt`。重建示例：", "",
           "```bash",
           f"uv venv .venv -p {sysinfo.get('python', '3.x')} && source .venv/bin/activate",
           "uv pip install -r requirements.txt   # 或 pip install -r requirements.txt",
           "```", ""]
    if sysinfo.get("key_packages"):
        md.append("关键包：" + "，".join(f"{k}=={v}" for k, v in sysinfo["key_packages"].items()))
        md.append("")
    md += ["## 输入文件（模型 / 数据，只记指纹，不复制）", ""]
    if input_checks:
        for p, st, sz in input_checks:
            md.append(f"- {st}：`{p}`（{human_bytes(sz)}）")
    else:
        md.append("- 没有观察到读入的大文件")
    outs = run.get("outputs_summary") or {}
    md += ["", "## 产物", "",
           f"- 当时写出 {outs.get('files', 0)} 个文件，共 {human_bytes(outs.get('bytes'))}，"
           f"副本在 `{rd / 'outputs'}`（{outs.get('mode', '-')}，只读）", "",
           "## 重跑", "", "```bash", f"bash {dest / 'rerun.sh'}", "```", ""]
    (dest / "RESTORE.md").write_text("\n".join(md))
    report["rerun"] = str(dest / "rerun.sh")
    report["cwd"] = cwd_new
    store.journal("restore", run=run_id, dest=str(dest))
    return report
