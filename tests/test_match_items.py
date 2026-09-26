"""比赛战报详情：装备图标 + 每位玩家独立区块的行为测试。

覆盖三层：
1. ``core.item_icons`` —— 物品 ID → 中文名 / 官方图标 URL 的解析与兜底；
2. ``core.templates.render_match_detail`` —— 输出结构（每人一区块、含全部新字段）；
3. ``core.image_renderer._clean_inline`` —— 本地 Pillow 兜底把图片语法降级为 alt 文本。
"""
import re
import unittest

from astrbot_plugin_dota2assistant_plus.core.item_icons import (
    ICON_BASE_URL,
    item_icon_markdown,
    item_icon_url,
    item_name,
    render_items_markdown,
)
from astrbot_plugin_dota2assistant_plus.core.hero_icons import PORTRAIT_BASE_URL
from astrbot_plugin_dota2assistant_plus.core.models import (
    MatchDetail,
    MatchPlayer,
)
from astrbot_plugin_dota2assistant_plus.core.templates import (
    _combat_rate,
    _render_player_block,
    render_match_detail,
)


class TestItemIconResolution(unittest.TestCase):
    """数字物品 ID → 中文名 / 图标 URL。"""

    def test_known_item_has_chinese_name_and_icon(self):
        # 116 = Black King Bar
        self.assertEqual(item_name(116), "黑皇杖")
        self.assertEqual(item_icon_url(116), f"{ICON_BASE_URL}black_king_bar.png")

    def test_accepts_int_and_str_ids(self):
        self.assertEqual(item_name("116"), item_name(116))
        self.assertEqual(item_icon_url("116"), item_icon_url(116))

    def test_recipe_items_share_single_icon(self):
        """recipe_* 内部名带不同后缀，但共用 CDN 上的 recipe.png。"""
        # 35 = recipe_magic_wand（内部名与图标名不同，靠 "i" 覆盖）
        url = item_icon_url(35)
        self.assertTrue(url, "图纸类物品也应有图标 URL")
        self.assertTrue(url.endswith("/recipe.png"), f"实际 {url}")

    def test_unknown_id_degrades(self):
        self.assertEqual(item_icon_url(999999), "")
        self.assertEqual(item_name(999999), "物品#999999")
        # 没有图标时应退化为纯文本，而不是留下空的图片语法
        self.assertEqual(item_icon_markdown(999999), "物品#999999")

    def test_empty_and_invalid_input(self):
        for bad in (None, "", "0", 0, "abc"):
            with self.subTest(bad=bad):
                self.assertEqual(item_icon_url(bad), "")
                self.assertEqual(item_icon_markdown(bad), "")

    def test_markdown_carries_alt_text(self):
        md = item_icon_markdown(116)
        self.assertEqual(md, f"![黑皇杖]({ICON_BASE_URL}black_king_bar.png)")


class TestRenderItemsMarkdown(unittest.TestCase):
    """一组物品 ID → 一行 Markdown，空栏位被跳过。"""

    def test_renders_all_slots(self):
        md = render_items_markdown(["116", "50", "151"])
        self.assertIn("黑皇杖", md)
        self.assertIn("相位鞋", md)
        self.assertEqual(md.count("!["), 3)

    def test_skips_empty_slots(self):
        """空装备栏位是常态（0 / None / ""），不应产生空图标。"""
        md = render_items_markdown(["116", 0, None, "", "0", "50"])
        self.assertEqual(md.count("!["), 2)

    def test_all_empty_returns_placeholder(self):
        self.assertEqual(render_items_markdown([]), "无")
        self.assertEqual(render_items_markdown([0, None, ""]), "无")
        # neutral 场景用 empty="" 表示「不显示这一行」
        self.assertEqual(render_items_markdown([], empty=""), "")

    def test_no_numeric_name_leaks_into_icons(self):
        md = render_items_markdown(["116", "50"])
        self.assertNotIn("116", md)
        self.assertNotIn("物品#", md)


class TestMatchDetailLayout(unittest.TestCase):
    """比赛详情 Markdown 契约：顶部 MVP 横幅 + 分队条 + 玩家核心行 + 两行指标条 + 装备。"""

    def _match(self):
        return MatchDetail(
            match_id=8831125663,
            duration_seconds=2534,
            radiant_win=True,
            radiant_score=41,
            dire_score=28,
            start_time=1700000000,
            players=[
                MatchPlayer(
                    hero_name="敌法师", persona_name="miracle", level=19,
                    account_id=899428504,
                    kills=10, deaths=2, assists=5, gpm=631, xpm=672,
                    hero_damage=25000, tower_damage=10202, hero_healing=1234,
                    last_hits=198, denies=9, net_worth=18586,
                    items=["116", "50", "151"], neutral_items=["577"],
                    is_radiant=True, win=True,
                ),
                MatchPlayer(
                    hero_name="斧王", level=18, kills=5, deaths=8, assists=15,
                    gpm=400, xpm=450, hero_damage=18000, tower_damage=500,
                    hero_healing=0, last_hits=120, denies=3, net_worth=14000,
                    items=["63"], is_radiant=False, win=False,
                ),
            ],
        )

    def _render(self):
        return render_match_detail(self._match())

    def _first_block(self) -> str:
        md = self._render()
        start = md.index("### ")
        rest = md[start:]
        nxt = rest.find("### ", 3)
        return rest if nxt == -1 else rest[:nxt]

    def _first_rows(self) -> list[str]:
        return [ln for ln in self._first_block().splitlines() if ln.strip()]

    def test_one_block_per_player(self):
        result = self._render()
        self.assertEqual(result.count("### "), 2)

    def test_mvp_banner_at_top(self):
        """顶部横幅：MVP 立绘 + 昵称 + KDA + 比分。"""
        md = self._render()
        banner = [ln for ln in md.splitlines() if ln.startswith("## MVP ")][0]
        self.assertIn("miracle", banner)
        self.assertIn("10/2/5", banner)
        # 横幅立绘是横版 dota_react
        self.assertIn("dota_react/heroes/antimage.png?kind=banner", banner)

    def test_score_line_has_both_teams_and_duration(self):
        md = self._render()
        line = [ln for ln in md.splitlines() if "天辉" in ln and " : " in ln and "夜魇" in ln][0]
        self.assertIn("天辉 41 : 28 夜魇", line)
        self.assertIn("42:14", line)

    def test_team_heads_show_result_kills_and_gold(self):
        md = self._render()
        self.assertIn("## 天辉 获胜 · 击杀 41 · 经济 18.6k", md)
        self.assertIn("## 夜魇 失败 · 击杀 28 · 经济 14.0k", md)

    def test_team_headings_swap_when_dire_wins(self):
        md = render_match_detail(MatchDetail(
            match_id=1, radiant_win=False,
            players=[MatchPlayer(hero_name="斧王", is_radiant=False)],
        ))
        self.assertIn("## 天辉 失败", md)
        self.assertIn("## 夜魇 获胜", md)

    def test_team_kills_fall_back_to_player_sum(self):
        md = render_match_detail(MatchDetail(
            match_id=1, radiant_win=True, radiant_score=0, dire_score=0,
            players=[
                MatchPlayer(hero_name="敌法师", kills=7, is_radiant=True),
                MatchPlayer(hero_name="斧王", kills=3, is_radiant=True),
                MatchPlayer(hero_name="影魔", kills=9, is_radiant=False),
            ],
        ))
        self.assertIn("击杀 10", md)
        self.assertIn("击杀 9", md)

    def test_player_title_includes_level_name_kda(self):
        title = self._first_rows()[0]
        for token in ("敌法师", "Lv.19", "miracle", "10/2/5"):
            self.assertIn(token, title)
        # GPM/XPM 已下移到指标条，标题行不再重复
        self.assertNotIn("GPM", title)
        self.assertNotIn("XPM", title)

    def test_stats_line_has_eight_metrics(self):
        """指标条 8 项：GPM/XPM/金钱/补刀/伤害/参战/推塔/治疗，GPM/XPM 在最前。"""
        line = self._first_rows()[1]
        for metric in ("GPM", "XPM", "金钱", "补刀", "伤害", "参战", "推塔", "治疗"):
            self.assertIn(metric, line)
        # GPM/XPM 必须在最前
        self.assertTrue(line.startswith("GPM "), f"指标条应以 GPM 开头: {line}")
        self.assertLess(line.index("GPM"), line.index("金钱"))
        self.assertLess(line.index("XPM"), line.index("金钱"))

    def test_cs_shows_last_hits_and_denies(self):
        """补刀写作 正补/反补 格式，不再单独列「反补」标签。"""
        md = self._render()
        self.assertIn("198/9", md)
        self.assertNotIn("反补 ", md)

    def test_third_line_is_items(self):
        self.assertTrue(self._first_rows()[2].startswith("装备："))

    def test_neutral_items_merged_into_equipment_row(self):
        result = self._render()
        self.assertEqual(result.count("中立："), 0)
        equipment = [ln for ln in result.splitlines() if ln.startswith("装备：")]
        self.assertTrue(equipment)
        with_neutral = [ln for ln in equipment if "＋" in ln]
        self.assertEqual(len(with_neutral), 1)
        self.assertIn("＋ ![附魂面具]", with_neutral[0])

    def test_aghanims_status_in_equipment_row(self):
        """神杖/魔晶点亮状态出现在出装行，alt 带 ［已］/［无］ 前缀。"""
        result = self._render()
        equipment = [ln for ln in result.splitlines() if ln.startswith("装备：")]
        self.assertTrue(equipment)
        # 两个玩家都默认未出 → 都是 ［无］
        self.assertTrue(all("ultimate_scepter.png" in ln for ln in equipment))
        self.assertTrue(all("aghanims_shard.png" in ln for ln in equipment))
        self.assertTrue(all("［无］阿哈利姆神杖" in ln for ln in equipment))

    def test_aghanims_status_lit_when_owned(self):
        """出了神杖/魔晶时 alt 前缀为 ［已］。"""
        match = MatchDetail(
            match_id=1,
            players=[MatchPlayer(
                hero_name="斧王", items=["116"], is_radiant=True, win=True,
                aghanims_scepter=True, aghanims_shard=True,
            )],
        )
        result = render_match_detail(match)
        equipment = [ln for ln in result.splitlines() if ln.startswith("装备：")][0]
        self.assertIn("［已］阿哈利姆神杖", equipment)
        self.assertIn("［已］魔晶", equipment)

    def test_backpack_listed_in_equipment_row(self):
        """背包三格以「 ◦ 」前缀并入出装行。"""
        match = MatchDetail(
            match_id=1,
            players=[MatchPlayer(
                hero_name="斧王", items=["116"], backpack=["50", "63"],
                is_radiant=True, win=True,
            )],
        )
        result = render_match_detail(match)
        equipment = [ln for ln in result.splitlines() if ln.startswith("装备：")][0]
        self.assertIn("◦", equipment)

    def test_large_numbers_use_k_suffix(self):
        stats = self._first_rows()[1]
        self.assertIn("18.6k", stats)
        self.assertIn("25.0k", stats)
        self.assertIn("10.2k", stats)
        self.assertIn("1.2k", stats)
        self.assertNotIn("18,586", self._first_block())

    def test_player_block_has_three_rows(self):
        result = self._render()
        parts = re.split(r"(?=^### )", result, flags=re.M)
        blocks = [p for p in parts if p.startswith("### ")]
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            rows = [ln for ln in block.splitlines() if ln.strip() and not ln.startswith("## ")]
            self.assertEqual(len(rows), 3, f"玩家区块行数不是 3: {rows}")
            self.assertTrue(rows[1].startswith("GPM "))
            self.assertTrue(rows[2].startswith("装备："))

    def test_hero_rendered_as_official_portrait(self):
        title = self._first_rows()[0]
        self.assertIn(f"![敌法师]({PORTRAIT_BASE_URL}antimage_vert.jpg)", title)

    def test_existing_contract_kept(self):
        """不回归：双方英雄名 / 比分 / 击杀 / 经济仍需出现。"""
        result = self._render()
        self.assertIn("敌法师", result)
        self.assertIn("斧王", result)
        self.assertIn("41 : 28", result)
        self.assertIn("经济", result)
        self.assertIn("击杀", result)

    def test_players_without_items_do_not_break(self):
        match = MatchDetail(match_id=1, players=[MatchPlayer(hero_name="斧王", is_radiant=False)])
        result = render_match_detail(match)
        self.assertIn("装备：无", result)
        self.assertNotIn("中立：", result)

    def test_empty_players(self):
        result = render_match_detail(MatchDetail(match_id=1))
        self.assertIn("暂无数据", result)


