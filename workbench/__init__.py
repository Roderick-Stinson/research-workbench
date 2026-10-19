"""实验工作台（workbench）：自动抓现场 + 说"记录"时认领。

设计见 CONTEXT.md 与 docs/adr/。核心部分只用标准库（Python >= 3.9）。
"""

__version__ = "0.13.0"


def log(step=None, **metrics):
    """可选的 Python API：在实验代码里记录指标（ADR-0008）。不调用也不影响抓现场。"""
    from .metrics_api import log as _log

    return _log(step=step, **metrics)
