from __future__ import annotations

from ..compat import AstrAgentContext, ContextWrapper, Field, FunctionTool, ToolExecResult, dataclass

from .delivery import deliver_result
from .triggers import SCOPE_RULE, WAKE_RULE


@dataclass
class DotaPlayerTool(FunctionTool[AstrAgentContext]):
    name: str = "dota_player_query"
    description: str = (
        WAKE_RULE + SCOPE_RULE +
        "查询刀塔玩家资料、段位、MMR 和近期战绩。"
        "当用户想了解某个刀塔玩家时调用，例如「查一下 miracle 的刀塔战绩」「查他最近一场刀塔比赛」。"
        "返回数据后，请根据以下要点为用户生成简洁分析：\n"
        "1. 根据胜率和 KDA 评价玩家近期状态（上升/稳定/下滑）\n"
        "2. 指出表现突出或需改进的方面\n"
        "3. 如果近期常用英雄集中，点评英雄池特点"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "player_name": {
                    "type": "string",
                    "description": "玩家名称或 Steam ID，例如 Miracle、yarizm、899428504",
                },
                "text_only": {
                    "type": "boolean",
                    "description": "仅当用户明确要求「纯文字」「不要图片」或输入以 --text 结尾时设为 true",
                },
            },
            "required": ["player_name"],
        }
    )
    client: object = Field(default=None, exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        from ..core.templates import fmt_avg_kda, fmt_rank, render_player_profile

        player_name = str(kwargs.get("player_name") or "").strip()
        if not player_name:
            return "请提供玩家名称或 Steam ID。"

        try:
            # Search for player
            profiles = await self.client.search_players(player_name)
            if not profiles:
                return f"找不到名为 '{player_name}' 的 Dota2 玩家。"

            # Use first match
            profile = profiles[0]

            # Get full profile
            full_profile = await self.client.get_player_profile(profile.account_id)
            if full_profile:
                profile = full_profile

            # Get recent matches
            recent = await self.client.get_player_recent_matches(profile.account_id, limit=10)

            rank_str = fmt_rank(profile.rank_tier, profile.leaderboard_rank)
            avg_kda = fmt_avg_kda(recent)
            markdown = render_player_profile(profile, recent, rank_str, avg_kda)
        except Exception as exc:
            return f"查询玩家失败：{exc}"

        return await deliver_result(context, markdown, text_only=bool(kwargs.get("text_only")))
