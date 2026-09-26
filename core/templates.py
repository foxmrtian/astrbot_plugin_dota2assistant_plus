"""工具输出模板，统一格式并引导 LLM 生成有价值的解读。"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .hero_icons import banner_markdown, portrait_markdown
from .item_icons import render_items_markdown, ICON_BASE_URL

# 神杖/魔晶在出装行里的识别：渲染器靠图标 URL 文件名判断这两张是否要
# 合成「上神杖下图标」的组合状态图。
AGHANIM_SCEPTER_URL = f"{ICON_BASE_URL}ultimate_scepter.png"
AGHANIM_SHARD_URL = f"{ICON_BASE_URL}aghanims_shard.png"
from .models import (
    HeroInfo,
    ItemInfo,
    LiveGame,
    MatchDetail,
    MatchPlayer,
    PlayerProfile,
    ProMatch,
    RecentMatch,
)


# ──────────────────────────────────────────────
# 卡片种类：决定末尾那一块该写什么
# ──────────────────────────────────────────────

# 只有**具体某一局**的比赛详情卡片才有双方十人的数据（天辉/夜魇各五人、
# 经济/KDA/参战率），能写「天辉方的表现 / 夜魇方的表现 / 一句话总结」这种
# 三点式赛后总结。其它查询（玩家资料与近期战绩、英雄、英雄列表、出装、
# 物品、实时比赛、职业比赛）的数据形态完全不同 —— 卡上没有分队、也没有十名
# 玩家，套用三点式只会写出与上方列表对不上的总结（用户反馈的实际问题：
# 查近期战绩时，总结仍在讲「双方队伍表现」）。
# 因此按种类换一套与卡片内容对应的点评。
KIND_MATCH = "match"
KIND_PLAYER = "player"
KIND_HERO = "hero"
KIND_HERO_LIST = "hero_list"
KIND_HERO_BUILD = "hero_build"
KIND_ITEM = "item"
KIND_LIVE = "live"
KIND_PRO = "pro"

# 卡片末尾那一块的标题。对局用「综合分析」，其余用「点评」。
# 两条渲染路径（网络 t2i 模板 JS、本地 Pillow）与产物校验都按这两个标题
# 识别这一块，必须同时认这两个（见 ANALYSIS_TITLES）。
_ANALYSIS_TITLE_MATCH = "综合分析"
_ANALYSIS_TITLE_REVIEW = "点评"
ANALYSIS_TITLES: tuple[str, ...] = (_ANALYSIS_TITLE_MATCH, _ANALYSIS_TITLE_REVIEW)

# 按 h1 认卡片种类。每种模板的 h1 都是唯一的，认它最稳 ——
# 既不用改 8 个工具的调用签名，也不用把种类一路透传到钩子，
# 而且一轮对话里合并了多张卡片时同样能认出「这里面有对局卡」。
_KIND_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (KIND_MATCH, ("# 比赛详情",)),
    (KIND_PLAYER, ("# 玩家资料",)),
    (KIND_HERO_LIST, ("# 英雄列表",)),
    (KIND_HERO, ("# 英雄：",)),
    # 出装卡片的 h1 是「# {英雄名} 出装推荐」，标记不在行首，只能按包含匹配。
    # 「出装推荐」这个词在其它卡片里不会出现，不会误伤。
    (KIND_HERO_BUILD, ("出装推荐",)),
    (KIND_ITEM, ("# 物品：",)),
    (KIND_LIVE, ("# 实时比赛",)),
    (KIND_PRO, ("# 近期职业比赛",)),
)


def analysis_title(kind: str) -> str:
    """评价区块的标题：对局用「综合分析」，其余用「点评」。

    认不出种类（空串）时按对局处理，沿用原行为。
    """
    return (
        _ANALYSIS_TITLE_MATCH
        if kind in ("", KIND_MATCH)
        else _ANALYSIS_TITLE_REVIEW
    )


def detect_kind(cards) -> str:
    """按卡片 h1 判断这是哪一种查询，认不出时返回空串。

    返回空串时调用方按**对局**处理（保守选择：沿用原行为），
    这样历史卡片、缓存卡片与测试夹具都不会因为认不出种类而改变表现。
    """
    text = "\n".join(str(c) for c in (cards or []) if c)
    if not text:
        return ""

    seen: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        # 只认 h1（"## " 不以 "# " 开头，天然排除分栏标题）
        if not line.startswith("# "):
            continue
        for kind, markers in _KIND_MARKERS:
            # 「# 英雄：」「# 比赛详情」这类标记在 h1 行首；出装卡片的
            # 「出装推荐」夹在英雄名之后，因此两种都认（标记本身足够独特，
            # 不会把别的卡片误判成出装）。
            if any(line.startswith(m) or m in line for m in markers):
                # 一轮里同时查了对局和别的：对局优先 —— 三点式赛后总结
                # 信息量最大，也能覆盖用户最关心的那一问。
                if kind == KIND_MATCH:
                    return KIND_MATCH
                if kind not in seen:
                    seen.append(kind)

    return seen[0] if seen else ""


# ──────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────


def markdown_to_plain(text: str) -> str:
    """把卡片 Markdown 降级为可读纯文本。

    比赛卡片里的装备是 ``![中文物品名](图标URL)``，直接当文本发出去会让用户
    看到一长串 CDN 链接。纯文本路径（``--text``、渲染失败兜底、斜杠命令）
    统一走这里：图片保留 alt 文本、链接保留标题、行内标记去掉。
    """
    out = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text or "")
    out = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", out)
    out = out.replace("**", "").replace("__", "").replace("`", "")
    # 装备图标之间的连续空格压缩成一个
    return re.sub(r"[ \t]{2,}", " ", out).strip()


# 玩家资料未公开时，OpenDota 既不给 personaname 也不给 account_id。
# 显式标注「匿名玩家」而不是留空：战绩卡上每个玩家的标题结构保持一致，
# 用户也不会误以为渲染缺了一块。
ANONYMOUS_PLAYER = "匿名玩家"


def fmt_big(value: int) -> str:
    """大数值压缩显示：超过 1000 用 ``k`` 表示，保留 1 位小数。

    卡片上金钱/伤害动辄五位数，原样铺开会把每行撑得很长、也不好横向对比
    （``18,586`` 与 ``15,605`` 需要逐位读）。压缩成 ``18.6k`` / ``15.6k``
    后一眼能比大小，且与 ``631`` 这类小数值排在一起也不突兀。
    """
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return str(value)
    if abs(n) > 1000:
        return f"{n / 1000:.1f}k"
    return f"{n:,}"


def fmt_duration(seconds: int) -> str:
    """格式化秒数为 H:MM:SS 或 M:SS。"""
    if seconds <= 0:
        return "未知"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_rank(rank_tier: int, leaderboard_rank: int = 0) -> str:
    """格式化段位信息。"""
    if not rank_tier:
        return "未知"
    tier = rank_tier // 10
    star = rank_tier % 10
    tier_names = {
        1: "先锋", 2: "卫士", 3: "中军", 4: "统帅",
        5: "传奇", 6: "万古流芳", 7: "超凡入圣", 8: "冠绝一世",
    }
    name = tier_names.get(tier, f"段位{tier}")
    result = f"{name} {star}星" if star else name
    if leaderboard_rank:
        result += f" (排名 #{leaderboard_rank})"
    return result


def fmt_avg_kda(matches: list[RecentMatch]) -> str:
    """计算平均 KDA。"""
    if not matches:
        return "N/A"
    total_k = sum(m.kills for m in matches)
    total_d = sum(m.deaths for m in matches)
    total_a = sum(m.assists for m in matches)
    n = len(matches)
    kda_ratio = (total_k + total_a) / max(total_d, 1)
    return f"{total_k / n:.1f}/{total_d / n:.1f}/{total_a / n:.1f} ({kda_ratio:.2f})"


# ──────────────────────────────────────────────
# 玩家资料 + 近期战绩
# ──────────────────────────────────────────────

_PLAYER_TEMPLATE = """\
# 玩家资料：{persona_name}

