"""可选的指标 API（ADR-0008）。

    import workbench as wb
    wb.log(step=100, loss=0.31)       # 训练曲线
    wb.log(acc=0.83, f1=0.79)         # 最终指标

不需要任何初始化，也不需要守护进程在跑：数据先写进 $WB_HOME/spool/<pid>-<starttime>.jsonl，
守护进程在 Run 结束时把它并入 runs/<ID>/metrics.jsonl。写失败只会静默跳过，绝不影响实验。
"""
from __future__ import annotations

import json
import os
import time

from . import config

_path = None


def _spool_path():
    global _path
    if _path is None:
        with open("/proc/self/stat") as f:
            s = f.read()
        pstart = s[s.rfind(")") + 2:].split()[19]
        d = config.home() / "spool"
        d.mkdir(parents=True, exist_ok=True)
        _path = d / f"{os.getpid()}-{pstart}.jsonl"
    return _path


def log(step=None, **metrics):
    try:
        rec = {"t": round(time.time(), 3)}
        if step is not None:
            rec["step"] = step
        for k, v in metrics.items():
            try:
                rec[k] = float(v)
            except (TypeError, ValueError):
                rec[k] = str(v)
        with open(_spool_path(), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