class TestFmtBig(unittest.TestCase):
    """大数值压缩：>1000 用 k，保留 1 位小数。"""

    def test_boundary(self):
        from astrbot_plugin_dota2assistant_plus.core.templates import fmt_big

        self.assertEqual(fmt_big(0), "0")
        self.assertEqual(fmt_big(999), "999")
        self.assertEqual(fmt_big(1000), "1,000")   # 恰好 1000 不压缩
        self.assertEqual(fmt_big(1001), "1.0k")
        self.assertEqual(fmt_big(1234), "1.2k")
        self.assertEqual(fmt_big(10202), "10.2k")
        self.assertEqual(fmt_big(25000), "25.0k")

    def test_large_numbers_stay_short(self):
        """六位数也只占 4 个字符左右，避免把行撑长。"""
        from astrbot_plugin_dota2assistant_plus.core.templates import fmt_big

        # 数值越大头部越长（1234.6k），上限按 6 位数取 7 字符
        for v, limit in ((100000, 6), (987654, 6), (1234567, 7)):
            with self.subTest(v=v):
                self.assertLessEqual(len(fmt_big(v)), limit)

    def test_rounding_is_one_decimal(self):
        from astrbot_plugin_dota2assistant_plus.core.templates import fmt_big

        self.assertEqual(fmt_big(1949), "1.9k")
        # 注意 1950 会得到 "1.9k"：1.95 的二进制表示略小于 1.95，
        # f"{1.95:.1f}" 因而不是 2.0。这是 Python 浮点格式化的正常行为，
        # 不额外做 Decimal 处理（卡片展示场景无需精确进位）。
        self.assertEqual(fmt_big(1950), "1.9k")
        self.assertEqual(fmt_big(1960), "2.0k")

    def test_bad_input_does_not_raise(self):
        from astrbot_plugin_dota2assistant_plus.core.templates import fmt_big

        self.assertEqual(fmt_big(None), "0")
        self.assertEqual(fmt_big("abc"), "abc")


class TestLocalRendererImageDegradation(unittest.TestCase):
    """Pillow 兜底路径画不了图，必须把图片语法降级成 alt 文本。"""

    def test_image_becomes_alt_text(self):
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import _clean_inline

        out = _clean_inline("装备：![黑皇杖](https://x/black_king_bar.png)")
        self.assertEqual(out, "装备：黑皇杖")
        self.assertNotIn("http", out)

    def test_multiple_icons_become_readable_list(self):
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import _clean_inline

        out = _clean_inline("装备：![黑皇杖](https://x/a.png) ![相位鞋](https://x/b.png)")
        self.assertEqual(out, "装备：黑皇杖 相位鞋")

    def test_plain_link_keeps_text(self):
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import _clean_inline

        self.assertEqual(_clean_inline("见[官网](https://x/y)"), "见官网")

    def test_bold_still_stripped(self):
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import _clean_inline

        self.assertEqual(_clean_inline("KDA **10/2/5** ｜ GPM **631**"), "KDA 10/2/5 ｜ GPM 631")


class TestInlineBoldParsing(unittest.TestCase):
    """本地兜底渲染器必须保留行内粗体，用于突出关键英雄与玩家 ID。

    早期实现把 ``**`` 直接删掉，导致总结里所有字一样粗细，
    「重点显示」在 Pillow 路径上完全失效。
    """

    def test_bold_runs_are_preserved(self):
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import _inline_runs

        runs = _inline_runs("**暗夜魔王** 表现优异")
        self.assertEqual(runs[0], ("暗夜魔王", True))
        self.assertFalse(runs[1][1])

    def test_clean_inline_returns_plain_text(self):
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import _clean_inline

        self.assertEqual(_clean_inline("KDA **10/2/5** ｜ GPM **631**"), "KDA 10/2/5 ｜ GPM 631")

    def test_mask_aligns_with_plain_text(self):
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import (
            _bold_mask,
            _clean_inline,
            _inline_runs,
        )

        raw = "**暗夜魔王** 表现优异"
        plain = _clean_inline(raw)
        mask = _bold_mask(plain, _inline_runs(raw))
        self.assertEqual(len(mask), len(plain))
        self.assertEqual(mask[:4], [True] * 4)
        self.assertFalse(mask[4])

    def test_image_degradation_still_works_inside_bold(self):
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import _clean_inline

        self.assertEqual(_clean_inline("**![黑皇杖](https://x/a.png)**"), "黑皇杖")
        self.assertNotIn("http", _clean_inline("![黑皇杖](https://x/a.png)"))

    def test_analysis_font_size_is_larger_than_body(self):
        """总结正文要放大到分栏目标题尺寸，不能和普通正文一样大。"""
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import (
            _ANALYSIS_FONT_SIZE,
        )

        self.assertGreaterEqual(_ANALYSIS_FONT_SIZE, 20)