| 项目 | 数据 |
|------|------|
| Steam ID | {account_id} |
| 段位 | {rank} |
| 估算 MMR | {mmr} |

## 近期战绩（最近 {match_count} 场）

**总体**: {wins}胜 {losses}负（胜率 {win_rate}%）
**平均 KDA**: {avg_kda}

| 结果 | 英雄 | KDA | 时长 | GPM | 伤害 |
|------|------|-----|------|-----|------|
{match_rows}
"""


def render_player_profile(
    profile: PlayerProfile,
    recent_matches: list[RecentMatch],
    rank_str: str,
    avg_kda_str: str,
) -> str:
    """渲染玩家资料 + 近期战绩模板。"""
    wins = sum(1 for m in recent_matches if m.win)
    losses = len(recent_matches) - wins
    win_rate = f"{wins / len(recent_matches) * 100:.0f}" if recent_matches else "N/A"

    match_rows = []
    for m in recent_matches[:5]:
        result = "✅" if m.win else "❌"
        duration = fmt_duration(m.duration_seconds)
        kda = f"{m.kills}/{m.deaths}/{m.assists}"
        match_rows.append(f"| {result} | {m.hero_name} | {kda} | {duration} | {m.gpm} | {m.hero_damage:,} |")

    return _PLAYER_TEMPLATE.format(
        persona_name=profile.persona_name or str(profile.account_id),
        account_id=profile.account_id,
        rank=rank_str,
        mmr=profile.estimated_mmr or "未知",
        match_count=len(recent_matches),
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        avg_kda=avg_kda_str,
        match_rows="\n".join(match_rows) if match_rows else "| - | 暂无数据 | - | - | - | - |",
    )


# ──────────────────────────────────────────────
# 英雄信息
# ──────────────────────────────────────────────

_HERO_TEMPLATE = """\
# 英雄：{localized_name}

