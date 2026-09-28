from __future__ import annotations

import asyncio
from typing import Any

try:
    import aiohttp

    _AIOHTTP_AVAILABLE = True
except ModuleNotFoundError:
    _AIOHTTP_AVAILABLE = False

    class _MissingAiohttp:
        class ClientError(Exception):
            pass

        class ClientTimeout:
            def __init__(self, total: int):
                self.total = total

        class ClientSession:
            def __init__(self, *_args, **_kwargs):
                raise RuntimeError("aiohttp is required to make OpenDota API requests")

    aiohttp = _MissingAiohttp()

from ..compat import logger
from .hero_names import to_chinese
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

BASE_URL = "https://api.opendota.com/api"

# 瞬时故障（超时 / 网络错误 / 5xx）的重试次数与线性退避基数
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF = 1.0


class OpenDotaRequestError(RuntimeError):
    """OpenDota 请求未能完成（超时 / 限流 / 网络故障 / HTTP 错误）。

    必须和「请求成功但没有匹配结果」区分开。早先 `_get` 用 None 表示失败，
    而 `search_players` 又把 None 和"空结果"一并变成空列表，于是**一次请求
    超时会被误报成「找不到名为 X 的玩家」** —— 用户看到的是数据不存在，
    实际只是网络抖动，而且因为拿不到数据也就出不了图。
    """


# 进程内 MatchDetail 注册表：match_id → 最近一次拿到的 MatchDetail。
# 用途：on_decorating_result 钩子里只有卡片 Markdown（match_id 从文本解析），
# 但生成录像总结需要 MatchDetail 对象（含 replay/parsed）。
# 工具链里 ``get_match_detail`` / ``wait_for_parse`` 都会写入，
# 钩子据此取回，避免对同一场比赛重复请求 OpenDota。
_MATCH_REGISTRY: dict[int, "MatchDetail"] = {}
_MATCH_REGISTRY_MAX = 32


