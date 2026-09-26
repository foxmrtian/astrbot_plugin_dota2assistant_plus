from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HeroInfo:
    id: int
    name: str  # internal name like npc_dota_hero_antimage
    localized_name: str
    primary_attr: str  # str/agi/int
    attack_type: str  # Melee/Ranged
    roles: list[str] = field(default_factory=list)
    base_health: int = 0
    base_mana: int = 0
    base_armor: float = 0
    base_attack_min: int = 0
    base_attack_max: int = 0
    base_str: int = 0
    base_agi: int = 0
    base_int: int = 0
    str_gain: float = 0
    agi_gain: float = 0
    int_gain: float = 0
    move_speed: int = 0
    # win rate stats
    pro_pick: int = 0
    pro_win: int = 0
    pub_pick: int = 0
    pub_win: int = 0


@dataclass
class ItemInfo:
    name: str  # internal name
    display_name: str
    cost: int = 0
    description: str = ""
    behavior: str = ""
    item_type: str = ""
    components: list[str] = field(default_factory=list)


@dataclass
class PlayerProfile:
    account_id: int
    persona_name: str = ""
    profile_url: str = ""
    avatar_url: str = ""
    rank_tier: int = 0
    leaderboard_rank: int = 0
    estimated_mmr: int = 0


@dataclass
class RecentMatch:
    match_id: int
    hero_id: int
    hero_name: str = ""
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    duration_seconds: int = 0
    win: bool = False
    game_mode: int = 0
    start_time: int = 0
    gpm: int = 0
    xpm: int = 0
    hero_damage: int = 0
    tower_damage: int = 0
    last_hits: int = 0


@dataclass
class MatchPlayer:
    account_id: int = 0
    persona_name: str = ""
    hero_id: int = 0
    hero_name: str = ""
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    gpm: int = 0
    xpm: int = 0
    hero_damage: int = 0
    tower_damage: int = 0
    hero_healing: int = 0
    last_hits: int = 0
    denies: int = 0
    net_worth: int = 0
    level: int = 0
    items: list[str] = field(default_factory=list)
    neutral_items: list[str] = field(default_factory=list)
    backpack: list[str] = field(default_factory=list)
    aghanims_scepter: bool = False
    aghanims_shard: bool = False
    talents: list[str] = field(default_factory=list)  # 天赋的 dname（中文）
    is_radiant: bool = False
    win: bool = False


@dataclass
class MatchDetail:
    match_id: int
    duration_seconds: int = 0
    radiant_win: bool = False
    radiant_score: int = 0
    dire_score: int = 0
    game_mode: int = 0
    start_time: int = 0
    patch: str = ""
    region: str = ""
    players: list[MatchPlayer] = field(default_factory=list)
    picks_bans: list[dict] = field(default_factory=list)
    # 录像解析数据（OpenDota 解析后才有）：teamfights / objectives /
    # benchmarks / lane 等。``parsed`` 为 True 表示这场已解析出录像级数据，
    # 赛后总结据此生成；为 False 则是「未解析 / 解析中」，
    # 只有基础对战数据，不足以写赛后总结。
    parsed: bool = False
    replay: dict = field(default_factory=dict)


@dataclass
class LiveGame:
    match_id: int
    game_time: int = 0
    average_mmr: int = 0
    radiant_score: int = 0
    dire_score: int = 0
    radiant_lead: int = 0
    spectators: int = 0
    league_id: int = 0
    league_name: str = ""
    team_name_radiant: str = ""
    team_name_dire: str = ""
    players: list[dict] = field(default_factory=list)


@dataclass
class ProMatch:
    match_id: int
    duration_seconds: int = 0
    start_time: int = 0
    radiant_team_name: str = ""
    dire_team_name: str = ""
    radiant_score: int = 0
    dire_score: int = 0
    radiant_win: bool = False
    league_name: str = ""
    series_type: int = 0