| 属性 | 值 |
|------|-----|
| 主属性 | {primary_attr} |
| 攻击类型 | {attack_type} |
| 定位 | {roles} |
| 力量 | {base_str} (+{str_gain}/级) |
| 敏捷 | {base_agi} (+{agi_gain}/级) |
| 智力 | {base_int} (+{int_gain}/级) |
| 基础生命/魔法 | {base_health} / {base_mana} |
| 基础护甲 | {base_armor} |
| 攻击力 | {base_attack_min}-{base_attack_max} |
| 移动速度 | {move_speed} |

{stats_section}
"""


def render_hero_info(hero: HeroInfo) -> str:
    """渲染英雄信息模板。"""
    attr_map = {"str": "力量", "agi": "敏捷", "int": "智力", "all": "全能"}

    stats_parts = []
    if hero.pub_pick > 0:
        win_rate = hero.pub_win / hero.pub_pick * 100
        stats_parts.append(f"## 天梯数据\n- 选取次数: {hero.pub_pick:,}\n- 胜率: {win_rate:.1f}%")
    if hero.pro_pick > 0:
        pro_win_rate = hero.pro_win / hero.pro_pick * 100
        stats_parts.append(f"## 职业数据\n- 选取次数: {hero.pro_pick:,}\n- 胜率: {pro_win_rate:.1f}%")
    stats_section = "\n\n".join(stats_parts) if stats_parts else ""

    return _HERO_TEMPLATE.format(
        localized_name=hero.localized_name,
        primary_attr=attr_map.get(hero.primary_attr, hero.primary_attr),
        attack_type=hero.attack_type,
        roles=", ".join(hero.roles),
        base_str=hero.base_str,
        str_gain=hero.str_gain,
        base_agi=hero.base_agi,
        agi_gain=hero.agi_gain,
        base_int=hero.base_int,
        int_gain=hero.int_gain,
        base_health=hero.base_health,
        base_mana=hero.base_mana,
        base_armor=hero.base_armor,
        base_attack_min=hero.base_attack_min,
        base_attack_max=hero.base_attack_max,
        move_speed=hero.move_speed,
        stats_section=stats_section,
    )


# ──────────────────────────────────────────────
# 英雄列表
# ──────────────────────────────────────────────

_HERO_LIST_TEMPLATE = """\
# 英雄列表（共 {count} 个）
{filter_desc}