def _register_match(detail: "MatchDetail | None") -> None:
    if detail is None:
        return
    _MATCH_REGISTRY[detail.match_id] = detail
    # 简易 LRU：超限时清掉最早的一半，避免无界增长
    if len(_MATCH_REGISTRY) > _MATCH_REGISTRY_MAX:
        for key in list(_MATCH_REGISTRY.keys())[: _MATCH_REGISTRY_MAX // 2]:
            _MATCH_REGISTRY.pop(key, None)


def get_registered_match(match_id: int) -> "MatchDetail | None":
    """取回进程内缓存的 MatchDetail；没有则返回 None。"""
    return _MATCH_REGISTRY.get(match_id)


class OpenDotaClient:
    def __init__(self, timeout: int = 15, valve_client: Any | None = None):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.valve_client = valve_client
        # 英雄表在比赛详情/近期战绩里都要用，缓存一份避免重复请求
        self._heroes_cache: list[HeroInfo] | None = None

    # --- Player ---

    async def search_players(self, query: str) -> list[PlayerProfile]:
        # 数字输入直接按 account_id 查询
        if query.isdigit():
            account_id = int(query)
            profile = await self.get_player_profile(account_id)
            if profile:
                return [profile]
            # 可能是 Steam ID 64-bit (76561198xxxxxxxxx)，转为 32-bit
            if len(query) == 17 and query.startswith("76561198"):
                account_id_32 = account_id - 76561197960265728
                profile = await self.get_player_profile(account_id_32)
                if profile:
                    return [profile]
            return []

        # 非数字走名称搜索
        data = await self._get("/search", params={"q": query}, context="玩家搜索")
        if not isinstance(data, list):
            return []
        return [
            PlayerProfile(
                account_id=item.get("account_id", 0),
                persona_name=item.get("personaname", ""),
                avatar_url=item.get("avatarfull", ""),
            )
            for item in data[:10]
        ]

    async def get_player_profile(self, account_id: int) -> PlayerProfile | None:
        data = await self._get(f"/players/{account_id}", context="玩家资料")
        if not isinstance(data, dict):
            return None
        profile = data.get("profile", {})
        return PlayerProfile(
            account_id=account_id,
            persona_name=profile.get("personaname", ""),
            profile_url=profile.get("profileurl", ""),
            avatar_url=profile.get("avatarfull", ""),
            rank_tier=data.get("rank_tier") or 0,
            leaderboard_rank=data.get("leaderboard_rank") or 0,
            estimated_mmr=data.get("mmr_estimate", {}).get("estimate", 0),
        )

    async def get_player_recent_matches(self, account_id: int, limit: int = 10) -> list[RecentMatch]:
        # 请求失败时若配了 Valve 兜底就改走 Valve，否则如实抛出，不伪装成"没有比赛"
        try:
            data = await self._get(f"/players/{account_id}/recentMatches", context="近期比赛")
        except OpenDotaRequestError:
            if self.valve_client:
                logger.info("OpenDota 请求失败，改用 Valve API...")
                return await self.valve_client.get_match_history(account_id, limit)
            raise
        if isinstance(data, list) and data:
            # OpenDota 返回数据，使用 OpenDota 数据
            # 英雄表只用于把 hero_id 换成中文名：拿不到就退回「英雄#id」，
            # 不能因为附加信息失败就让整场比赛/战绩查询失败
            heroes = await self.get_heroes(allow_failure=True)
            hero_map = {h.id: to_chinese(h.localized_name) for h in heroes}

            matches = []
            for item in data[:limit]:
                hero_id = item.get("hero_id", 0)
                matches.append(RecentMatch(
                    match_id=item.get("match_id", 0),
                    hero_id=hero_id,
                    hero_name=hero_map.get(hero_id) or f"英雄#{hero_id}",
                    kills=item.get("kills", 0),
                    deaths=item.get("deaths", 0),
                    assists=item.get("assists", 0),
                    duration_seconds=item.get("duration", 0),
                    win=_is_radiant_win(item),
                    game_mode=item.get("game_mode", 0),
                    start_time=item.get("start_time", 0),
                    gpm=item.get("gold_per_min", 0),
                    xpm=item.get("xp_per_min", 0),
                    hero_damage=item.get("hero_damage", 0),
                    tower_damage=item.get("tower_damage", 0),
                    last_hits=item.get("last_hits", 0),
                ))
            return matches

        # OpenDota 返回空数据，尝试 Valve API
        if self.valve_client:
            logger.info("OpenDota 返回空数据，尝试 Valve API...")
            return await self.valve_client.get_match_history(account_id, limit)

        return []

    # --- Heroes ---

    async def get_heroes(self, *, allow_failure: bool = False) -> list[HeroInfo]:
        """获取英雄表。结果会缓存，避免比赛详情/近期战绩重复请求。

        ``allow_failure=True`` 用于「英雄表只是附加信息」的场景（如比赛详情里
        把 hero_id 换成中文名）：此时请求失败返回空表，让主流程用
        ``英雄#id`` 兜底，而不是让整场查询失败。
        """
        if self._heroes_cache is not None:
            return self._heroes_cache
        try:
            data = await self._get("/heroStats", context="英雄数据")
        except OpenDotaRequestError:
            if allow_failure:
                logger.warning("英雄数据获取失败，本次跳过英雄名翻译。")
                return []
            raise
        if not isinstance(data, list):
            return []
        heroes = [
            HeroInfo(
                id=h.get("id", 0),
                name=h.get("name", ""),
                localized_name=to_chinese(
                    h.get("localized_name") or h.get("name", "")
                ),
                primary_attr=h.get("primary_attr", ""),
                attack_type=h.get("attack_type", ""),
                roles=h.get("roles", []),
                base_health=h.get("base_health", 0),
                base_mana=h.get("base_mana", 0),
                base_armor=h.get("base_armor", 0),
                base_attack_min=h.get("base_attack_min", 0),
                base_attack_max=h.get("base_attack_max", 0),
                base_str=h.get("base_str", 0),
                base_agi=h.get("base_agi", 0),
                base_int=h.get("base_int", 0),
                str_gain=h.get("str_gain", 0),
                agi_gain=h.get("agi_gain", 0),
                int_gain=h.get("int_gain", 0),
                move_speed=h.get("move_speed", 0),
                pro_pick=h.get("pro_pick", 0),
                pro_win=h.get("pro_win", 0),
                pub_pick=h.get("pub_pick", 0),
                pub_win=h.get("pub_win", 0),
            )
            for h in data
        ]
        if heroes:
            self._heroes_cache = heroes
        return heroes

    # --- Items ---

    async def get_items(self) -> dict[str, ItemInfo]:
        data = await self._get("/constants/items", context="物品数据")
        if not isinstance(data, dict):
            return {}
        items = {}
        for key, val in data.items():
            items[key] = ItemInfo(
                name=key,
                display_name=val.get("dname", key),
                cost=val.get("cost", 0),
                description=_extract_item_description(val),
                behavior=str(val.get("behavior", "")),
                item_type=val.get("qual", ""),
                components=[c for c in (val.get("components") or []) if c],
            )
        return items

    # --- Match ---

    async def get_match_detail(self, match_id: int) -> MatchDetail | None:
        # 请求失败时若配了 Valve 兜底就改走 Valve，否则如实抛出。
        # 返回 None 只表示「请求成功但 OpenDota 没有这场数据」（未解析/不存在）。
        try:
            data = await self._get(f"/matches/{match_id}", context="比赛详情")
        except OpenDotaRequestError:
            if self.valve_client:
                logger.info("OpenDota 请求失败，改用 Valve API...")
                return await self.valve_client.get_match_detail(match_id)
            raise
        if isinstance(data, dict):
            # OpenDota 返回数据，使用 OpenDota 数据
            # 英雄表只用于把 hero_id 换成中文名：拿不到就退回「英雄#id」，
            # 不能因为附加信息失败就让整场比赛/战绩查询失败
            heroes = await self.get_heroes(allow_failure=True)
            hero_map = {h.id: to_chinese(h.localized_name) for h in heroes}
            players = []
            for p in data.get("players", []):
                hero_id = p.get("hero_id", 0)
                items = []
                for i in range(6):
                    item_key = f"item_{i}"
                    item_val = p.get(item_key)
                    if item_val:
                        items.append(str(item_val))

                # 中立物品单独放在 item_neutral / item_neutral2，不属于 6 个装备栏
                neutral_items = [
                    str(p[key]) for key in ("item_neutral", "item_neutral2") if p.get(key)
                ]
                # 背包三格：不参与主战出装，但展示时一并列出便于复盘
                backpack = [
                    str(p[key]) for key in ("backpack_0", "backpack_1", "backpack_2")
                    if p.get(key)
                ]

                players.append(MatchPlayer(
                    # 匿名玩家（未公开资料）没有 account_id。这里不能退回
                    # player_slot：那是 0~132 的栏位号，会被当成真实账号 ID
                    # 展示给用户。缺失就留空，由展示层决定如何兜底。
                    account_id=p.get("account_id") or 0,
                    persona_name=p.get("personaname") or "",
                    hero_id=hero_id,
                    hero_name=hero_map.get(hero_id) or f"英雄#{hero_id}",
                    kills=p.get("kills", 0),
                    deaths=p.get("deaths", 0),
                    assists=p.get("assists", 0),
                    gpm=p.get("gold_per_min", 0),
                    xpm=p.get("xp_per_min", 0),
                    hero_damage=p.get("hero_damage", 0),
                    tower_damage=p.get("tower_damage", 0),
                    hero_healing=p.get("hero_healing", 0),
                    last_hits=p.get("last_hits", 0),
                    denies=p.get("denies", 0),
                    net_worth=p.get("net_worth", 0),
                    level=p.get("level", 0),
                    items=items,
                    neutral_items=neutral_items,
                    backpack=backpack,
                    aghanims_scepter=bool(p.get("aghanims_scepter")),
                    aghanims_shard=bool(p.get("aghanims_shard")),
                    is_radiant=p.get("isRadiant", False),
                    win=p.get("win", False),
                ))

            detail = MatchDetail(
                match_id=match_id,
                duration_seconds=data.get("duration", 0),
                radiant_win=data.get("radiant_win", False),
                radiant_score=data.get("radiant_score", 0),
                dire_score=data.get("dire_score", 0),
                game_mode=data.get("game_mode", 0),
                start_time=data.get("start_time", 0),
                patch=data.get("patch", ""),
                region=str(data.get("region", "")),
                players=players,
                picks_bans=data.get("picks_bans") or [],
                parsed=self.is_parsed(data),
                replay=self.extract_replay_data(data),
            )
            _register_match(detail)
            return detail

        # OpenDota 返回空数据，尝试 Valve API
        if self.valve_client:
            logger.info("OpenDota 返回空数据，尝试 Valve API...")
            return await self.valve_client.get_match_detail(match_id)

        return None

    # --- 录像解析 ---

    @staticmethod
    def is_parsed(data: dict | None) -> bool:
        """这场比赛是否已解析出录像级数据。

        OpenDota 解析完成后 match dict 会带 ``teamfights`` / ``objectives``
        等字段；未解析或解析中的比赛这两个字段为空。这是「能否写赛后总结」
        的判据：基础对战数据（KDA/经济）任何时候都有，但团战节奏、
        关键目标、对线优劣只有解析后才有。
        """
        if not isinstance(data, dict):
            return False
        return bool(data.get("teamfights") or data.get("objectives"))

    @staticmethod
    def extract_replay_data(data: dict | None) -> dict:
        """从 match dict 抽出赛后总结要用的录像分析数据。

        只取「能讲出这场比赛故事」的字段，原始响应太大、多数字段（如逐秒
        事件流）对生成总结没用。玩家级数据在 ``render_match_detail`` 已用
        的 KDA/经济之外，补 benchmarks（百分位表现）与 lane（对线结果）。
        """
        if not isinstance(data, dict):
            return {}
        players = []
        for p in data.get("players", []) or []:
            players.append({
                "hero_id": p.get("hero_id", 0),
                "is_radiant": bool(p.get("isRadiant", False)),
                "kills": p.get("kills", 0),
                "deaths": p.get("deaths", 0),
                "assists": p.get("assists", 0),
                "lane": p.get("lane"),
                "lane_role": p.get("lane_role"),
                "is_roaming": bool(p.get("is_roaming", False)),
                "lane_efficiency_pct": p.get("lane_efficiency_pct"),
                "benchmarks": p.get("benchmarks") or {},
                # 视野控制：插眼 / 排眼（复盘「视野」维度用）
                "obs_placed": p.get("obs_placed", 0),
                "sen_placed": p.get("sen_placed", 0),
                "observer_kills": p.get("observer_kills", 0),
                "sentry_kills": p.get("sentry_kills", 0),
                # 购物时间线：复盘「关键物品成型时点」用（含 time/key）
                "purchase_log": p.get("purchase_log") or [],
            })
        return {
            "duration": data.get("duration", 0),
            "first_blood_time": data.get("first_blood_time"),
            "radiant_gold_adv": data.get("radiant_gold_adv") or [],
            "radiant_xp_adv": data.get("radiant_xp_adv") or [],
            "teamfights": data.get("teamfights") or [],
            "objectives": data.get("objectives") or [],
            "picks_bans": data.get("picks_bans") or [],
            "players": players,
        }

    async def request_parse(self, match_id: int) -> bool:
        """向 OpenDota 提交录像解析任务；成功受理返回 True。

        重复提交同一场是安全的（OpenDota 会去重）。这里只负责"登记"，
        真正的解析在 OpenDota 服务器后台跑，约需数分钟到十分钟。
        """
        if not _AIOHTTP_AVAILABLE:
            return False
        url = f"{BASE_URL}/request/{match_id}"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url) as response:
                    # 200 = 受理；429 = 限流；5xx = 服务端忙。受理与否都不抛，
                    # 由轮询阶段按"是否已解析"兜底判断。
                    logger.info(f"OpenDota 解析请求 #{match_id} HTTP {response.status}。")
                    return response.status == 200
        except Exception as exc:
            logger.warning(f"OpenDota 解析请求 #{match_id} 失败: {exc}")
            return False

    async def wait_for_parse(self, match_id: int, timeout: float = 600.0,
                             interval: float = 20.0) -> MatchDetail | None:
        """轮询等待录像解析完成，返回带录像数据的 MatchDetail；超时返回 None。

        OpenDota 解析时长不定（官方说数分钟，实测常见 5~10 分钟），
        不能同步阻塞用户请求，因此设计为后台任务调用：
        先提交解析，再以 ``interval`` 间隔重新拉取 match，
        直到 ``is_parsed`` 为真或总时长超过 ``timeout``。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        # 已解析就直接返回，不必再等
        try:
            existing = await self.get_match_detail(match_id)
        except OpenDotaRequestError:
            existing = None
        if existing and existing.parsed:
            return existing

        await self.request_parse(match_id)
        attempt = 0
        while asyncio.get_event_loop().time() < deadline:
            attempt += 1
            await asyncio.sleep(interval)
            try:
                detail = await self.get_match_detail(match_id)
            except OpenDotaRequestError as exc:
                logger.info(f"等待解析 #{match_id} 第 {attempt} 次拉取失败: {exc}")
                continue
            if detail and detail.parsed:
                logger.info(f"录像 #{match_id} 解析完成（第 {attempt} 次轮询）。")
                return detail
        logger.warning(f"录像 #{match_id} 解析超时（{timeout}s），放弃生成赛后总结。")
        return None

    # --- Live ---

    async def get_live_games(self) -> list[LiveGame]:
        data = await self._get("/live", context="实时比赛")
        if not isinstance(data, list):
            return []
        return [
            LiveGame(
                match_id=g.get("match_id", 0),
                game_time=g.get("game_time", 0),
                average_mmr=g.get("average_mmr", 0),
                radiant_score=g.get("radiant_score", 0),
                dire_score=g.get("dire_score", 0),
                radiant_lead=g.get("radiant_lead", 0),
                spectators=g.get("spectators", 0),
                league_id=g.get("league_id", 0),
                team_name_radiant=g.get("team_name_radiant", ""),
                team_name_dire=g.get("team_name_dire", ""),
                players=g.get("players") or [],
            )
            for g in data[:20]
        ]

    # --- Pro ---

    async def get_pro_matches(self, limit: int = 10) -> list[ProMatch]:
        data = await self._get("/proMatches", context="职业比赛")
        if not isinstance(data, list):
            return []
        return [
            ProMatch(
                match_id=m.get("match_id", 0),
                duration_seconds=m.get("duration", 0),
                start_time=m.get("start_time", 0),
                radiant_team_name=m.get("radiant_name", ""),
                dire_team_name=m.get("dire_name", ""),
                radiant_score=m.get("radiant_score", 0),
                dire_score=m.get("dire_score", 0),
                radiant_win=m.get("radiant_win", False),
                league_name=m.get("league_name", ""),
                series_type=m.get("series_type", 0),
            )
            for m in data[:limit]
        ]

    # --- Internal ---

    async def _get(self, path: str, params: dict[str, Any] | None = None, context: str = "OpenDota") -> Any | None:
        """请求 OpenDota；成功返回解析后的 JSON，请求失败抛 OpenDotaRequestError。

        返回 None 仅表示「请求成功但响应不是预期结构」，绝不用于表示请求失败 ——
        否则调用方无法区分「没有这个玩家」和「这 15 秒网络不通」。
        """
        if not _AIOHTTP_AVAILABLE:
            raise OpenDotaRequestError(f"{context} 请求跳过：当前环境未安装 aiohttp。")

        url = f"{BASE_URL}{path}"
        last_error = ""
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.get(url, params=params) as response:
                        if response.status == 429:
                            last_error = "请求触发频率限制"
                            logger.warning(f"{context} {last_error}（第 {attempt} 次）。")
                        elif response.status >= 500:
                            last_error = f"服务端错误 HTTP {response.status}"
                            logger.warning(f"{context} {last_error}（第 {attempt} 次）。")
                        elif response.status == 404:
                            # 404 是资源不存在（如未解析的比赛、不存在的账号），
                            # 属于数据层面的"没有"，不是请求失败，交给调用方按空结果处理
                            logger.info(f"{context} 资源不存在（HTTP 404）。")
                            return None
                        elif response.status >= 400:
                            # 其余 4xx 是确定性错误，重试没有意义
                            raise OpenDotaRequestError(
                                f"{context} 请求失败，HTTP {response.status}。"
                            )
                        else:
                            return await response.json(content_type=None)
            except OpenDotaRequestError:
                raise
            except (TimeoutError, asyncio.TimeoutError):
                last_error = "请求超时"
                logger.warning(f"{context} {last_error}（第 {attempt} 次）。")
            except aiohttp.ClientError:
                last_error = "网络请求失败"
                logger.warning(f"{context} {last_error}（第 {attempt} 次）。")
            except ValueError:
                last_error = "JSON 解析失败"
                logger.warning(f"{context} {last_error}（第 {attempt} 次）。")

            if attempt < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF * attempt)

        raise OpenDotaRequestError(f"{context} {last_error}（已重试 {_RETRY_ATTEMPTS} 次）。")


def _is_radiant_win(match_data: dict) -> bool:
    """判断玩家是否胜利。recentMatches 中通过 player_slot + radiant_win 推断。"""
    radiant_win = match_data.get("radiant_win", False)
    player_slot = match_data.get("player_slot", 0)
    is_radiant = player_slot < 128
    return radiant_win == is_radiant


def _extract_item_description(item_data: dict) -> str:
    """提取物品描述（abilities 中的 description）。"""
    abilities = item_data.get("abilities") or []
    parts = []
    for ab in abilities:
        desc = ab.get("description", "")
        if desc:
            parts.append(desc)
    hint = item_data.get("hint") or []
    for h in hint:
        if h:
            parts.append(h)
    return " ".join(parts)[:300]