class TestHeroNameTables(unittest.TestCase):
    """英雄名对照表：英文名与中文名必须一一对应，别名必须指向正确的英雄。

    这类错误在界面上表现为「问 A 英雄、给出 B 英雄的数据」，用户一眼就能
    看出不对，但代码不会报任何错，所以用测试锁住。
    真实事故：``assets/heroes.json`` 里「天涯墨客」（Grimstroke，121）
    被写成了 ``npc_dota_hero_pangolier``（石鳞剑士，120），
    查询「天涯墨客」会返回石鳞剑士的资料。
    """

    def _assets(self):
        """返回 (hero_names 表, heroes 别名表)。"""
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "assets"
        names = json.loads((root / "hero_names.json").read_text(encoding="utf-8"))
        aliases = json.loads((root / "heroes.json").read_text(encoding="utf-8"))
        return names, aliases

    def test_hero_names_pairs_en_and_zh(self):
        """每条都同时有英文名与中文名，且 id 唯一。"""
        names, _ = self._assets()
        self.assertTrue(names)
        ids = set()
        for internal, info in names.items():
            with self.subTest(hero=internal):
                self.assertTrue(internal.startswith("npc_dota_hero_"))
                self.assertTrue(info.get("en"), "缺英文名")
                self.assertTrue(info.get("zh"), "缺中文名")
                self.assertIsInstance(info.get("id"), int)
                self.assertNotIn(info["id"], ids, f"id {info['id']} 重复")
                ids.add(info["id"])

    def test_no_duplicate_names_between_heroes(self):
        """英文名与中文名都不能有两个英雄共用。"""
        names, _ = self._assets()
        for field in ("en", "zh"):
            with self.subTest(field=field):
                values = [info[field] for info in names.values()]
                self.assertEqual(len(values), len(set(values)), f"{field} 有重复")

    def test_aliases_point_to_the_right_hero(self):
        """别名是官方中文名时，必须指向该名字自己的英雄。

        「天涯墨客」这种写成别的英雄内部名的，会让查询结果张冠李戴。
        """
        names, aliases = self._assets()
        by_zh = {info["zh"]: internal for internal, info in names.items()}

        for alias, internal in aliases.items():
            if alias not in by_zh:
                continue  # 俗称/简写（火猫、水人…）不在此校验范围
            with self.subTest(alias=alias):
                self.assertEqual(
                    internal, by_zh[alias],
                    f"别名「{alias}」指向了 {internal}，应指向 {by_zh[alias]}",
                )

    def test_alias_targets_all_exist(self):
        """别名指向的内部名必须真实存在，否则查询会静默失败。"""
        names, aliases = self._assets()
        unknown = sorted({i for i in aliases.values() if i not in names})
        self.assertEqual(unknown, [], f"别名指向了不存在的英雄：{unknown}")


class TestQueryKindDetection(unittest.TestCase):
    """点评要与上方卡片对应：按卡片 h1 认出这是哪一种查询。"""

    def test_each_card_kind_is_detected(self):
        """八种卡片各自的 h1 都要认得出。"""
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        cases = {
            "# 比赛详情 #8877665544\n\n| 项目 | 数据 |": tpl.KIND_MATCH,
            "# 玩家资料：miracle\n\n| 段位 | 万古流芳 |": tpl.KIND_PLAYER,
            "# 英雄：敌法师\n\n| 属性 | 值 |": tpl.KIND_HERO,
            "# 英雄列表（共 127 个）\n": tpl.KIND_HERO_LIST,
            "# 敌法师 出装推荐\n\n## 出门装": tpl.KIND_HERO_BUILD,
            "# 物品：黑皇杖\n\n| 价格 | 100 |": tpl.KIND_ITEM,
            "# 实时比赛（共 3 场）\n": tpl.KIND_LIVE,
            "# 近期职业比赛（共 5 场）\n": tpl.KIND_PRO,
        }
        for card, expected in cases.items():
            with self.subTest(card=card.splitlines()[0]):
                self.assertEqual(tpl.detect_kind([card]), expected)

    def test_match_wins_over_other_cards(self):
        """一轮里同时查了对局和别的，按对局处理（三点式信息量最大）。"""
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        match = "# 比赛详情 #1\n"
        other = "# 玩家资料：x\n"
        self.assertEqual(tpl.detect_kind([other, match]), tpl.KIND_MATCH)
        self.assertEqual(tpl.detect_kind([match, other]), tpl.KIND_MATCH)

    def test_unknown_card_falls_back_to_match(self):
        """认不出种类时返回空串，调用方按对局处理（沿用原行为）。"""
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        self.assertEqual(tpl.detect_kind([]), "")
        self.assertEqual(tpl.detect_kind(["# 战绩\n"]), "")

    def test_analysis_title_switches_by_kind(self):
        """对局叫「综合分析」，其余叫「点评」；认不出时沿用「综合分析」。"""
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        self.assertEqual(tpl.analysis_title(tpl.KIND_MATCH), "综合分析")
        self.assertEqual(tpl.analysis_title(""), "综合分析")
        for kind in (tpl.KIND_PLAYER, tpl.KIND_HERO, tpl.KIND_HERO_LIST,
                     tpl.KIND_HERO_BUILD, tpl.KIND_ITEM, tpl.KIND_LIVE,
                     tpl.KIND_PRO):
            with self.subTest(kind=kind):
                self.assertEqual(tpl.analysis_title(kind), "点评")


class TestReviewScenarios(unittest.TestCase):
    """非对局场景的点评：一段式、与卡片内容对应、提示词不得跑偏。"""

    _PLAYER_CARD = (
        "# 玩家资料：miracle\n\n| 段位 | 万古流芳 |\n\n"
        "## 近期战绩（最近 5 场）\n\n**总体**: 3胜 2负（胜率 60%）\n"
        "**平均 KDA**: 4.2\n\n"
        "| 结果 | 英雄 | KDA | 时长 | GPM | 伤害 |\n"
        "|------|------|-----|------|-----|------|\n"
        "| ✅ | 灰烬之灵 | 12/3/8 | 41:20 | 620 | 38000 |\n"
        "| ❌ | 敌法师 | 3/9/4 | 35:10 | 410 | 12000 |\n"
    )

    def test_review_prompts_exist_for_every_kind(self):
        """每种非对局卡片都要有自己的点评范围说明，不能落到兜底。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        kinds = (tpl.KIND_PLAYER, tpl.KIND_HERO, tpl.KIND_HERO_LIST,
                 tpl.KIND_HERO_BUILD, tpl.KIND_ITEM, tpl.KIND_LIVE,
                 tpl.KIND_PRO)
        for kind in kinds:
            with self.subTest(kind=kind):
                self.assertIn(kind, summarizer._REVIEW_SCOPES)
        # 兜底文案只给认不出的种类
        fallback = summarizer._review_prompt("不存在的种类")
        self.assertIn("未能识别", fallback)

    def test_review_prompt_forbids_fabricating_data(self):
        """点评不得引入卡片上没有的信息 —— 这正是本次要修的问题。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        for kind in (tpl.KIND_PLAYER, tpl.KIND_HERO, tpl.KIND_HERO_LIST,
                     tpl.KIND_HERO_BUILD, tpl.KIND_ITEM, tpl.KIND_LIVE,
                     tpl.KIND_PRO):
            with self.subTest(kind=kind):
                prompt = summarizer._review_prompt(kind)
                self.assertIn("真实出现", prompt)
                self.assertIn("不要自己编造数字", prompt)
                # 必须明确要求「一段」，不能写成三点式
                self.assertIn("不要分小标题", prompt)

    def test_hero_list_prompt_forbids_winrate(self):
        """英雄名单卡片里没有胜率，提示词必须显式禁止提胜率。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        prompt = summarizer._review_prompt(tpl.KIND_HERO_LIST)
        self.assertIn("绝对不要编造胜率", prompt)

    def test_player_prompt_does_not_mention_two_teams(self):
        """近期战绩没有分队，提示词不能要求点评「队友」或「对局双方」。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        prompt = summarizer._review_prompt(tpl.KIND_PLAYER)
        self.assertIn("不要评价并不存在的", prompt)

    def test_review_output_is_single_paragraph(self):
        """非对局点评即使模型写了小标题，也要摊平成一段。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        model_text = (
            "### 近期状态\n\n最近 5 场 3 胜 2 负，胜率 60%。\n\n"
            "### 亮点\n\n用灰烬之灵那场 12/3/8，GPM 620。"
        )
        out = summarizer.format_analysis(
            model_text, [self._PLAYER_CARD], kind=tpl.KIND_PLAYER
        )
        self.assertNotIn("###", out)
        self.assertIn("3 胜 2 负", out)
        self.assertIn("灰烬之灵", out)

    def test_review_bolds_hero_names_from_card(self):
        """非对局卡片里的英雄名同样由代码加粗，不依赖模型自觉。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        out = summarizer.format_analysis(
            "灰烬之灵打得不错，敌法师那场失误偏多。",
            [self._PLAYER_CARD],
            kind=tpl.KIND_PLAYER,
        )
        self.assertIn("**灰烬之灵**", out)

    def test_review_is_condensed_by_char_budget(self):
        """一段式点评同样受字数上限约束。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        long_text = "最近状态起伏较大。" * 60
        out = summarizer.format_analysis(
            long_text, [self._PLAYER_CARD], max_chars=100, kind=tpl.KIND_PLAYER
        )
        self.assertLessEqual(len(out), 120)

    def test_match_kind_keeps_three_sections(self):
        """对局仍走三点式，改动不能破坏原行为。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer
        from astrbot_plugin_dota2assistant_plus.core import templates as tpl

        match_card = (
            "# 比赛详情 #1\n\n## 天辉\n\n"
            "### ![暗夜魔王](https://x/night_stalker_vert.jpg) · Lv.19 · x · 1/2/3\n"
        )
        model_text = (
            "### 天辉方的表现\n\n暗夜魔王经济 18.6k。\n\n"
            "### 夜魇方的表现\n\n巨牙海民参战 100%。\n\n"
            "### 一句话总结\n\n天辉节奏更好。"
        )
        out = summarizer.format_analysis(
            model_text, [match_card], kind=tpl.KIND_MATCH
        )
        for title in ("天辉方的表现", "夜魇方的表现", "一句话总结"):
            self.assertIn(f"### {title}", out)


