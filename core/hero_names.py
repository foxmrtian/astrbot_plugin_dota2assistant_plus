"""英雄名统一层：把 OpenDota 的英文名 / 内部名 / 各种别名映射为官方中文名。

背景：OpenDota 的 ``/heroStats`` 接口返回的是英文 ``localized_name``
（如 ``Ember Spirit``），早期版本还可能是 ``npc_dota_hero_ember_spirit``
这类内部名。展示给中文用户时应统一成 Valve 官方中文译名（``灰烬之灵``）。

数据来源：``assets/hero_names.json``，由
``https://www.dota2.com/datafeed/herolist?language=schinese`` 生成，
结构为 ``{internal_name: {"en": ..., "zh": ..., "id": ...}}``。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_HERO_NAMES_FILE = _ASSETS_DIR / "hero_names.json"

# 缓存：进程内只解析一次
_CACHE: dict | None = None
# 数字 ID → 中文名的缓存（picks_bans 只有 hero_id）
_ID_INDEX_CACHE: dict[int, str] | None = None


def _load() -> dict:
    """加载官方英雄名表（带缓存）。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    data: dict = {}
    try:
        if _HERO_NAMES_FILE.exists():
            raw = json.loads(_HERO_NAMES_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
    except Exception:
        data = {}

    _CACHE = data
    return data


def _norm(text: str) -> str:
    """归一化：小写、去掉 npc_dota_hero_ 前缀、把空格/下划线/连字符统一掉。

    这样 ``Ember Spirit`` / ``ember_spirit`` / ``npc_dota_hero_ember_spirit``
    都会归一到 ``emberspirit``。
    """
    s = str(text or "").strip().lower()
    if s.startswith("npc_dota_hero_"):
        s = s[len("npc_dota_hero_"):]
    return re.sub(r"[\s_\-'.]+", "", s)


def _build_index() -> dict[str, str]:
    """构建 归一化键 -> 官方中文名 的索引。

    同时登记内部名、英文名和英文别名（Valve 改名前的旧称，例如
    ``Windrunner`` -> ``Windranger``、``Necrolyte`` -> ``Necrophos``）。
    """
    data = _load()
    index: dict[str, str] = {}

    for internal, info in data.items():
        if not isinstance(info, dict):
            continue
        zh = info.get("zh") or ""
        if not zh:
            continue
        index[_norm(internal)] = zh
        en = info.get("en") or ""
        if en:
            index[_norm(en)] = zh

    # Valve 历史改名：旧英文名指向新英雄
    for alias, internal in _LEGACY_EN_ALIASES.items():
        info = data.get(internal)
        if isinstance(info, dict) and info.get("zh"):
            index[_norm(alias)] = info["zh"]

    return index


# Valve 历史曾用英文名 -> 当前内部名
_LEGACY_EN_ALIASES = {
    "Windrunner": "npc_dota_hero_windrunner",
    "Necrolyte": "npc_dota_hero_necrolyte",
    "Obsidian Destroyer": "npc_dota_hero_obsidian_destroyer",
    "Outworld Destroyer": "npc_dota_hero_obsidian_destroyer",
    "Magnataur": "npc_dota_hero_magnataur",
    "Magnus": "npc_dota_hero_magnataur",
    "Soul Keeper": "npc_dota_hero_nevermore",
    "Shadow Fiend": "npc_dota_hero_nevermore",
    "Lifestealer": "npc_dota_hero_life_stealer",
    "Doom Bringer": "npc_dota_hero_doom_bringer",
    "Doom": "npc_dota_hero_doom_bringer",
    "Wisp": "npc_dota_hero_io",
    "Io": "npc_dota_hero_io",
    "Timbersaw": "npc_dota_hero_shredder",
    "Shredder": "npc_dota_hero_shredder",
    "Treant": "npc_dota_hero_treant",
    "Clockwerk": "npc_dota_hero_rattletrap",
    "Rattletrap": "npc_dota_hero_rattletrap",
    "Windranger": "npc_dota_hero_windrunner",
}

_INDEX_CACHE: dict[str, str] | None = None


def _index() -> dict[str, str]:
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        _INDEX_CACHE = _build_index()
    return _INDEX_CACHE


def all_hero_names(min_len: int = 2) -> list[str]:
    """返回全部官方中文英雄名，按长度降序（长的优先匹配）。

    用于在**非对局卡片**（近期战绩表、英雄档案、英雄名单等）里找出点评中
    可能提到的英雄名并加粗 —— 那些卡片没有 ``### ![英雄](立绘)`` 标题行，
    取不到英雄名，只能按名字表去认。

    ``min_len`` 默认 2：英雄名里有三个单字名（陈 / 凯 / 獸），
    单字在中文里太容易误伤（「陈述」「凯旋」都会被当成英雄），
    宁可不加粗，也不要在正文里冒出莫名其妙的粗体。
    """
    names = [
        info["zh"]
        for info in _load().values()
        if isinstance(info, dict) and info.get("zh")
    ]
    uniq = sorted({n for n in names if len(n) >= max(2, int(min_len))},
                  key=len, reverse=True)
    return uniq


def hero_name_by_id(hero_id: int | str | None, fallback: str = "") -> str:
    """按数字英雄 ID 取官方中文名。

    ``picks_bans`` 里只有 ``hero_id``（如 22），没有名字，需要这张表才能把
    BP 阵容渲染成可读文本。数据同样来自 ``assets/hero_names.json`` 的 ``id`` 字段
    （与 Valve 官方 herolist 一致，127/127 全覆盖）。
    """
    global _ID_INDEX_CACHE
    if _ID_INDEX_CACHE is None:
        idx: dict[int, str] = {}
        for entry in _load().values():
            if not isinstance(entry, dict):
                continue
            hid, zh = entry.get("id"), entry.get("zh")
            if isinstance(hid, int) and zh:
                idx[hid] = zh
        _ID_INDEX_CACHE = idx

    try:
        key = int(hero_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback or str(hero_id or "")

    hit = _ID_INDEX_CACHE.get(key)
    if hit:
        return hit
    return fallback or f"英雄#{key}"


def to_chinese(name: str | None, fallback: str = "") -> str:
    """把任意英雄名转换为官方中文名。

    已经是中文（或无法识别）时原样返回，避免把中文名弄丢。
    """
    raw = str(name or "").strip()
    if not raw:
        return fallback or raw

    # 已包含中文：认为已经是中文名
    if any("\u4e00" <= ch <= "\u9fff" for ch in raw):
        return raw

    hit = _index().get(_norm(raw))
    if hit:
        return hit

    # 形如 Hero#35 的占位符保留
    return raw


def localize_team_names(text: str) -> str:
    """占位：批量替换暂不需要，保留给未来扩展。"""
    return text


def _candidate_keys(hero) -> set[str]:
    """一个英雄对象的所有可比对名字（归一化后）。"""
    keys = set()
    for attr in ("localized_name", "name"):
        value = getattr(hero, attr, None)
        if value:
            keys.add(_norm(value))
    internal = getattr(hero, "name", "") or ""
    if internal.startswith("npc_dota_hero_"):
        keys.add(_norm(internal))
    return keys


def match_hero(heroes, query: str):
    """在英雄列表中定位查询目标，找不到返回 None。

    匹配顺序（由严到宽）：

    1. 中文名 / 内部名 / 英文名 / 英文别名 精确匹配
    2. 内部名后缀（``ember_spirit``）精确匹配
    3. 中文名包含关系（唯一命中才算）
    """
    raw = str(query or "").strip()
    if not raw or not heroes:
        return None

    # 1. 归一化精确匹配：中文输入经 _norm 后仍是中文，可与 localized_name 对上
    target = _norm(raw)
    for hero in heroes:
        if target in _candidate_keys(hero):
            return hero

    # 2. 英文输入先翻译成中文，再回表比对（localized_name 已是中文）
    zh = to_chinese(raw)
    if zh and zh != raw:
        zh_norm = _norm(zh)
        for hero in heroes:
            if zh_norm in _candidate_keys(hero):
                return hero

    # 3. 包含关系：唯一命中才采用，避免「火」这类短词误匹配
    lowered = raw.lower()
    hits = [
        hero
        for hero in heroes
        if lowered in str(getattr(hero, "localized_name", "") or "").lower()
        or lowered in str(getattr(hero, "name", "") or "").lower()
    ]
    if len(hits) == 1:
        return hits[0]

    return None


def match_heroes_fuzzy(heroes, query: str) -> list:
    """返回所有包含关系的候选，用于「找到多个英雄」的提示。"""
    lowered = str(query or "").strip().lower()
    if not lowered or not heroes:
        return []
    return [
        hero
        for hero in heroes
        if lowered in str(getattr(hero, "localized_name", "") or "").lower()
        or lowered in str(getattr(hero, "name", "") or "").lower()
    ]