{grouped_section}
"""


def render_hero_list(heroes: list[HeroInfo], filter_attr: str = "", filter_role: str = "") -> str:
    """渲染英雄列表模板。"""
    filtered = heroes
    if filter_attr:
        filtered = [h for h in filtered if h.primary_attr == filter_attr]
    if filter_role:
        filtered = [h for h in filtered if filter_role in h.roles]

    if not filtered:
        return "没有找到符合条件的英雄。"

    attr_map = {"str": "力量", "agi": "敏捷", "int": "智力", "all": "全能"}
    filter_parts = []
    if filter_attr:
        filter_parts.append(f"主属性={attr_map.get(filter_attr, filter_attr)}")
    if filter_role:
        filter_parts.append(f"定位={filter_role}")
    filter_desc = f"\n筛选：{', '.join(filter_parts)}" if filter_parts else ""

    attr_order = ["str", "agi", "int", "all"]
    attr_names = {"str": "力量英雄", "agi": "敏捷英雄", "int": "智力英雄", "all": "全能英雄"}
    groups = []
    for attr in attr_order:
        group = [h for h in filtered if h.primary_attr == attr]
        if not group:
            continue
        names = ", ".join(h.localized_name for h in sorted(group, key=lambda x: x.localized_name))
        groups.append(f"## {attr_names[attr]}\n{names}")
    grouped_section = "\n\n".join(groups)

    return _HERO_LIST_TEMPLATE.format(
        count=len(filtered),
        filter_desc=filter_desc,
        grouped_section=grouped_section,
    )


# ──────────────────────────────────────────────
# 英雄出装
# ──────────────────────────────────────────────

_HERO_BUILD_TEMPLATE = """\
# {hero_name} 出装推荐

{build_section}
"""


def render_hero_build(hero_name: str, build_data: dict, id_map: dict) -> str:
    """渲染英雄出装模板。"""
    phase_names = {
        "start_game_items": "出门装",
        "early_game_items": "前期",
        "mid_game_items": "中期",
        "late_game_items": "后期",
    }
    sections = []
    for phase_key, phase_label in phase_names.items():
        items = build_data.get(phase_key)
        if not items:
            continue
        sorted_items = sorted(items.items(), key=lambda x: int(x[1]), reverse=True)[:5]
        rows = [f"| {id_map.get(int(item_id), f'Item#{item_id}')} | {count}% |" for item_id, count in sorted_items]
        sections.append(f"## {phase_label}\n| 装备 | 选取率 |\n|------|--------|\n" + "\n".join(rows))

    return _HERO_BUILD_TEMPLATE.format(
        hero_name=hero_name,
        build_section="\n\n".join(sections) if sections else "暂无出装数据",
    )


# ──────────────────────────────────────────────
# 物品信息
# ──────────────────────────────────────────────

_ITEM_TEMPLATE = """\
# 物品：{display_name}

| 属性 | 值 |
|------|-----|
| 价格 | {cost} |
| 类型 | {item_type} |
| 效果 | {description} |
| 合成需要 | {components} |
"""


def render_item_info(item: ItemInfo) -> str:
    """渲染物品信息模板。"""
    return _ITEM_TEMPLATE.format(
        display_name=item.display_name,
        cost=item.cost,
        item_type=item.item_type or "基础",
        description=item.description or "无",
        components=", ".join(item.components) if item.components else "无（基础物品）",
    )


# ──────────────────────────────────────────────
# 比赛详情
# ──────────────────────────────────────────────

_MATCH_TEMPLATE = """\
# 比赛详情 #{match_id}

{banner}

## {radiant_head}

{radiant_rows}

## {dire_head}