class TestComposeTitleByKind(unittest.TestCase):
    """main.py 拼接评价区块时，标题必须随查询种类切换。"""

    def _plugin(self):
        from astrbot_plugin_dota2assistant_plus.main import Dota2AssistantPlugin

        return object.__new__(Dota2AssistantPlugin)

    def test_match_card_uses_analysis_title(self):
        md = self._plugin()._compose_card_markdown(
            ["# 比赛详情 #1\n\n| 项目 | 数据 |"], "总结内容"
        )
        self.assertIn("## 综合分析", md)
        self.assertNotIn("## 点评", md)

    def test_player_card_uses_review_title(self):
        md = self._plugin()._compose_card_markdown(
            ["# 玩家资料：x\n\n| 段位 | 万古流芳 |"], "点评内容"
        )
        self.assertIn("## 点评", md)
        self.assertNotIn("## 综合分析", md)

    def test_unknown_card_keeps_old_title(self):
        md = self._plugin()._compose_card_markdown(["# 战绩\n"], "内容")
        self.assertIn("## 综合分析", md)


class TestAnalysisBoldNames(unittest.TestCase):
    """综合分析：英雄名要加粗（只加粗、不放大），正文与栏目标题分层。"""

    # 新版卡片：英雄名写在头像图片的 alt 里
    _CARD = (
        f"### ✅ ![暗夜魔王]({PORTRAIT_BASE_URL}night_stalker_vert.jpg) · Lv.19 · 匿名玩家 · KDA **12/6/12**\n"
        f"### ✅ ![巨牙海民]({PORTRAIT_BASE_URL}tusk_vert.jpg) · Lv.17 · KAKA · KDA **4/3/32**\n"
    )

    def test_bolding_helpers_exist(self):
        """英雄名加粗由代码兜底，不依赖模型自觉。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        self.assertTrue(hasattr(summarizer, "_match_terms"))
        self.assertTrue(hasattr(summarizer, "_bold_terms"))

    def test_match_terms_reads_hero_names_from_portrait_alt(self):
        """英雄名要从头像图片的 alt 里取，不能把玩家昵称也当成要点。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        terms = summarizer._match_terms([self._CARD])
        self.assertIn("暗夜魔王", terms)
        self.assertIn("巨牙海民", terms)
        # 昵称/占位词绝不进词表：总结里不该出现匿名玩家
        self.assertNotIn("KAKA", terms)
        self.assertNotIn("匿名玩家", terms)

    def test_bold_terms_wraps_and_skips_already_bold(self):
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        out = summarizer._bold_terms("暗夜魔王表现优异，巨牙海民参战率高。",
                                     ["暗夜魔王", "巨牙海民"])
        self.assertIn("**暗夜魔王**", out)
        self.assertIn("**巨牙海民**", out)
        # 已加粗的片段不再嵌套，避免 **** 这种破碎标记
        self.assertEqual(
            summarizer._bold_terms("**暗夜魔王**很强", ["暗夜魔王"]),
            "**暗夜魔王**很强",
        )

    def test_template_bolds_strong_in_analysis_without_enlarging(self):
        """card.html：分析正文的 strong 恢复加粗，但不再放大字号。"""
        from pathlib import Path

        import astrbot_plugin_dota2assistant_plus.core.card_renderer as cr

        html = (Path(cr.__file__).resolve().parent.parent
                / "assets" / "card.html").read_text(encoding="utf-8")
        self.assertIn("#content>.analysis-body{", html)
        # 三属性都要在：变色 + 加粗 + **不**放大
        self.assertRegex(
            html,
            r"#content>\.analysis-body strong\{[^}]*font-weight:700[^}]*\}",
        )
        self.assertRegex(
            html,
            r"#content>\.analysis-body strong\{[^}]*font-size:inherit[^}]*\}",
        )
        # 「再放大一号」曾让同一段字号忽大忽小，已明确去掉
        self.assertNotIn("font-size:1.1em", html)

    def test_analysis_body_keeps_20px(self):
        """正文本身仍是 20px —— 去掉的是「再放大一号」，不是正文尺寸。"""
        from pathlib import Path

        import astrbot_plugin_dota2assistant_plus.core.card_renderer as cr

        html = (Path(cr.__file__).resolve().parent.parent
                / "assets" / "card.html").read_text(encoding="utf-8")
        self.assertIn("font-size:20px", html)

    def test_pillow_analysis_bolds_hero_names(self):
        """本地兜底路径：分析正文里的英雄名同样要描边（描边即加粗）。

        走完整链路：模型的纯文字 → format_analysis 补小标题并加粗英雄名 →
        Pillow 绘制。锚定在**含英雄名的那一句**上：英雄名必须带描边，
        同一句里的普通文字仍是常规字重（描边会切分成不同的绘制调用）。
        """
        from PIL import ImageDraw

        import astrbot_plugin_dota2assistant_plus.core.image_renderer as ir
        from astrbot_plugin_dota2assistant_plus.core.summarizer import (
            format_analysis,
        )

        model_text = (
            "### 天辉方的表现\n\n暗夜魔王表现优异，经济 18.6k，参战 100%。\n\n"
            "### 夜魇方的表现\n\n巨牙海民参战率最高，但经济落后。\n\n"
            "### 一句话总结\n\n天辉的节奏更好。"
        )
        analysis = format_analysis(model_text, [self._CARD])
        md = self._CARD + "\n## 综合分析\n\n" + analysis + "\n"

        calls: list[tuple[str, int]] = []
        real = ImageDraw.ImageDraw.text

        def spy(self_img, xy, text, *a, **kw):
            calls.append((text, kw.get("stroke_width", 0)))
            return real(self_img, xy, text, *a, **kw)

        ImageDraw.ImageDraw.text = spy
        try:
            ir.render_text_to_image(md, "/tmp/_analysis_bold.png")
        finally:
            ImageDraw.ImageDraw.text = real

        bold = [t for t, s in calls if s == 1 and "暗夜魔王" in t]
        self.assertTrue(bold, "分析正文里的英雄名没有被加粗")
        plain = [t for t, s in calls if s == 0 and "表现优异" in t]
        self.assertTrue(plain, "英雄名之外的正文不应被加粗")

    def test_prompt_forbids_anonymous_placeholders(self):
        """提示词必须明确要求用英雄名、禁止输出昵称/「匿名玩家」。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        prompt = summarizer._SYSTEM_PROMPT
        self.assertIn("英雄名", prompt)
        self.assertIn("匿名玩家", prompt)
        self.assertIn("不要写玩家昵称", prompt)


class TestAnalysisThreeSections(unittest.TestCase):
    """赛后总结拆成三个可见小栏目：天辉方的表现 / 夜魇方的表现 / 一句话总结。"""

    _CARD = (
        f"### ✅ ![暗夜魔王]({PORTRAIT_BASE_URL}night_stalker_vert.jpg) · Lv.19 · 匿名玩家 · KDA **12/6/12**\n"
        f"### ✅ ![巨牙海民]({PORTRAIT_BASE_URL}tusk_vert.jpg) · Lv.17 · KAKA · KDA **4/3/32**\n"
    )

    def test_section_titles_are_fixed(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _SECTION_TITLES

        self.assertEqual(
            _SECTION_TITLES, ("天辉方的表现", "夜魇方的表现", "一句话总结"))

    def test_prompt_asks_for_the_three_subheadings(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _SYSTEM_PROMPT

        for title in ("天辉方的表现", "夜魇方的表现", "一句话总结"):
            with self.subTest(title=title):
                self.assertIn(f"### {title}", _SYSTEM_PROMPT)

    def _formatted(self, text):
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        return summarizer.format_analysis(text, [self._CARD])

    def test_model_output_with_h3_headings_is_normalised(self):
        """模型按提示词写 ### 小标题时，原样保留并补齐固定标题。"""
        text = (
            "### 天辉方的表现\n\n暗夜魔王 19/6/12，经济 18.6k，参战 100%。\n\n"
            "### 夜魇方的表现\n\n巨牙海民 4/3/32，经济 9.1k，参战 86%。\n\n"
            "### 一句话总结\n\n天辉靠暗夜魔王带线赢得比赛。"
        )
        out = self._formatted(text)
        self.assertIn("### 天辉方的表现", out)
        self.assertIn("### 夜魇方的表现", out)
        self.assertIn("### 一句话总结", out)
        # 加粗由代码补，模型没写也照样粗
        self.assertIn("**暗夜魔王**", out)
        self.assertIn("**巨牙海民**", out)
        # 内容一字不动
        self.assertIn("经济 18.6k", out)
        self.assertIn("带线赢得比赛", out)

    def test_numbered_output_is_also_split(self):
        """模型偶尔沿用旧写法「1. 天辉方胜负关键：」，也要能切出来。"""
        text = (
            "1. 天辉方胜负关键：暗夜魔王经济 18.6k，参战 100%。\n"
            "2. 夜魇方胜负关键：巨牙海民参战 86%，但经济落后。\n"
            "3. 一句话总结：天辉的团战节奏更好。"
        )
        out = self._formatted(text)
        self.assertIn("### 天辉方的表现", out)
        self.assertIn("### 夜魇方的表现", out)
        self.assertIn("### 一句话总结", out)
        self.assertIn("**暗夜魔王**", out)
        # 序号与「天辉方胜负关键」这类标题文字必须被剥掉，不能留在正文里
        self.assertNotIn("1. 天辉方胜负关键", out)
        self.assertNotIn("3. 一句话总结", out)
        # 标题行冒号后的正文不能被丢掉
        self.assertIn("团战节奏更好", out)

    def test_focus_hero_point_is_split_out_of_conclusion(self):
        """点名英雄的第 4 段要单独成栏，不能被并进「一句话总结」。

        它排在最后，若不切出来，「一句话总结」里会跟着一整段点评，
        标题就名不副实了。
        """
        text = (
            "### 天辉方的表现\n\n暗夜魔王经济 18.6k。\n\n"
            "### 夜魇方的表现\n\n巨牙海民参战 86%。\n\n"
            "### 一句话总结\n\n天辉节奏更好。\n\n"
            "4. 点名英雄：火猫本局 15/4/9，经济 22.1k，表现超预期。"
        )
        out = self._formatted(text)
        self.assertIn("### 点名英雄", out)
        # 「一句话总结」必须只剩那一句话
        tail = out.split("### 一句话总结")[1].split("### 点名英雄")[0]
        self.assertEqual(tail.strip(), "天辉节奏更好。")
        # 序号与「点名英雄：」这类标题文字都要剥掉
        self.assertNotIn("4. 点名英雄", out)
        self.assertIn("火猫本局 15/4/9", out)

    def test_inline_number_does_not_fake_a_focus_section(self):
        """正文里的「4.5」不能被当成第 4 段的分段点。"""
        text = (
            "### 天辉方的表现\n\n暗夜魔王经济 18.6k，参战 4.5 次每分。\n\n"
            "### 夜魇方的表现\n\n巨牙海民参战 86%。\n\n"
            "### 一句话总结\n\n天辉节奏更好。"
        )
        out = self._formatted(text)
        self.assertNotIn("### 点名英雄", out)
        self.assertIn("参战 4.5 次每分", out)

    def test_clean_keeps_a_leading_section_heading(self):
        """模型第一行就写栏目名时，_clean 不能把它当多余标题删掉。

        删掉的后果是首段失去小标题、整段退化成没标题的正文，
        而且后面的分栏也跟着定位不到。
        """
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        raw = ("### 天辉方的表现\n\n敌法师经济 18.6k。\n\n"
               "### 夜魇方的表现\n\n斧王先手果断。\n\n"
               "### 一句话总结\n\n天辉赢在带线。")
        self.assertTrue(summarizer._clean(raw).startswith("### 天辉方的表现"))
        out = summarizer.format_analysis(summarizer._clean(raw), [])
        self.assertIn("### 天辉方的表现", out)
        self.assertIn("敌法师经济 18.6k", out)

    def test_clean_still_drops_a_redundant_top_heading(self):
        """模型把「# 赛后总结」当大标题写出来时，那一行仍然是多余的。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        raw = "# 赛后总结\n\n### 天辉方的表现\n\n敌法师经济 18.6k。"
        cleaned = summarizer._clean(raw)
        self.assertFalse(cleaned.startswith("# 赛后总结"))
        self.assertIn("### 天辉方的表现", cleaned)

    def test_hash_heading_used_as_section_is_kept(self):
        """「# 总结」既可能是多余大标题，也可能是第三个栏目的标题。

        判据是它后面还有没有别的栏目标题：有（说明它排在结尾当栏目用）
        就保留并统一成「### 一句话总结」；没有就是多余标题，删掉。
        """
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        raw = ("### 天辉方的表现\n\n敌法师。\n\n"
               "### 夜魇方的表现\n\n斧王。\n\n"
               "# 总结\n\n带线。")
        out = summarizer.format_analysis(summarizer._clean(raw), [])
        self.assertIn("### 一句话总结", out)
        self.assertIn("带线。", out)

    def test_missing_section_keeps_other_titles_correct(self):
        """模型整段漏掉某一栏时，标题必须跟着内容走、不能按顺序硬配。

        若按出现顺序配标题，漏掉夜魇后「一句话总结」的正文会被挂到
        「夜魇方的表现」下面 —— 张冠李戴比没有小标题更糟。
        """
        text = ("### 天辉方的表现\n\n敌法师经济 18.6k。\n\n"
                "### 一句话总结\n\n天辉赢在带线。")
        out = self._formatted(text)
        self.assertIn("### 天辉方的表现", out)
        self.assertIn("### 一句话总结", out)
        # 夜魇那一栏没有内容，就不该出现它的标题
        self.assertNotIn("### 夜魇方的表现", out)
        # 各段正文必须挂在自己的标题下
        self.assertIn("天辉赢在带线", out.split("### 一句话总结")[1])

    def test_truncated_summary_keeps_labelled_sections(self):
        """截断导致后面的栏目没了时，剩下的段仍要带上正确的小标题。

        总结超过 summary_max_chars 会被截断，第三段可能整个消失。
        若不补标题，卡片上会退回「1. 天辉方胜负关键：…」这种没有栏目的原文 ——
        正是这次要修掉的样子。
        """
        reply = (
            "1. 天辉方胜负关键：敌法师是本局核心，经济 18.6k、XPM 672。\n"
            "2. 夜魇方胜负关键：斧王经济 14.0k、XPM 450。\n"
            "3. 一句话总结：天辉靠敌法师的带线节奏赢下比赛。"
        )
        for keep, expect in ((len(reply), 3), (60, 2), (38, 1)):
            with self.subTest(keep=keep):
                out = self._formatted(reply[:keep])
                self.assertIn("### 天辉方的表现", out)
                # 行首不能残留带序号的旧式标题
                for line in out.splitlines():
                    if line.startswith(("1.", "2.", "3.")):
                        self.fail(f"残留旧式标题: {line!r}")
                self.assertEqual(out.count("### "), expect)

    def test_free_prose_starting_with_team_name_is_not_a_section(self):
        """正文恰好以「天辉」开头，不能当成栏目标题硬补小标题。"""
        for text in ("天辉前期压制力不足，中期被翻盘。",
                     "夜魇方经济领先却没有推塔。"):
            with self.subTest(text=text):
                out = self._formatted(text)
                self.assertNotIn("###", out)
                self.assertEqual(out, text)

    def test_backticks_from_prompt_example_are_stripped(self):
        """模型抄提示词示例时带出的反引号必须清掉。

        提示词用 ``**英雄名**`` 举例说明加粗写法，GLM 会把那对反引号一起
        抄进正文（``**敌法师**``）。网络 t2i 会**原样画出反引号**，
        本地 Pillow 又会把它当格式标记清掉 —— 同一份总结两条路径长相不同。
        """
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        raw = ("### 天辉方的表现\n\n``**暗夜魔王**`` 经济 18.6k，表现亮眼。\n\n"
               "### 夜魇方的表现\n\n``**巨牙海民**`` 参战率 100%。\n\n"
               "### 一句话总结\n\n天辉赢在带线。")
        out = summarizer._clean(raw)
        self.assertNotIn("`", out)
        # 加粗标记本身要留住
        self.assertIn("**暗夜魔王**", out)

    def test_overlong_summary_keeps_conclusion_intact(self):
        """模型写超时按栏目精简，绝不能砍掉结尾的「一句话总结」。

        先截断再分栏的话，被砍掉的永远是最后那一栏 ——
        读者看到的就是「总结显示不完整」，这正是要修的现象。
        """
        long = ("### 天辉方的表现\n\n"
                + "暗夜魔王经济 18.6k、XPM 672、KDA 10/2/5、参战率 100%，带线牵制成功。" * 8
                + "\n\n### 夜魇方的表现\n\n"
                + "巨牙海民经济 14.0k、XPM 450、KDA 5/8/15，先手果断但成型偏慢。" * 8
                + "\n\n### 一句话总结\n\n天辉靠暗夜魔王的带线节奏赢下比赛。")
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        # 用带这两名英雄的卡片，英雄名才会被加粗
        out = summarizer.format_analysis(long, [self._CARD], max_chars=500)
        self.assertIn("### 一句话总结", out)
        # 结论文本一字不少
        self.assertIn("天辉靠**暗夜魔王**的带线节奏赢下比赛。", out)
        # 各栏都在，没有哪一栏被整段砍掉
        for title in ("天辉方的表现", "夜魇方的表现"):
            with self.subTest(title=title):
                self.assertIn(f"### {title}", out)
        # 正文总额度受控（标题不计入）
        import re as _re
        parts = [p for p in _re.split(r"### [^\n]+", out) if p.strip()]
        self.assertLessEqual(sum(len(p.strip()) for p in parts), 500 + 40)

    def test_within_budget_is_left_untouched(self):
        """没超上限时不做任何精简。"""
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        text = ("### 天辉方的表现\n\n敌法师经济 18.6k。\n\n"
                "### 夜魇方的表现\n\n斧王参战 100%。\n\n"
                "### 一句话总结\n\n天辉赢在带线。")
        out = summarizer.format_analysis(text, [self._CARD], max_chars=500)
        self.assertIn("敌法师经济 18.6k。", out)
        self.assertIn("斧王参战 100%。", out)
        self.assertIn("天辉赢在带线。", out)

    def test_default_budget_is_500(self):
        """默认上限 500 字，与 schema 一致。"""
        import inspect

        from astrbot_plugin_dota2assistant_plus.core import summarizer

        src = inspect.getsource(summarizer.summarize)
        self.assertIn('"summary_max_chars", 500', src)

    def test_freeform_output_falls_back_to_bold_only(self):
        """切不出三个栏目时不能硬套标题 —— 宁可只加粗。"""
        text = "这场比赛的转折点在第 30 分钟，暗夜魔王带线牵制很成功。"
        out = self._formatted(text)
        self.assertNotIn("### 天辉方的表现", out)
        self.assertNotIn("### 夜魇方的表现", out)
        self.assertIn("**暗夜魔王**", out)
        self.assertIn("带线牵制很成功", out)

    def test_empty_input_stays_empty(self):
        self.assertEqual(self._formatted(""), "")
        self.assertEqual(self._formatted("   \n  "), "")

    def test_composed_card_puts_sections_under_analysis_heading(self):
        """卡片正文里，三个小栏目必须挂在「## 综合分析」之下。"""
        from astrbot_plugin_dota2assistant_plus.main import (
            Dota2AssistantPlugin,
        )

        plugin = object.__new__(Dota2AssistantPlugin)
        text = (
            "### 天辉方的表现\n\n暗夜魔王经济 18.6k。\n\n"
            "### 夜魇方的表现\n\n巨牙海民参战 86%。\n\n"
            "### 一句话总结\n\n比赛节奏决定胜负。"
        )
        card = plugin._compose_card_markdown(["## 比赛详情 #1"], text)
        self.assertLess(card.index("## 综合分析"),
                        card.index("### 天辉方的表现"))
        for title in ("天辉方的表现", "夜魇方的表现", "一句话总结"):
            with self.subTest(title=title):
                self.assertIn(f"### {title}", card)

    def test_pillow_renders_subheadings_smaller_than_body(self):
        """本地路径：小栏目标题要比正文小一号，否则层级就反了。"""
        import astrbot_plugin_dota2assistant_plus.core.image_renderer as ir

        self.assertLess(ir._ANALYSIS_SUBHEAD_SIZE, ir._ANALYSIS_FONT_SIZE)

    def test_pillow_treats_analysis_h3_as_subheading_not_player(self):
        """分析块里的 ### 是栏目标题，不能被当成玩家区块（那会找立绘并放大）。

        用高度区分：玩家区块会为头像撑到 96px 以上，小栏目标题按 19px 排，
        连行高加留白也远低于 96px。若 ### 被误判为玩家区块，高度会明显变大。
        """
        import os

        import astrbot_plugin_dota2assistant_plus.core.image_renderer as ir

        head = "\n## 综合分析\n\n### 天辉方的表现\n\n暗夜魔王经济 18.6k。\n"
        with_h3 = self._CARD + head
        without = self._CARD + "\n## 综合分析\n\n暗夜魔王经济 18.6k。\n"
        p1 = ir.render_text_to_image(with_h3, "/tmp/_analysis_h3.png")
        p2 = ir.render_text_to_image(without, "/tmp/_analysis_no_h3.png")
        h1 = self._height(p1)
        h2 = self._height(p2)
        # 多出的标题只应占「一行 19px + 留白」的量级，绝不能是头像级的高度
        self.assertGreater(h1, h2)
        self.assertLess(h1 - h2, ir._AVATAR_HEIGHT)

    @staticmethod
    def _height(path: str) -> int:
        from PIL import Image

        with Image.open(path) as img:
            return img.size[1]


