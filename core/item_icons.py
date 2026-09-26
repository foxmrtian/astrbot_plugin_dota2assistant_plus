"""物品图标层：把比赛数据里的数字物品 ID 解析成官方图标 URL + 中文名。

背景：OpenDota / Valve 的比赛详情接口在 ``item_0`` … ``item_5`` 里只给
**数字物品 ID**（如 ``116``），既没有名字也没有图标。展示时需要：

* 中文名 —— 用于图标下方的无障碍文本，也是网络渲染失败时的兜底；
* 图标 URL —— 指向 Valve 官方 CDN 上的游戏内装备图标。

数据来源：``assets/item_ids.json``，由
``https://www.dota2.com/datafeed/itemlist?language=schinese``（官方中文名
``name_loc``）与 OpenDota ``/constants/items``（``img`` 字段，用于推导图标
文件名）离线合并生成，结构为
``{id: {"n": 内部名, "zh": 显示名, "i": 图标名（可选）}}``。

图标由远端 t2i 的浏览器加载，本插件不需要下载或缓存任何图片；
拿不到图标时 Markdown 的 alt 文本会作为兜底显示出来。
"""

from __future__ import annotations

import json
from pathlib import Path

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_ITEM_IDS_FILE = _ASSETS_DIR / "item_ids.json"

# Valve 官方 CDN 上的游戏内装备图标（88x64 的透明底 PNG）
ICON_BASE_URL = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/items/"
)

# 缓存：进程内只解析一次
_CACHE: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    """加载物品 ID 表（带缓存）。失败返回空表，调用方按缺失处理。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    data: dict[str, dict] = {}
    try:
        if _ITEM_IDS_FILE.exists():
            raw = json.loads(_ITEM_IDS_FILE.read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else None
            if isinstance(items, dict):
                data = {str(k): v for k, v in items.items() if isinstance(v, dict)}
    except Exception:
        data = {}

    _CACHE = data
    return data


def _key(item_id) -> str:
    """归一化物品 ID：只有正整数才是有效 ID，其余（None / "" / 0 / 非数字）返回空串。"""
    if item_id is None:
        return ""
    key = str(item_id).strip()
    if not key.isdigit() or key == "0":
        return ""
    return key


def _lookup(item_id) -> dict | None:
    """按物品 ID 取条目，兼容 int / str / 带空白 的输入。"""
    key = _key(item_id)
    return _load().get(key) if key else None


def item_name(item_id, fallback: str = "") -> str:
    """物品 ID → 中文显示名。

    未知但合法的数字 ID 返回 ``物品#<id>``；``fallback`` 非空时优先用它。
    空白 / 非数字输入返回空串，让调用方直接跳过这一类脏数据。
    """
    entry = _lookup(item_id)
    if entry:
        name = str(entry.get("zh") or entry.get("n") or "").strip()
        if name:
            return name
    if fallback:
        return fallback
    key = _key(item_id)
    return f"物品#{key}" if key else ""


def item_icon_url(item_id) -> str:
    """物品 ID → 官方图标 URL；未知 ID 返回空串。"""
    entry = _lookup(item_id)
    if not entry:
        return ""
    # ``i`` 只在图标名与内部名不同时出现（recipe 类共用一张 recipe.png）
    icon = str(entry.get("i") or entry.get("n") or "").strip()
    if not icon:
        return ""
    return f"{ICON_BASE_URL}{icon}.png"


def item_icon_markdown(item_id) -> str:
    """物品 ID → Markdown 图片。图标 URL 缺失时退化为纯文本名。"""
    name = item_name(item_id)
    if not name:
        return ""
    url = item_icon_url(item_id)
    if not url:
        return name
    return f"![{name}]({url})"


def render_items_markdown(items, empty: str = "无") -> str:
    """把一组物品 ID 渲染成一行 Markdown（图标 + 空格分隔）。

    ``items`` 里混入 0 / None / 空串 是常态（空装备栏位），这类值会被跳过；
    全部为空时返回 ``empty``。
    """
    parts = []
    for raw in items or []:
        if raw in (None, "", 0, "0"):
            continue
        md = item_icon_markdown(raw)
        if md:
            parts.append(md)
    return " ".join(parts) if parts else empty
