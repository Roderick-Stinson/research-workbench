"""配置：默认值 + $WB_HOME/config.json 覆盖。"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_HOME = "~/workbench-data"

DEFAULTS = {
    # 只有 cwd 或脚本位于这些目录下的进程才算候选（用 GPU 的进程例外）
    "watch_roots": [],  # 必填：wb install --watch-root <目录>
    "tick_seconds": 2,
    # 候选满足任一条件即升级为 Run：用了 GPU，或存活超过这么多秒（ADR-0009）
    "promote_after_seconds": 60,
    # 启动后多久才发现就标记为"迟到抓取"（代码可能已经被改过）
    "late_capture_seconds": 10,
    # 代码快照：超过这个大小的文件不进快照，只记路径 + 大小 + mtime
    "snapshot_max_file_mb": 20,
    # 非 git 目录：一次快照最多收多少文件（防止把结果目录当代码收进去）
    "snapshot_max_files": 20000,
    # 非 git 目录：进快照的扩展名（代码 + 配置）
    "snapshot_code_exts": [
        ".py", ".pyx", ".pxd", ".ipynb", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".json", ".jsonnet",
        ".toml", ".cfg", ".ini", ".conf", ".txt", ".md", ".rst", ".env.example", ".c", ".cc", ".cpp",
        ".cu", ".cuh", ".h", ".hpp", ".rs", ".go", ".js", ".ts", ".jinja", ".j2", ".tex", ".mk",
        "Makefile", "Dockerfile", ".lock", ".r", ".R", ".jl", ".lua", ".sql", ".proto",
    ],
    # 非 git 目录：遍历时直接跳过的目录名（输出 / 数据 / 环境）
    "snapshot_prune_dirs": [
        ".git", "__pycache__", ".venv", "venv", ".conda", "node_modules", ".ipynb_checkpoints",
        "wandb", "mlruns", "results", "outputs", "output", "logs", "log", "runs", "checkpoints",
        "ckpt", "ckpts", "saved_models", ".cache", "cache", "datasets", "huggingface_datasets",
        "Models", "models_cache", "tmp", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
        "site-packages", "dist", "build", "egg-info", ".idea", ".vscode",
    ],
    # 不进任何快照的文件模式（git 的 exclude 语法）
    "snapshot_exclude_patterns": [
        "*.safetensors", "*.bin", "*.pt", "*.pth", "*.ckpt", "*.gguf", "*.onnx", "*.h5", "*.npy",
        "*.npz", "*.arrow", "*.parquet", "*.pkl", "*.pickle", "*.tar", "*.tar.gz", "*.zip", "*.mp4",
        "*.so", "*.o", "*.pyc", "__pycache__/", ".venv/", "venv/", "wandb/", "node_modules/",
        ".ipynb_checkpoints/", "*.log", "nohup.out", ".env", "*.env", ".secrets/", "*.pem", "*.key",
    ],
    # 产物（Run 写出的文件）：总量不超过这个值就复制一份到 runs/<ID>/outputs（只读）；
    # 超过就只做硬链接（同一文件系统、零额外空间），并在面板提示"未完整保护"
    "artifact_copy_max_gb": 50,
    # 写出的这些路径不算产物
    "artifact_ignore_prefixes": ["/dev/", "/proc/", "/sys/", "/tmp/", "/run/", "/var/"],
    "artifact_ignore_patterns": [
        "/.cache/", "/__pycache__/", "/.triton/", "/.nv/", "/site-packages/", "/.git/",
        "/.codex/", "/.claude/", "/.config/", "/.local/share/", "/wandb/run-",
    ],
    # 命令行匹配到这些片段的进程不抓（工具进程，不是实验）
    "exclude_cmd_patterns": [
        ".vscode-server", "pylance", "jedi", "pyright", "ruff", "black ", "isort", "mypy",
        "ipykernel_launcher", "jupyter", "pip ", "-m pip", "uv ", "conda ", "poetry",
        "tensorboard", "http.server", "/.codex/", "/.claude/", "codex ", "claude ",
        "mcp-server", "mcp_server", "-m mcp", "workbench daemon", "-m workbench", "/workbench/bin/wb", "pre-commit",
        "setup.py egg_info", "unattended-upgr", "networkd-dispatcher", "supervisord",
        "gpustat", "nvitop", "/bin/wb ",
        # 下载工具：写出的是数据 / 模型，不是实验产物
        "/bin/hf ", "hf download", "huggingface-cli", "huggingface_hub.commands", "modelscope download",
        "modelscope.cli", "snapshot_download", "aria2c", "gdown", "/bin/wget", "/bin/curl",
    ],
    # 命令行匹配到这些片段的标记为"服务"（推理服务器等），照常抓，但面板里折叠
    "service_cmd_patterns": [
        "vllm serve", "vllm.entrypoints", "sglang.launch_server", "sglang serve", "uvicorn",
        "gunicorn", "gradio", "streamlit", "lmdeploy serve", "text-generation-launcher",
    ],
    # 子进程是这些启动器的产物时，并入父 Run（不单独成 Run）
    "launcher_cmd_patterns": [
        "torchrun", "torch.distributed.run", "torch.distributed.launch", "accelerate launch",
        "accelerate/commands", "deepspeed", "multiprocessing", "joblib", "ray::", "spawn_main",
        "forkserver", "resource_tracker", "loky",
    ],
    # 默认的"种子字段"：项目可以覆盖（ADR-0005）
    "seed_fields": ["seed", "data_seed", "shuffle_seed", "random_seed", "rng_seed", "global_seed"],
    # 会话记录位置（ADR-0009 关联对话）
    "codex_sessions": "~/.codex/sessions",
    "claude_projects": "~/.claude/projects",
    "session_search_hours": 12,
    # 外部请求代理（cron 里没有代理环境变量；wb install 时从当前环境写入 config.json）
    "proxy": None,
    "contact_email": None,
    # 面板地址（推送里的链接用）
    "panel_url": None,
    # Telegram 凭据文件（含 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）；不配就不推送
    "telegram_env": None,
    # 复现时官方代码放在哪（ADR-0023），agent 规则与 skill 里引用
    "baseline_dir": None,
    # cron 守护进程用的时区相对 UTC 的小时数（有的服务器 cron 按 UTC 调度：此时填 0，北京时间 01:00 会写成 UTC 17:00）
    "cron_utc_offset_hours": 0,
    # 磁盘告警阈值（ADR-0004：只提示，不动手）
    "disk_warn_free_gb": 300,
}


def home() -> Path:
    return Path(os.environ.get("WB_HOME", DEFAULT_HOME)).expanduser()


_cache: dict | None = None


def load(reload: bool = False) -> dict:
    global _cache
    if _cache is not None and not reload:
        return _cache
    cfg = json.loads(json.dumps(DEFAULTS))
    p = home() / "config.json"
    if p.exists():
        try:
            user = json.loads(p.read_text())
            cfg.update(user)
        except Exception:
            pass
    _cache = cfg
    return cfg