class TestImageOutputSwitch(unittest.TestCase):
    """图片输出开关：默认开启，关闭后改发 Markdown。"""

    def _plugin_config_default(self, key):
        import json
        from pathlib import Path

        from astrbot_plugin_dota2assistant_plus.core import summarizer

        pkg = Path(summarizer.__file__).resolve().parent.parent
        schema = json.loads((pkg / "_conf_schema.json").read_text(encoding="utf-8"))
        return schema[key].get("default")

    def test_default_is_on(self):
        """默认必须开启，保持原有行为。"""
        self.assertIs(self._plugin_config_default("enable_image_output"), True)

    def test_default_is_on_in_dev_fallback(self):
        """main.py 读不到配置项时也要默认出图。"""
        import inspect

        from astrbot_plugin_dota2assistant_plus import main

        src = inspect.getsource(main.Dota2AssistantPlugin.__init__)
        self.assertIn('"enable_image_output", True', src)

    def test_hook_branches_to_text_when_disabled(self):
        """关掉图片后出图函数要退化为纯文本发送。"""
        import inspect

        from astrbot_plugin_dota2assistant_plus import main

        # 重构后「按开关决定发图还是发文」的逻辑收敛在 _send_card 里
        src = inspect.getsource(main.Dota2AssistantPlugin._send_card)
        self.assertIn("enable_image_output", src)
        self.assertIn("plain_result", src)

    def test_slash_command_follows_switch(self):
        """关掉图片后 /dota 也要默认走文本，否则用户会以为配置没生效。"""
        import inspect

        from astrbot_plugin_dota2assistant_plus import main

        src = inspect.getsource(main.Dota2AssistantPlugin._extract_output_mode)
        self.assertIn("enable_image_output", src)

    def test_hint_is_mode_neutral(self):
        from astrbot_plugin_dota2assistant_plus.tools.delivery import _HINT

        self.assertIn("Markdown", _HINT)
        self.assertNotIn("已作为图片发送给用户，", _HINT)


