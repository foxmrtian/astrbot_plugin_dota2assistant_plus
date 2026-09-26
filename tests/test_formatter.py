import unittest

from astrbot_plugin_dota2assistant_plus.core.templates import (
    fmt_avg_kda,
    fmt_duration,
    fmt_rank,
    render_hero_info,
    render_hero_list,
    render_item_info,
    render_live_games,
    render_match_detail,
    render_player_profile,
    render_pro_matches,
)
from astrbot_plugin_dota2assistant_plus.core.models import (
    HeroInfo,
    ItemInfo,
    LiveGame,
    MatchDetail,
    MatchPlayer,
    PlayerProfile,
    ProMatch,
    RecentMatch,
)


class TestHelpers(unittest.TestCase):
    def test_fmt_rank(self):
        self.assertEqual(fmt_rank(75), "超凡入圣 5星")
        self.assertEqual(fmt_rank(81, 100), "冠绝一世 1星 (排名 #100)")
        self.assertEqual(fmt_rank(0), "未知")

    def test_fmt_duration(self):
        self.assertEqual(fmt_duration(0), "未知")
        self.assertEqual(fmt_duration(65), "1:05")
        self.assertEqual(fmt_duration(3661), "1:01:01")

    def test_fmt_avg_kda(self):
        matches = [
            RecentMatch(match_id=1, hero_id=1, kills=10, deaths=2, assists=5),
            RecentMatch(match_id=2, hero_id=2, kills=3, deaths=8, assists=12),
        ]
        result = fmt_avg_kda(matches)
        self.assertIn("6.5", result)  # (10+3)/2
        self.assertIn("5.0", result)  # (2+8)/2
        self.assertIn("8.5", result)  # (5+12)/2


class TestPlayerFormatter(unittest.TestCase):
    def test_basic_profile(self):
        profile = PlayerProfile(
            account_id=123,
            persona_name="TestPlayer",
            rank_tier=75,
            estimated_mmr=5000,
        )
        rank_str = fmt_rank(profile.rank_tier, profile.leaderboard_rank)
        avg_kda = fmt_avg_kda([])
        result = render_player_profile(profile, [], rank_str, avg_kda)
        self.assertIn("TestPlayer", result)
        self.assertIn("超凡入圣", result)
        self.assertIn("5000", result)

    def test_profile_with_recent_matches(self):
        profile = PlayerProfile(account_id=123, persona_name="TestPlayer", rank_tier=55)
        matches = [
            RecentMatch(match_id=1, hero_id=1, hero_name="Anti-Mage", kills=10, deaths=2, assists=5,
                        duration_seconds=2400, win=True),
            RecentMatch(match_id=2, hero_id=2, hero_name="Axe", kills=3, deaths=8, assists=12,
                        duration_seconds=1800, win=False),
        ]
        rank_str = fmt_rank(profile.rank_tier)
        avg_kda = fmt_avg_kda(matches)
        result = render_player_profile(profile, matches, rank_str, avg_kda)
        self.assertIn("近期战绩", result)
        self.assertIn("1胜 1负", result)
        self.assertIn("Anti-Mage", result)
        self.assertIn("Axe", result)


class TestHeroFormatter(unittest.TestCase):
    def test_hero_info(self):
        hero = HeroInfo(
            id=1,
            name="npc_dota_hero_antimage",
            localized_name="Anti-Mage",
            primary_attr="agi",
            attack_type="Melee",
            roles=["Carry", "Escape", "Nuker"],
            base_str=21,
            base_agi=24,
            base_int=12,
            str_gain=1.6,
            agi_gain=2.8,
            int_gain=1.8,
            move_speed=310,
            pub_pick=100000,
            pub_win=52000,
        )
        result = render_hero_info(hero)
        self.assertIn("Anti-Mage", result)
        self.assertIn("敏捷", result)
        self.assertIn("52.0%", result)

    def test_hero_list(self):
        heroes = [
            HeroInfo(id=1, name="a", localized_name="HeroA", primary_attr="str", attack_type="Melee"),
            HeroInfo(id=2, name="b", localized_name="HeroB", primary_attr="agi", attack_type="Ranged"),
            HeroInfo(id=3, name="c", localized_name="HeroC", primary_attr="str", attack_type="Melee"),
        ]
        result = render_hero_list(heroes, filter_attr="str")
        self.assertIn("HeroA", result)
        self.assertIn("HeroC", result)
        self.assertNotIn("HeroB", result)


class TestItemFormatter(unittest.TestCase):
    def test_item_info(self):
        item = ItemInfo(
            name="blink",
            display_name="Blink Dagger",
            cost=2250,
            description="Teleport to a target point up to 1200 units away.",
        )
        result = render_item_info(item)
        self.assertIn("Blink Dagger", result)
        self.assertIn("2250", result)
        self.assertIn("Teleport", result)


class TestMatchFormatter(unittest.TestCase):
    def test_match_detail(self):
        match = MatchDetail(
            match_id=12345,
            duration_seconds=2400,
            radiant_win=True,
            radiant_score=45,
            dire_score=30,
            players=[
                MatchPlayer(hero_name="Anti-Mage", kills=10, deaths=2, assists=5,
                            gpm=650, hero_damage=25000, is_radiant=True, win=True),
                MatchPlayer(hero_name="Axe", kills=5, deaths=8, assists=15,
                            gpm=400, hero_damage=18000, is_radiant=False, win=False),
            ],
        )
        result = render_match_detail(match)
        self.assertIn("天辉 获胜", result)
        self.assertIn("Anti-Mage", result)
        self.assertIn("Axe", result)
        self.assertIn("45 : 30", result)


class TestLiveFormatter(unittest.TestCase):
    def test_live_games(self):
        games = [
            LiveGame(match_id=1, game_time=1200, radiant_score=20, dire_score=15,
                     team_name_radiant="Team A", team_name_dire="Team B", spectators=500),
        ]
        result = render_live_games(games)
        self.assertIn("Team A", result)
        self.assertIn("Team B", result)
        self.assertIn("500", result)

    def test_empty(self):
        result = render_live_games([])
        self.assertIn("没有", result)


class TestProFormatter(unittest.TestCase):
    def test_pro_matches(self):
        matches = [
            ProMatch(match_id=1, radiant_team_name="Team A", dire_team_name="Team B",
                     radiant_score=2, dire_score=1, radiant_win=True,
                     league_name="TI", duration_seconds=3600, start_time=1700000000),
        ]
        result = render_pro_matches(matches)
        self.assertIn("Team A", result)
        self.assertIn("Team B", result)
        self.assertIn("TI", result)

    def test_empty(self):
        result = render_pro_matches([])
        self.assertIn("暂无", result)


if __name__ == "__main__":
    unittest.main()
