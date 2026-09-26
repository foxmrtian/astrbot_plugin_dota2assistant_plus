"""Valve 官方 Dota2 API 客户端，作为 OpenDota 的备选数据源。"""

from __future__ import annotations

import asyncio
from typing import Any

try:
    import aiohttp

    _AIOHTTP_AVAILABLE = True
except ModuleNotFoundError:
    _AIOHTTP_AVAILABLE = False

from ..compat import logger
from .hero_names import to_chinese
from .models import MatchDetail, MatchPlayer, PlayerProfile, RecentMatch

BASE_URL = "https://api.steampowered.com"


class ValveClient:
    """Valve 官方 Dota2 API 客户端。"""

    def __init__(self, api_key: str, timeout: int = 15):
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    # ──────────────────────────────────────────────
    # 玩家资料
    # ──────────────────────────────────────────────

    async def get_player_summary(self, account_id: int) -> PlayerProfile | None:
        """获取玩家资料（通过 Steam Web API）。

        Args:
            account_id: 32-bit 账号 ID

        Returns:
            PlayerProfile 或 None
        """
        # 转换为 64-bit Steam ID
        steam_id_64 = account_id + 76561197960265728

        data = await self._get(
            "/ISteamUser/GetPlayerSummaries/v2",
            params={"steamids": str(steam_id_64)},
            context="Valve 玩家资料",
        )

        if not data:
            return None

        players = data.get("response", {}).get("players", [])
        if not players:
            return None

        player = players[0]
        return PlayerProfile(
            account_id=account_id,
            persona_name=player.get("personaname", ""),
            profile_url=player.get("profileurl", ""),
            avatar_url=player.get("avatarfull", ""),
        )

    # ──────────────────────────────────────────────
    # 比赛历史
    # ──────────────────────────────────────────────

    async def get_match_history(self, account_id: int, limit: int = 20) -> list[RecentMatch]:
        """获取玩家比赛历史。

        Args:
            account_id: 32-bit 账号 ID
            limit: 返回比赛数量

        Returns:
            RecentMatch 列表
        """
        data = await self._get(
            "/IDOTA2Match_570/GetMatchHistory/v1",
            params={
                "account_id": str(account_id),
                "matches_requested": str(limit),
            },
            context="Valve 比赛历史",
        )

        if not data:
            return []

        result = data.get("result", {})
        status = result.get("status", 0)

        if status != 1:
            status_detail = result.get("statusDetail", "未知错误")
            logger.warning(f"Valve 比赛历史请求失败: {status_detail}")
            return []

        matches = result.get("matches", [])

        # 获取英雄映射
        hero_map = await self._get_hero_map()

        recent_matches = []
        for match in matches:
            # 找到当前玩家的 hero_id
            hero_id = 0
            for player in match.get("players", []):
                if player.get("account_id") == account_id:
                    hero_id = player.get("hero_id", 0)
                    break

            recent_matches.append(RecentMatch(
                match_id=match.get("match_id", 0),
                hero_id=hero_id,
                hero_name=hero_map.get(hero_id) or to_chinese(f"Hero#{hero_id}"),
                duration_seconds=0,  # GetMatchHistory 不返回时长
                start_time=match.get("start_time", 0),
                game_mode=match.get("game_mode", 0),
            ))

        return recent_matches

    # ──────────────────────────────────────────────
    # 比赛详情
    # ──────────────────────────────────────────────

    async def get_match_detail(self, match_id: int) -> MatchDetail | None:
        """获取比赛详情。

        Args:
            match_id: 比赛 ID

        Returns:
            MatchDetail 或 None
        """
        data = await self._get(
            "/IDOTA2Match_570/GetMatchDetails/v1",
            params={"match_id": str(match_id)},
            context="Valve 比赛详情",
        )

        if not data:
            return None

        result = data.get("result", {})

        # 获取英雄映射
        hero_map = await self._get_hero_map()

        # 解析玩家数据
        players = []
        for p in result.get("players", []):
            hero_id = p.get("hero_id", 0)
            player_slot = p.get("player_slot", 0)
            is_radiant = player_slot < 128

            items = []
            for i in range(6):
                item_key = f"item_{i}"
                item_val = p.get(item_key)
                if item_val:
                    items.append(str(item_val))

            neutral_items = [
                str(p[key]) for key in ("item_neutral", "item_neutral2") if p.get(key)
            ]

            players.append(MatchPlayer(
                account_id=p.get("account_id", 0),
                persona_name=p.get("personaname") or "",
                hero_id=hero_id,
                hero_name=hero_map.get(hero_id) or to_chinese(f"Hero#{hero_id}"),
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
                level=p.get("level", 0),
                items=items,
                neutral_items=neutral_items,
                is_radiant=is_radiant,
                win=(result.get("radiant_win", False) == is_radiant),
            ))

        return MatchDetail(
            match_id=match_id,
            duration_seconds=result.get("duration", 0),
            radiant_win=result.get("radiant_win", False),
            radiant_score=result.get("radiant_score", 0),
            dire_score=result.get("dire_score", 0),
            game_mode=result.get("game_mode", 0),
            start_time=result.get("start_time", 0),
            players=players,
            picks_bans=result.get("picks_bans") or [],
        )

    # ──────────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────────

    async def _get_hero_map(self) -> dict[int, str]:
        """获取英雄 ID 到官方中文名的映射。

        之前恒返回空映射，导致调用方只能拿到 ``Hero#<id>`` 占位符。
        这里直接读本地英雄名表（``assets/hero_names.json``），
        无需额外网络请求。
        """
        try:
            from .hero_names import _load

            return {
                int(info["id"]): info["zh"]
                for info in _load().values()
                if isinstance(info, dict) and info.get("id") and info.get("zh")
            }
        except Exception as exc:  # pragma: no cover
            logger.warning(f"加载英雄 ID 映射失败: {exc}")
            return {}

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        context: str = "Valve API",
    ) -> Any | None:
        """发送 GET 请求。

        Args:
            path: API 路径
            params: 请求参数
            context: 日志上下文

        Returns:
            响应数据或 None
        """
        if not _AIOHTTP_AVAILABLE:
            logger.warning(f"{context} 请求跳过：当前环境未安装 aiohttp。")
            return None

        if not self.api_key:
            logger.warning(f"{context} 请求跳过：未配置 Steam API Key。")
            return None

        # 添加 API Key
        request_params = {"key": self.api_key}
        if params:
            request_params.update(params)

        url = f"{BASE_URL}{path}"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, params=request_params) as response:
                    if response.status == 403:
                        logger.warning(f"{context} 请求失败：API Key 无效或权限不足。")
                        return None
                    if response.status == 429:
                        logger.warning(f"{context} 请求触发频率限制。")
                        return None
                    if response.status >= 400:
                        logger.warning(f"{context} 请求失败，HTTP {response.status}。")
                        return None
                    return await response.json(content_type=None)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(f"{context} 请求超时。")
        except aiohttp.ClientError:
            logger.warning(f"{context} 网络请求失败。")
        except ValueError:
            logger.warning(f"{context} JSON 解析失败。")
        return None