class TestLocalRendererLongTitle(unittest.TestCase):
    """本地兜底渲染：标题行变长后必须折行，不能被裁到卡片外。

    背景：标题行现在承载「英雄 · 等级 · 玩家ID · KDA」，玩家昵称可能很长
    （英文/俄文昵称没有长度上限）。本地渲染器的 h3 原先不折行，会直接画到
    右边界之外；网络 t2i 有 CSS 会自动换行，所以这个 bug 只在兜底路径出现。
    """

    def _rightmost_ink(self, path: str) -> bool:
        """图片最右一列是否有正文像素（即文字被截断）。

        顶部 6 行是强调色条（``draw.rectangle([0, 0, img_w, 3])``），
        覆盖最右列，需排除；底部波浪装饰纹已随本次改版删除，无需再扣。
        """
        from PIL import Image

        im = Image.open(path).convert("RGB")
        bg = im.getpixel((5, 40))
        for y in range(8, im.height):
            p = im.getpixel((im.width - 1, y))
            if not (abs(p[0] - bg[0]) < 12 and abs(p[1] - bg[1]) < 12
                    and abs(p[2] - bg[2]) < 12):
                return True
        return False

    def test_long_title_wraps_instead_of_clipping(self):
        import tempfile
        from pathlib import Path

        from astrbot_plugin_dota2assistant_plus.core.image_renderer import render_text_to_image

        title = "### ❌ 拉比克 · Lv.15 · " + "玩家昵称超长" * 12 + " · KDA **2/11/7**"
        md = f"## 天辉\n\n{title}\n金钱 **18.6k** ｜ GPM **631**\n装备：黑皇杖\n"
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "long.png")
            render_text_to_image(md, out)
            self.assertFalse(
                self._rightmost_ink(out),
                "标题行未折行，被裁到卡片边界外",
            )

    def test_normal_title_still_renders(self):
        """常规长度标题不应因折行改动而丢失内容。"""
        import tempfile
        from pathlib import Path

        from astrbot_plugin_dota2assistant_plus.core.image_renderer import render_text_to_image

        md = ("## 天辉\n\n### ✅ 敌法师 · Lv.19 · miracle · KDA **10/2/5**\n"
              "金钱 **18.6k** ｜ GPM **631**\n装备：黑皇杖\n")
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "ok.png")
            render_text_to_image(md, out)
            from PIL import Image

            self.assertGreater(Image.open(out).size[1], 50)


class TestMatchCardLayout(unittest.TestCase):
    """战报卡本地渲染：浅色/深色主题 + 独立版式。

    原先这些用例测的是通用区块流里的 ``_measure_player``（头像竖跨三行、
    数据行一行八项）。现在战报卡走独立渲染路径 ``render_match_card``，
    因此这里改为直接对输出图片做像素级结构断言。
    """

    def _render(self, theme: dict | None = None, **match_kwargs):
        import tempfile
        from pathlib import Path as _Path
        from unittest.mock import patch

        from PIL import Image as _Image
        from astrbot_plugin_dota2assistant_plus.core import image_renderer as ir
        from astrbot_plugin_dota2assistant_plus.core.models import MatchDetail, MatchPlayer
        from astrbot_plugin_dota2assistant_plus.core.templates import render_match_detail

        kwargs = dict(
            hero_name="敌法师", hero_id=1, level=19, persona_name="miracle",
            kills=10, deaths=2, assists=5, gpm=631, xpm=672,
            hero_damage=25000, tower_damage=10202, hero_healing=1234,
            last_hits=198, denies=9, net_worth=18586,
            items=["116", "50", "151"], neutral_items=["577"],
            is_radiant=True, win=True,
        )
        kwargs.update(match_kwargs)
        md = render_match_detail(MatchDetail(
            match_id=1, radiant_win=True, radiant_score=41, dire_score=28,
            players=[MatchPlayer(**kwargs)],
        ))
        # 用一张与真实 antimage_vert.jpg 同比例的纯色图替代立绘，避免测试依赖缓存
        fake = _Image.new("RGBA", (235, 272), (30, 90, 200, 255))
        with patch.object(ir, "_open_image", return_value=fake),              tempfile.TemporaryDirectory() as d:
            out = str(_Path(d) / "c.png")
            ir.render_text_to_image(md, out, theme=theme)
            return _Image.open(out).convert("RGB")

    def test_light_background_is_white(self):
        img = self._render()
        w, h = img.size
        self.assertEqual(img.getpixel((20, 20)), (255, 255, 255),
                         "浅色主题左上角应为纯白")
        self.assertEqual(img.getpixel((w - 20, 20)), (255, 255, 255),
                         "浅色主题右上角应为纯白")

    def test_mvp_name_is_deep_blue(self):
        """MVP 大字在白底上用 #1e3a8a，且有一定面积。"""
        img = self._render()
        px = img.load()
        target = (30, 58, 138)
        count = sum(
            1 for y in range(40, 160) for x in range(40, 400)
            if all(abs(px[x, y][i] - target[i]) <= 25 for i in range(3))
        )
        self.assertGreater(count, 200, "未找到足够的 MVP 深蓝像素")

    def test_score_plate_is_light(self):
        """比分区在横幅右下方，应有近不透明的浅色衬板。"""
        img = self._render()
        px = img.load()
        # 衬板大约在 x 600~800、y 130~200 之间；取右下局部亮度均值
        samples = [px[x, y] for y in range(140, 190) for x in range(640, 790)]
        import statistics
        mean_lum = statistics.mean((r + g + b) / 3 for r, g, b in samples)
        self.assertGreater(mean_lum, 220, "比分衬板不够亮")

    def test_metric_columns_present(self):
        """6 个指标列在玩家行都有墨点，且数值/标签按列居中。"""
        img = self._render()
        px = img.load()
        # 版式重排后：指标条占满整行（宽 780，col_w=130），每列中心在
        # 32 + i*130 + 65 = 97/227/357/487/617/747；数值居中于该列。
        # 只要求每个列中心附近有足够多的深色像素。
        for i, cx in enumerate((97, 227, 357, 487, 617, 747)):
            dark = sum(
                1 for y in range(330, 385) for x in range(cx - 55, cx + 55)
                if (px[x, y][0] + px[x, y][1] + px[x, y][2]) / 3 < 100
            )
            self.assertGreater(dark, 40, f"指标列 {i + 1} 几乎没有墨点")

    def test_dark_theme_switches_background(self):
        """theme 传 dark=True 时背景应明显变暗。"""
        light = self._render(theme={})
        dark = self._render(theme={"dark": True, "bg": "#06090e", "panel": "#0d1117"})
        self.assertLess(sum(dark.getpixel((20, 20))),
                        sum(light.getpixel((20, 20))) - 300,
                        "深色主题背景没有变暗")