{dire_rows}
"""


def _combat_rate(p: MatchPlayer, team_contacts: int, team_size: int) -> str:
    """参战率 = 该玩家参战次数相对队友平均水平的高低。

    为什么用 K + A + D：

    * 每一次**击杀**、**助攻**、**死亡**都意味着与对手发生过一次战斗接触，
      三者相加就是这名玩家的「参战次数」；
    * 一个避战选手（只刷钱不参团）的击杀、助攻、死亡都会很少，
      参战次数自然低 —— 这正好刻画题目要的「他是不是在避战」。

    为什么不按团战场次算：``teamfights`` 只在 OpenDota **已解析**的比赛里
    才返回，实测普通玩家近期对局的解析率只有约 7%，绝大多数比赛会算不出来。
    只用 K/A/D 则任何一场比赛都能算，不依赖是否被解析。

    分母取本队五人的参战次数之和，再乘队伍人数，使
    **100% = 与队友平均水平持平**：
    高于 100% 说明比队友更好战，低于 100% 说明比队友更避战。
    """
    if team_contacts <= 0 or team_size <= 0:
        # 缺数据占位用两个连字符而不是破折号「—」：设计规范要求成图里
        # **零破折号**（破折号是典型的 AI 文案痕迹），占位符也算成图内容。
        return "--"
    contacts = (p.kills or 0) + (p.assists or 0) + (p.deaths or 0)
    return f"{contacts * team_size * 100 // team_contacts}%"


# 指标条的指标顺序 = **重要性顺序**。渲染器放不下时会从**末尾**依次丢弃整项，
# 因此最不重要的指标必须排在最后：
#   金钱/补刀   —— 复盘的基本盘，绝不丢；
#   伤害/参战   —— 判断「打没打人 / 有没有避战」的核心；
#   推塔/治疗   —— 有价值但通常是辅助信息，先丢它们。
#
# GPM / XPM 已提到玩家标题行（与 KDA 并列的「核心指标」），不在这条里，
# 否则同一份数据会在两个位置重复出现。
# 指标条顺序：GPM/XPM 放在最前（它们与金钱一样是「发育速度」的第一眼指标），
# 其余六项沿用经济→输出→团队贡献的优先级。两项一组「上行数值、下行标签」。
_METRIC_ORDER = ("GPM", "XPM", "金钱", "补刀", "伤害", "参战", "推塔", "治疗")


def _stats_row(p: MatchPlayer, team_contacts: int, team_size: int) -> str:
    """渲染指标条：``GPM 631｜XPM 672｜金钱 28.6k｜补刀 412/6｜…``。

    GPM / XPM 从玩家标题行下移到这里，与金钱等六项同排：**上行大号数值、
    下行小号标签**，层级由字号承担。它们和「金钱」同属「发育速度」，
    放最前便于横向对比。

    「补刀」按 ``正补/反补`` 呈现（``412/6`` = 正补 412、反补 6）：单看正补
    会把「压制力」漏掉一半 —— 反补同样是线优的直接证据，而两者合起来只多
    一个斜杠的宽度。

    数值**不加粗**：渲染器把这一条拆成上下两行（上行为大号数值、下行为小号
    标签），层级由字号承担就够了；再加粗会让整条变成一堵粗体墙，反而更难扫读
    （用户反馈过「这一版加粗后不如上一版易读」）。
    """
    values = {
        "GPM": str(p.gpm or 0),
        "XPM": str(p.xpm or 0),
        "金钱": fmt_big(p.net_worth),
        "补刀": f"{p.last_hits}/{p.denies}",
        "伤害": fmt_big(p.hero_damage),
        "参战": _combat_rate(p, team_contacts, team_size),
        "推塔": fmt_big(p.tower_damage),
        "治疗": fmt_big(p.hero_healing),
    }
    return "｜".join(f"{name} {values[name]}" for name in _METRIC_ORDER)


def _render_player_block(p: MatchPlayer, team_contacts: int = 0,
                         team_size: int = 0) -> str:
    """单个玩家一个区块，固定三行信息（头像占左侧、竖跨三行）：

    第一行（标题）突出**英雄头像、等级、玩家 ID、KDA**。
    英雄名由**官方立绘**承担（``![英雄名](立绘 URL)``）：图标占据左侧一列；
    立绘拿不到时 Markdown 的 alt 文本就是英雄名，信息不会丢。
    第二行是全部指标：**GPM / XPM / 金钱 / 补刀 / 伤害 / 参战 / 推塔 / 治疗**，
    渲染器拆成上下两行 —— **上行大号数值、下行小号标签**，层级由字号承担。
    GPM/XPM 与「金钱」同属发育速度，放指标条最前。「补刀」写作 ``正补/反补``。
    第三行是装备（含中立物品、背包、神杖/魔晶状态），用 Markdown 图片指向
    官方游戏内图标，alt 文本是中文物品名：图标拿不到时中文名会顶上来。

    玩家 ID 优先用昵称；资料未公开时 OpenDota 不给 personaname，退回账号数字；
    两者都拿不到才显示「匿名玩家」—— 宁可显式说明是匿名，也不要留一段空白让
    用户以为渲染出错，这样三行的字段位置在所有玩家之间保持对齐。
    """
    # 英雄名写进图片的 alt：渲染时显示立绘，纯文本/降级时显示名字
    portrait = portrait_markdown(p.hero_name, p.hero_id, alt=p.hero_name)
    # 标题用「·」分段而非括号包裹 ID：ID 里可能带右括号，
    # 用 `（...）` 会把标题截断得难以阅读。
    #
    # 行内**不再标胜负**（原先的 ✅/❌ 已去掉）：胜负属于队伍而非个人，
    # 改由分队标题统一呈现（见 render_match_detail 的「天辉 获胜」）。
    # 每位玩家的第一行保留四项：头像、等级、玩家 ID、KDA。
    title = f"### {portrait}"
    if p.level:
        title += f" · Lv.{p.level}"
    player_id = p.persona_name or (str(p.account_id) if p.account_id else "")
    # 玩家 ID 加粗：复盘时最先看的就是「这是谁」，昵称与英雄名一样需要一眼可辨。
    # 不存在的 ID 用「匿名玩家」占位，同样加粗以保持整行字重一致。
    title += f" · **{player_id or ANONYMOUS_PLAYER}**"
    # KDA 只留数字（``**7/1/5**``）：标签「KDA」占宽且每行都重复，
    # 斜杠格式本身已足够自明。
    title += f" · **{p.kills}/{p.deaths}/{p.assists}**"
    # GPM / XPM 已下移到指标条（见 _stats_row），标题行不再重复。

    # 中立物品并入装备行（原先是独立第四行），保证右侧数据正好三行、
    # 与左侧头像「竖跨三行」对齐。分隔符用「 ＋ 」而不是「 ｜ 」，
    # 一眼能看出后半段是中立物品而非又一件基础装备。
    items = render_items_markdown(p.items)
    neutral = render_items_markdown(p.neutral_items, empty="")
    inventory = f"{items} ＋ {neutral}" if neutral else items
    # 背包三格以「 ◦ 」起头列在末尾：与主战装备/中立物品区分开。
    if getattr(p, "backpack", None):
        bp = render_items_markdown(p.backpack, empty="")
        if bp:
            inventory = f"{inventory} ◦ {bp}"
    # 阿哈得姆神杖 / 魔晶点亮状态：客户端 HUD 里是一张**组合图标**（上神杖、
    # 下魔晶），默认空心、出了对应装备才填充颜色。CDN 上没有这张组合图，
    # 本地渲染器见到这两张相邻的神杖/魔晶图标时合成组合状态图（未得灰化、
    # 已得原色）。alt 前缀 [已]/[无] 供渲染器判断点亮状态。
    scepter = getattr(p, "aghanims_scepter", False)
    shard = getattr(p, "aghanims_shard", False)
    enh = []
    enh.append(f"![{'［已］' if scepter else '［无］'}阿哈利姆神杖]({ICON_BASE_URL}ultimate_scepter.png)")
    enh.append(f"![{'［已］' if shard else '［无］'}魔晶]({ICON_BASE_URL}aghanims_shard.png)")
    inventory = f"{inventory} ｜ {' '.join(enh)}"

    # 三行之间必须用**空行**分隔，不能只用单个换行：
    # Markdown 里连续两行属于同一个段落，单换行会被合并成一个 <p>，
    # 于是数据行与装备行挤在同一行 —— 右侧就不是三行，头像的「跨三行」也失去意义。
    # 空行让它们成为三个独立段落，网络模板的 grid 与本地渲染器都能正确分行。
    lines = [
        title,
        _stats_row(p, team_contacts, team_size),
        f"装备：{inventory}",
    ]
    return "\n\n".join(lines)


def _team_kills(players: list[MatchPlayer], score: int) -> int:
    """队伍总击杀。

    优先用接口给的 ``radiant_score`` / ``dire_score`` —— 这两个字段就是
    团队击杀数。个别数据源（如 Valve 的实时接口）可能没有该字段而留 0，
    此时退回「队员击杀数之和」，避免标题上出现一个假的 0。
    """
    if score:
        return score
    return sum(p.kills or 0 for p in players)


def _mvp_score(p: MatchPlayer) -> float:
    """MVP 评选分数（只在胜方内部比较）。

    权重取值的依据 —— 各项折算后量级相当，不让单一维度独裁：

    * ``击杀 × 3``、``助攻 × 1.5``：击杀的决定性高于助攻，但助攻必须计入，
      否则团队型中单/辅助会被系统性低估；
    * ``死亡 × 1.5``：送人头是负贡献，权重高于助攻、低于击杀；
    * ``GPM / 20``：正常对局 GPM 约 300~900，折算 15~45 分；
    * ``英雄伤害 / 3000``：正常约 10k~80k，折算 3~27 分。
    """
    return (
        (p.kills or 0) * 3
        + (p.assists or 0) * 1.5
        - (p.deaths or 0) * 1.5
        + (p.gpm or 0) / 20
        + (p.hero_damage or 0) / 3000
    )


def _pick_mvp(players: list[MatchPlayer], radiant_win: bool) -> MatchPlayer | None:
    """选出全场 MVP。

    只在**胜方**里选：败方数据再亮眼也不是「本场最佳」，
    参考样式（用户提供的基准图）同样把 MVP 位给了胜方选手。
    胜方五人全都缺数据时退回全体最高分，宁可选出一个人也不留空位。
    """
    if not players:
        return None
    winners = [p for p in players if bool(p.is_radiant) == bool(radiant_win)]
    return max(winners or players, key=_mvp_score)


def _display_name(p: MatchPlayer) -> str:
    """玩家显示名；资料未公开时退回账号数字，都没有才用占位。"""
    return p.persona_name or (str(p.account_id) if p.account_id else "") or ANONYMOUS_PLAYER


def _match_banner(match: MatchDetail, duration: str, start_time: str) -> str:
    """顶部横幅区块：MVP 金色大字 + 比分。

    形状（渲染器按此解析）：

        ## MVP ![英雄名](横版立绘 URL) · 昵称 · K/D/A

        天辉 35 : 49 夜魇 · 43:32 · 2025-09-17 06:40

    两行之间用空行分开：Markdown 里连续两行属于同一段落，只有空行才能让
    渲染器把它们当成两个独立区块（横幅正文 / 比分行）。
    """
    mvp = _pick_mvp(match.players, match.radiant_win)
    if mvp is None:
        return ""
    banner = banner_markdown(mvp.hero_name, mvp.hero_id, alt=mvp.hero_name)
    head = "## MVP {} · {} · {}/{}/{}".format(
        banner, _display_name(mvp), mvp.kills, mvp.deaths, mvp.assists
    )
    # 队伍顺序与下面两个分队标题保持一致（天辉在前）。胜负不写进这一行 ——
    # 分数本身已经给出胜负（Dota 无平局），渲染器据此给胜方着色即可。
    score = "天辉 {} : {} 夜魇 · {} · {}".format(
        match.radiant_score, match.dire_score, duration, start_time
    )
    return f"{head}\n\n{score}"


def render_match_detail(match: MatchDetail) -> str:
    """渲染比赛详情模板。"""
    duration = fmt_duration(match.duration_seconds)
    start_time = (
        datetime.fromtimestamp(match.start_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        if match.start_time else "未知"
    )

    radiant = [p for p in match.players if p.is_radiant]
    dire = [p for p in match.players if not p.is_radiant]

    def team_head(name: str, players: list[MatchPlayer], score: int, won: bool) -> str:
        # 一行同时给出「胜负 + 团队总击杀 + 团队经济」。
        # 胜负原先标在每位玩家行首（✅/❌），但那是**队伍**的结果，
        # 写十次既冗余又容易看串行，汇总到标题更自然。
        return "{} {} · 击杀 {} · 经济 {}".format(
            name,
            "获胜" if won else "失败",
            _team_kills(players, score),
            fmt_big(sum(p.net_worth or 0 for p in players)),
        )

    def player_blocks(players):
        # 参战率的分母是本队五人的 K+A+D 之和
        team_contacts = sum(
            (p.kills or 0) + (p.assists or 0) + (p.deaths or 0) for p in players
        )
        blocks = [
            _render_player_block(p, team_contacts, len(players)) for p in players
        ]
        return "\n\n".join(blocks) if blocks else "暂无数据"

    return _MATCH_TEMPLATE.format(
        match_id=match.match_id,
        banner=_match_banner(match, duration, start_time),
        radiant_head=team_head("天辉", radiant, match.radiant_score, match.radiant_win),
        dire_head=team_head("夜魇", dire, match.dire_score, not match.radiant_win),
        radiant_rows=player_blocks(radiant),
        dire_rows=player_blocks(dire),
    )


# ──────────────────────────────────────────────
# 实时比赛
# ──────────────────────────────────────────────

_LIVE_TEMPLATE = """\
# 实时比赛（共 {count} 场）

