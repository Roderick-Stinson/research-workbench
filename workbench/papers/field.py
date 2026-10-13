"""研究方向与种子（ADR-0015）。

registry/field.json  方向描述、子主题、arXiv 检索关键词与分类、维度（ADR-0021）
registry/seeds.json  种子论文：baseline 自动加入 + 本人指定
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import store as wbstore
from ..util import now_iso, read_json, write_json

# 没配置研究方向时的占位（新用户先 wb field init examples/fields/<方向>.json，或照着写一个）
DEFAULT_FIELD = {
    "id": "unset",
    "name": "（未配置研究方向）",
    "description": "",
    "subtopics": [],
    "arxiv_keywords": [],
    "arxiv_categories": ["cs.AI", "cs.CL", "cs.LG"],
    "facets": {"kind": {"label": "论文类型", "primary": True, "values": ["方法", "评测 / 基准", "分析 / 理论", "综述", "系统 / 工具"]}},
    "selection": {"max_per_day": 10, "min_quality": 6.0, "pick_window_days": 90, "require_std_baselines": True, "min_accept": 5,
                  "hf_max_per_day": 5, "hf_window_days": 7},
}

EXAMPLES = Path(__file__).resolve().parent.parent.parent / "examples" / "fields"


def field() -> dict:
    p = wbstore.registry_dir() / "field.json"
    f = read_json(p)
    return f if f is not None else dict(DEFAULT_FIELD)


def save_field(f: dict) -> None:
    write_json(wbstore.registry_dir() / "field.json", f)


def seeds() -> list:
    """[{arxiv, title, why, added_by, added_at}]。首次调用写入 5 个 baseline。"""
    p = wbstore.registry_dir() / "seeds.json"
    s = read_json(p) or []
    # 标了 baseline 且关联论文的配置组，自动是种子（ADR-0015）
    have = {x.get("arxiv") for x in s}
    for v in wbstore.all_variants():
        a = (v.get("paper") or "").replace("arxiv-", "")
        if v.get("baseline") and a and a not in have:
            s.append({"arxiv": a, "title": v.get("title"), "why": f"baseline 配置组 {v['id']}", "added_by": "variant",
                      "added_at": now_iso()})
            have.add(a)
    return s


def init_from(example: str) -> dict:
    """wb field init：从示例 / 自己写的 JSON 初始化研究方向与种子。"""
    src = Path(example)
    if not src.exists():
        src = EXAMPLES / (example if example.endswith(".json") else f"{example}.json")
    data = json.loads(src.read_text())
    seeds = data.pop("seeds", [])
    data["created"] = now_iso()
    save_field(data)
    save_seeds([dict(x, added_by="init", added_at=now_iso()) for x in seeds])
    return {"field": data.get("id"), "seeds": len(seeds), "from": str(src)}


def save_seeds(s: list) -> None:
    write_json(wbstore.registry_dir() / "seeds.json", s)


def cfg_selection() -> dict:
    return field().get("selection") or DEFAULT_FIELD["selection"]