class TestCardTemplatePlayerLayout(unittest.TestCase):
    """网络 t2i 模板：第一行字号 + 头像竖跨三行的 CSS/JS 必须都在。"""

    def _template(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parent.parent
                / "assets" / "card.html").read_text(encoding="utf-8")

    def test_heading_is_enlarged_but_not_doubled(self):
        """标题行 24px：比正文大，但不再是 2 倍的 36px。"""
        html = self._template()
        self.assertIn("font-size:24px", html)
        self.assertNotIn("font-size:36px", html)

    def test_heading_has_no_per_fragment_size_override(self):
        """整行同号：不能给 h3 里的 code 之类片段单独改字号。"""
        html = self._template()
        self.assertNotIn("h3 code", html)

    def test_portrait_spans_three_rows(self):
        html = self._template()
        self.assertIn("grid-row:1 / span 3", html)
        self.assertIn(".player-block", html)
        self.assertIn("hero-portrait", html)

    def test_grouping_script_runs_after_render(self):
        """分组脚本必须在 marked.parse 之后调用，否则 DOM 还没生成。"""
        html = self._template()
        self.assertIn("groupPlayerBlocks(el)", html)
        self.assertLess(html.index("marked.parse(src)"),
                        html.index("groupPlayerBlocks(el)"))

    def test_broken_portrait_is_hidden(self):
        html = self._template()
        self.assertIn("data-broken", html)
        self.assertIn("guardBrokenPortraits(el)", html)

    def test_grouping_strips_leading_separator(self):
        """网络路径也要去掉「摘走立绘后」行首悬空的「· 」分隔符。"""
        html = self._template()
        self.assertIn(r"/^\s*·\s*/", html)


class TestOrderedListRendering(unittest.TestCase):
    """赛后总结固定输出 1./2./3./4. 四点，本地兜底渲染要认得出有序列表。"""

    def test_numbered_lines_become_list_items(self):
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import _classify_line

        for raw, expect_num, text in (
            ("1. 阵容BP点评", "1", "阵容BP点评"),
            ("2、胜负手", "2", "胜负手"),
            ("10) 第十点", "10", "第十点"),
        ):
            with self.subTest(raw=raw):
                kind, content = _classify_line(raw)
                self.assertEqual(kind, "li")
                self.assertTrue(content.startswith(expect_num + "."))
                self.assertIn(text, content)

    def test_plain_sentence_is_not_a_list(self):
        """普通句子里出现数字不能被误判成列表。"""
        from astrbot_plugin_dota2assistant_plus.core.image_renderer import _classify_line

        for raw in ("他拿了 2 个人头", "攻速提升 1.5 倍", "1.5倍攻速"):
            with self.subTest(raw=raw):
                kind, _ = _classify_line(raw)
                self.assertEqual(kind, "p")

    def test_four_points_render_into_image(self):
        import tempfile
        from pathlib import Path

        from astrbot_plugin_dota2assistant_plus.core.image_renderer import render_text_to_image

        md = ("## 综合分析\n\n"
              "1. 阵容BP：天辉更优。\n"
              "2. 胜负手：熊战士 21.1k。\n"
              "3. 失利责任：拉比克 10.4k。\n"
              "4. 未达预期：宙斯。\n")
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "four.png")
            render_text_to_image(md, out)
            from PIL import Image

            self.assertGreater(Image.open(out).size[1], 50)


class TestBpSectionRemoved(unittest.TestCase):
    """阵容 BP 模块按要求删除：卡片不再渲染该区块。"""

    def _match(self, picks_bans):
        return MatchDetail(
            match_id=1, radiant_win=True, radiant_score=41, dire_score=28,
            picks_bans=picks_bans,
            players=[MatchPlayer(hero_name="敌法师", is_radiant=True, win=True)],
        )

    def test_no_bp_section_even_with_data(self):
        md = render_match_detail(self._match([
            {"is_pick": True, "hero_id": 22, "team": 0, "order": 0},
            {"is_pick": False, "hero_id": 60, "team": 1, "order": 1},
        ]))
        self.assertNotIn("阵容 BP", md)
        self.assertNotIn("选人", md)
        self.assertNotIn("禁用", md)

    def test_match_card_still_renders_teams(self):
        """删掉 BP 后，天辉/夜魇两个阵容区块仍须保留。"""
        md = render_match_detail(self._match([]))
        self.assertIn("## 天辉", md)
        self.assertIn("## 夜魇", md)
        self.assertIn("比赛详情", md)


class TestCombatRate(unittest.TestCase):
    """参战率 = K+A+D 相对队友的高低，不依赖比赛是否被解析。"""

    def _team(self):
        # 天辉五人：(K,A,D) -> contacts
        #   敌法 10,5,2  = 17
        #   火猫  2,3,1  =  6   <- 避战
        #   冰女  1,12,4 = 17
        #   潮汐  3,9,3  = 15
        #   毒龙  6,7,5  = 18
        # 合计 73，队均 = 73/5 = 14.6
        return [
            MatchPlayer(hero_name="敌法师", kills=10, assists=5, deaths=2),
            MatchPlayer(hero_name="灰烬之灵", kills=2, assists=3, deaths=1),
            MatchPlayer(hero_name="水晶室女", kills=1, assists=12, deaths=4),
            MatchPlayer(hero_name="潮汐猎人", kills=3, assists=9, deaths=3),
            MatchPlayer(hero_name="冥界亚龙", kills=6, assists=7, deaths=5),
        ]

    def _rates(self):
        team = self._team()
        total = sum((p.kills or 0) + (p.assists or 0) + (p.deaths or 0) for p in team)
        return [_combat_rate(p, total, len(team)) for p in team]

    def test_counts_all_three_kda_terms(self):
        """击杀、助攻、死亡都算参战，缺任一项都会算错。"""
        # 只有击杀：6/(6*1)=100%
        self.assertEqual(_combat_rate(
            MatchPlayer(kills=6, assists=0, deaths=0), 6, 1), "100%")
        # 只有死亡也算参战
        self.assertEqual(_combat_rate(
            MatchPlayer(kills=0, assists=0, deaths=6), 6, 1), "100%")
        # 只有助攻也算参战
        self.assertEqual(_combat_rate(
            MatchPlayer(kills=0, assists=6, deaths=0), 6, 1), "100%")

    def test_average_player_is_hundred_percent(self):
        """K+A+D 正好等于队均时是 100%。

        team_contacts 传的是**全队五人 K+A+D 之和**（这里是 15*5=75），
        不是队均：公式为 contacts * team_size / team_contacts，
        五人都是 15 时为 15*5/75 = 100%。
        """
        self.assertEqual(_combat_rate(
            MatchPlayer(kills=5, assists=5, deaths=5), 15 * 5, 5), "100%")

    def test_avoidant_player_rates_low(self):
        """击杀/助攻/死亡都少 = 避战选手，参战率明显低。"""
        rates = self._rates()
        avoidant = rates[1]
        self.assertLess(int(avoidant.rstrip("%")), 50)
        for other in (rates[0], rates[2], rates[3], rates[4]):
            with self.subTest(other=other):
                self.assertGreater(int(other.rstrip("%")), int(avoidant.rstrip("%")))

    def test_active_player_rates_high(self):
        rates = self._rates()
        self.assertGreater(int(rates[4].rstrip("%")), 100)

    def test_no_team_contacts_returns_dash(self):
        """全队都没接触（异常数据）时返回「—」，不显示 0%。"""
        self.assertEqual(_combat_rate(MatchPlayer(kills=1), 0, 5), "--")
        self.assertEqual(_combat_rate(MatchPlayer(kills=1), 10, 0), "--")

    def test_rendered_in_player_block(self):
        p = MatchPlayer(hero_name="敌法师", kills=5, assists=5, deaths=5)
        block = _render_player_block(p, 75, 5)
        self.assertIn("参战 100%", block)

    def test_does_not_need_parsed_match(self):
        """只靠 K/A/D，因此未解析的比赛也能算出参战率。"""
        p = MatchPlayer(hero_name="敌法师", kills=10, assists=5, deaths=2)
        self.assertEqual(_combat_rate(p, 73, 5), "116%")


