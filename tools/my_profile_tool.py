from __future__ import annotations

from ..compat import AstrAgentContext, ContextWrapper, Field, FunctionTool, ToolExecResult, dataclass

from .delivery import deliver_result
from .triggers import SCOPE_RULE, WAKE_RULE


@dataclass
class DotaMyProfileTool(FunctionTool[AstrAgentContext]):
    name: str = "dota_my_profile"
    description: str = (
        WAKE_RULE + SCOPE_RULE +
        "查询当前用户自己绑定的刀塔账号资料、段位和近期战绩。"
        "当用户说「我的刀塔战绩」「我的刀塔天梯分」「查询我的最近一场刀塔比赛」时调用。"
        "注意：此工具仅在用户已绑定 Steam ID 时有效。"
        "返回数据后，请根据以下要点为用户生成简洁分析：\n"
        "1. 根据胜率和 KDA 评价玩家近期状态（上升/稳定/下滑）\n"
        "2. 指出表现突出或需改进的方面\n"
        "3. 如果近期常用英雄集中，点评英雄池特点"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "text_only": {
                    "type": "boolean",
                    "description": "仅当用户明确要求「纯文字」「不要图片」或输入以 --text 结尾时设为 true",
                },
            },
            "required": [],
        }
    )
    client: object = Field(default=None, exclude=True)
    store: object = Field(default=None, exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        from ..core.templates import fmt_avg_kda, fmt_rank, render_player_profile

        event = context.context.event
        sender_id = event.get_sender_id()

        if not sender_id:
            return "无法获取用户身份信息。"

        try:
            account_id = self.store.get_bound_account(sender_id)
            if not account_id:
                return "你还没有绑定 Steam ID。请使用 /dota bind <Steam ID> 命令绑定。"

            profile = await self.client.get_player_profile(account_id)
            if not profile:
                return f"获取绑定账号 #{account_id} 的资料失败，请检查绑定的 ID 是否正确。"

            recent = await self.client.get_player_recent_matches(account_id, limit=10)

            rank_str = fmt_rank(profile.rank_tier, profile.leaderboard_rank)
            avg_kda = fmt_avg_kda(recent)
            markdown = render_player_profile(profile, recent, rank_str, avg_kda)
        except Exception as exc:
            return f"查询失败：{exc}"

        return await deliver_result(context, markdown, text_only=bool(kwargs.get("text_only")))
