"""英雄头像（Valve 官方立绘）层：英雄名 → 立绘 URL + Markdown。

卡片左侧的英雄头像用 Valve 官方竖版立绘
（``https://cdn.cloudflare.steamstatic.com/apps/dota2/images/heroes/<key>_vert.jpg``，
235x272）。选它而不是方块小图标（32x32）或横版立绘（256x144）的原因：
竖版比例最适合「占据左侧、竖跨三行」的版式，且原图信息量足够在手机上看清。

新英雄（破晓辰星 / 玛西 / 琼英碧灵 / 兽）没有 legacy 竖版图，只有横版立绘；
``core.icon_cache`` 在下载失败时自动改取横版并裁成竖版比例，版式保持一致。

立绘本身由 ``core.icon_cache`` 下载并缓存到本地，渲染时内联成 data URI，
因此远端 t2i 不需要访问外网 —— 这是「每次都有效渲染」的前提。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_HERO_NAMES_FILE = _ASSETS_DIR / "hero_names.json"
_HERO_IDS_FILE = _ASSETS_DIR / "hero_ids.json"

# 官方竖版立绘（legacy 路径，新英雄缺失时由 icon_cache 自动换横版）
PORTRAIT_BASE_URL = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/heroes/"

# 官方横版立绘（256x144），战报卡顶部横幅的底图。
#
# ``?kind=banner`` 是给 ``icon_cache`` 看的**用途标记**，CDN 会忽略它（已验证
# 带查询串仍返回同一张图）。之所以必须区分：icon_cache 会把英雄图统一归一化
# 成竖版 208x240 并共用 ``heroes/<key>.png``，若横幅也走那条路，256x144 的
# 横图会被裁成竖图，铺进 780x190 的横幅后构图全丢。带上标记后横幅落到
# ``banners/<key>.png``，保留横版比例。
BANNER_BASE_URL = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/"
)
BANNER_QUERY = "?kind=banner"

_INTERNAL_PREFIX = "npc_dota_hero_"
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_BY_KEY_CACHE: dict[str, dict] | None = None
_BY_ID_CACHE: dict[str, dict] | None = None


def _load_names() -> dict:
    try:
        if _HERO_NAMES_FILE.exists():
            raw = json.loads(_HERO_NAMES_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {}


def _load_ids() -> dict:
    """加载 hero_id → 内部名 的表（英雄接口拿不到时的兜底）。"""
    try:
        if _HERO_IDS_FILE.exists():
            raw = json.loads(_HERO_IDS_FILE.read_text(encoding="utf-8"))
            heroes = raw.get("heroes") if isinstance(raw, dict) else None
            if isinstance(heroes, dict):
                return heroes
    except Exception:
        pass
    return {}


def _lookup_index() -> tuple[dict[str, dict], dict[str, dict]]:
    """构建「归一化名 → 英雄条目」与「英雄 ID → 英雄条目」两张索引。"""
    global _BY_KEY_CACHE, _BY_ID_CACHE
    if _BY_KEY_CACHE is not None and _BY_ID_CACHE is not None:
        return _BY_KEY_CACHE, _BY_ID_CACHE

    by_key: dict[str, dict] = {}
    by_id: dict[str, dict] = {}

    for internal, info in _load_names().items():
        if not isinstance(info, dict):
            continue
        entry = {"n": internal, "zh": info.get("zh") or ""}
        key = _slug(internal)
        if key:
            by_key[key] = entry
        en = _slug(info.get("en") or "")
        if en:
            by_key.setdefault(en, entry)
        hid = info.get("id")
        if hid:
            by_id[str(hid)] = entry

    # 英雄接口缺失时，这份表保证英雄名/头像仍然可用
    for hid, info in _load_ids().items():
        if isinstance(info, dict) and hid not in by_id:
            by_id[str(hid)] = {"n": info.get("n") or "", "zh": info.get("zh") or ""}

    _BY_KEY_CACHE = by_key
    _BY_ID_CACHE = by_id
    return by_key, by_id


def _slug(text: str) -> str:
    """归一化成资源名（``Ember Spirit`` / ``npc_dota_hero_ember_spirit`` → ``ember_spirit``）。"""
    s = str(text or "").strip().lower()
    if s.startswith(_INTERNAL_PREFIX):
        s = s[len(_INTERNAL_PREFIX):]
    s = s.replace("'", "").replace("-", "_").replace(" ", "_")
    return _SLUG_RE.sub("_", s).strip("_")


def asset_key(hero_name: str = "", hero_id: int | str | None = None) -> str:
    """英雄名 / 内部名 / 英雄 ID → 立绘资源名；认不出来时返回空串。

    优先用英雄 ID 查表（比赛数据里英雄名可能是任意译名或缺失），
    再退回按名字匹配（内部名、英文名、中文名都能命中）。
    """
    by_key, by_id = _lookup_index()

    if hero_id not in (None, "", 0, "0"):
        entry = by_id.get(str(hero_id))
        if entry:
            key = _slug(entry.get("n") or "")
            if key:
                return key

    name = str(hero_name or "").strip()
    if not name:
        return ""

    slug = _slug(name)
    if slug and slug in by_key:
        return _slug(by_key[slug].get("n") or slug)

    # 中文名 → 内部名（hero_names.json 的「键」是内部名，值里有 zh）
    for internal, info in _load_names().items():
        if isinstance(info, dict) and (info.get("zh") or "") == name:
            return _slug(internal)

    return slug


def portrait_url(hero_name: str = "", hero_id: int | str | None = None) -> str:
    """英雄 → 官方立绘 URL；认不出英雄时返回空串。"""
    key = asset_key(hero_name, hero_id)
    return f"{PORTRAIT_BASE_URL}{key}_vert.jpg" if key else ""


def portrait_markdown(hero_name: str = "", hero_id: int | str | None = None,
                      alt: str = "") -> str:
    """英雄 → Markdown 头像；认不出英雄时退化为纯文本名（保证内容不丢）。"""
    url = portrait_url(hero_name, hero_id)
    text = alt or hero_name or ""
    if not url:
        return str(text)
    return f"![{text}]({url})"


def banner_url(hero_name: str = "", hero_id: int | str | None = None) -> str:
    """英雄 → 横幅底图 URL（横版）；认不出英雄时返回空串。"""
    key = asset_key(hero_name, hero_id)
    return f"{BANNER_BASE_URL}{key}.png{BANNER_QUERY}" if key else ""


def banner_markdown(hero_name: str = "", hero_id: int | str | None = None,
                    alt: str = "") -> str:
    """英雄 → Markdown 横幅底图；认不出英雄时退化为纯文本名。"""
    url = banner_url(hero_name, hero_id)
    text = alt or hero_name or ""
    if not url:
        return str(text)
    return f"![{text}]({url})"