class TestSummarizerThreePoints(unittest.TestCase):
    """赛后总结固定三点：天辉关键 / 夜魇关键 / 一句话总结。"""

    def test_points_are_numbered_one_to_three(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _SYSTEM_PROMPT

        for n in ("1.", "2.", "3."):
            with self.subTest(n=n):
                self.assertIn(n, _SYSTEM_PROMPT)
        # 已由四点改为三点，不应再要求第 4 点
        self.assertNotIn("4. **", _SYSTEM_PROMPT)

    def test_covers_both_teams(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _SYSTEM_PROMPT

        self.assertIn("天辉", _SYSTEM_PROMPT)
        self.assertIn("夜魇", _SYSTEM_PROMPT)

    def test_each_team_point_asks_for_four_metrics(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _SYSTEM_PROMPT

        for kw in ("经济", "经验", "KDA", "参战率"):
            with self.subTest(kw=kw):
                self.assertIn(kw, _SYSTEM_PROMPT)

    def test_asks_for_one_sentence_conclusion(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _SYSTEM_PROMPT

        self.assertIn("一句话总结", _SYSTEM_PROMPT)

    def test_warns_against_cross_team_attribution(self):
        """实测模型会把天辉玩家写进夜魇第2点，提示词必须明确分段归属。"""
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _SYSTEM_PROMPT

        self.assertIn("不要跨段取人", _SYSTEM_PROMPT)

    def test_requires_kda_in_slash_format(self):
        """实测模型会漏掉 KDA，必须在提示词里明确格式与「缺一不可」。"""
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _SYSTEM_PROMPT

        self.assertIn("KDA 12/6/12", _SYSTEM_PROMPT)
        self.assertIn("四项缺一不可", _SYSTEM_PROMPT)

    def test_explains_avoidant_reading(self):
        """必须告诉模型：K+A+D 偏少 = 避战，参战率低是这个意思。"""
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _SYSTEM_PROMPT

        self.assertIn("避战", _SYSTEM_PROMPT)
        self.assertIn("战斗接触", _SYSTEM_PROMPT)

    def test_no_longer_mentions_bp_dimensions(self):
        """BP 模块已删除，提示词不应再要求点评 BP。"""
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _SYSTEM_PROMPT

        self.assertNotIn("英雄克制关系", _SYSTEM_PROMPT)
        self.assertNotIn("战场拉扯", _SYSTEM_PROMPT)


class TestFocusHero(unittest.TestCase):
    """点名英雄时额外点评一段。"""

    def _host_and_provider(self):
        from types import SimpleNamespace

        captured = {}

        class P:
            model_name = "fake"

            async def text_chat(self, **kw):
                captured.update(kw)
                return SimpleNamespace(completion_text="1. 天辉关键\n2. 夜魇关键\n3. 一句话总结")

        class Host:
            enable_llm_summary = True
            summary_provider_id = ""
            summary_timeout = 30
            summary_max_chars = 900
            context = None

        return Host(), P(), captured

    def _run(self, focus):
        import asyncio
        from unittest.mock import patch

        from astrbot_plugin_dota2assistant_plus.core import summarizer

        host, prov, captured = self._host_and_provider()

        async def fake_resolve(h, e):
            return prov

        with patch.object(summarizer, "_resolve_provider", fake_resolve):
            out = asyncio.run(
                summarizer.summarize(host, ["# 比赛数据"], None,
                                     focus_hero=focus))
        return out, captured

    def test_focus_hero_adds_fourth_point(self):
        out, captured = self._run("火猫")
        self.assertIn("火猫", captured["system_prompt"])
        self.assertIn("「4.」", captured["system_prompt"])
        self.assertTrue(out)

    def test_no_focus_hero_keeps_prompt_clean(self):
        _, captured = self._run("")
        self.assertNotIn("【额外要求】", captured["system_prompt"])


class TestMarkdownToPlain(unittest.TestCase):
    """纯文本路径（--text / 渲染失败兜底 / 斜杠命令）不能泄漏 CDN 链接。"""

    def test_icons_become_item_names(self):
        from astrbot_plugin_dota2assistant_plus.core.templates import markdown_to_plain

        out = markdown_to_plain(
            f"装备：![黑皇杖]({ICON_BASE_URL}black_king_bar.png) "
            f"![相位鞋]({ICON_BASE_URL}phase_boots.png)"
        )
        self.assertEqual(out, "装备：黑皇杖 相位鞋")
        self.assertNotIn("http", out)

    def test_full_card_has_no_url_left(self):
        from astrbot_plugin_dota2assistant_plus.core.templates import markdown_to_plain

        match = MatchDetail(
            match_id=1, radiant_win=True, radiant_score=1, dire_score=0,
            players=[MatchPlayer(hero_name="敌法师", level=19, items=["116", "50"],
                                is_radiant=True, win=True)],
        )
        plain = markdown_to_plain(render_match_detail(match))
        self.assertNotIn("http", plain)
        self.assertNotIn("![", plain)
        self.assertIn("黑皇杖", plain)
        self.assertIn("相位鞋", plain)

    def test_markup_stripped(self):
        from astrbot_plugin_dota2assistant_plus.core.templates import markdown_to_plain

        self.assertEqual(markdown_to_plain("KDA **10/2/5** ｜ `x`"), "KDA 10/2/5 ｜ x")

    def test_empty_input(self):
        from astrbot_plugin_dota2assistant_plus.core.templates import markdown_to_plain

        self.assertEqual(markdown_to_plain(""), "")
        self.assertEqual(markdown_to_plain(None), "")


class TestDeliveryTextMode(unittest.TestCase):
    """deliver_result 的纯文本分支必须已降级图片语法。"""

    def test_text_only_returns_plain(self):
        import asyncio

        from astrbot_plugin_dota2assistant_plus.tools.delivery import deliver_result

        md = f"装备：![黑皇杖]({ICON_BASE_URL}black_king_bar.png)"
        out = asyncio.run(deliver_result(None, md, text_only=True))
        self.assertEqual(out, "装备：黑皇杖")


class TestFixCrossSide(unittest.TestCase):
    """赛后总结「跨边取人」的代码级纠正。

    模型偶发把某方英雄写进另一方栏目（实测：夜魇的大地之灵被写进
    「天辉方的表现」）。``_fix_cross_side`` 检测到后，把写错的栏目整段
    换成基于真实数据的兜底文本，正确归属的另一栏目保持不动。
    """

    def _match(self):
        from astrbot_plugin_dota2assistant_plus.core.models import (
            MatchDetail,
            MatchPlayer,
        )

        players = [
            MatchPlayer(hero_id=1, hero_name="敌法师", is_radiant=True, win=False),
            MatchPlayer(hero_id=35, hero_name="狙击手", is_radiant=True, win=False),
            MatchPlayer(hero_id=107, hero_name="大地之灵", is_radiant=False, win=True),
            MatchPlayer(hero_id=86, hero_name="拉比克", is_radiant=False, win=True),
        ]
        return MatchDetail(match_id=1, radiant_win=False, players=players)

    def _hero_name(self, hid):
        return {1: "敌法师", 35: "狙击手", 107: "大地之灵", 86: "拉比克"}.get(hid, f"英雄#{hid}")

    def test_cross_side_hero_in_radiant_section_is_replaced(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _fix_cross_side

        bad = (
            "### 天辉方的表现\n\n天辉的大地之灵表现亮眼但未能救主。\n\n"
            "### 夜魇方的表现\n\n夜魇的大地之灵带动节奏，拉比克发挥出色。\n\n"
            "### 一句话总结\n\n夜魇凭大地之灵获胜。"
        )
        fixed = _fix_cross_side(bad, self._match(), self._hero_name)
        radiant_part = fixed.split("夜魇方的表现")[0]
        # 天辉栏目里不该再出现夜魇的大地之灵
        self.assertNotIn("大地之灵", radiant_part)
        # 夜魇栏目的大地之灵是正确归属，保留
        dire_part = fixed.split("夜魇方的表现")[1].split("一句话总结")[0]
        self.assertIn("大地之灵", dire_part)

    def test_correct_summary_is_untouched(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _fix_cross_side

        good = (
            "### 天辉方的表现\n\n敌法师与狙击手经济落后，对线被打穿。\n\n"
            "### 夜魇方的表现\n\n大地之灵带动节奏，拉比克发挥出色。\n\n"
            "### 一句话总结\n\n夜魇凭大地之灵获胜。"
        )
        fixed = _fix_cross_side(good, self._match(), self._hero_name)
        # 无跨边错误时原文返回
        self.assertEqual(fixed, good)


if __name__ == "__main__":
    unittest.main()