| 天辉 | 夜魇 | 比分 | 进行时间 | MMR | 观战 |
|------|------|------|----------|-----|------|
{game_rows}
"""


def render_live_games(games: list[LiveGame]) -> str:
    """渲染实时比赛模板。"""
    if not games:
        return "当前没有正在进行的比赛。"

    rows = []
    for g in games:
        radiant = g.team_name_radiant or "天辉"
        dire = g.team_name_dire or "夜魇"
        score = f"{g.radiant_score}:{g.dire_score}"
        game_time = fmt_duration(g.game_time)
        mmr = g.average_mmr or "-"
        spectators = g.spectators or "-"
        rows.append(f"| {radiant} | {dire} | {score} | {game_time} | {mmr} | {spectators} |")

    return _LIVE_TEMPLATE.format(
        count=len(games),
        game_rows="\n".join(rows),
    )


# ──────────────────────────────────────────────
# 职业比赛
# ──────────────────────────────────────────────

_PRO_TEMPLATE = """\
# 近期职业比赛（共 {count} 场）

| 天辉 | 夜魇 | 比分 | 胜者 | 时长 | 赛事 | 时间 |
|------|------|------|------|------|------|------|
{match_rows}
"""


def render_pro_matches(matches: list[ProMatch]) -> str:
    """渲染职业比赛模板。"""
    if not matches:
        return "暂无职业比赛数据。"

    rows = []
    for m in matches:
        radiant = m.radiant_team_name or "天辉"
        dire = m.dire_team_name or "夜魇"
        score = f"{m.radiant_score}:{m.dire_score}"
        winner = radiant if m.radiant_win else dire
        duration = fmt_duration(m.duration_seconds)
        start = (
            datetime.fromtimestamp(m.start_time, tz=timezone.utc).strftime("%m-%d %H:%M")
            if m.start_time else ""
        )
        league = m.league_name or "-"
        rows.append(f"| {radiant} | {dire} | {score} | {winner} | {duration} | {league} | {start} |")

    return _PRO_TEMPLATE.format(
        count=len(matches),
        match_rows="\n".join(rows),
    )
