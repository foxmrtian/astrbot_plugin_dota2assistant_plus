from __future__ import annotations

from ..compat import AstrAgentContext, ContextWrapper, Field, FunctionTool, ToolExecResult, dataclass

from .delivery import deliver_result
from .triggers import SCOPE_RULE, WAKE_RULE


@dataclass
class DotaLiveTool(FunctionTool[AstrAgentContext]):
    name: str = "dota_live_games"
    description: str = (
        WAKE_RULE + SCOPE_RULE +
        "查看当前正在进行的 Dota2（刀塔2）对局，包括比分、进行时间、观战人数。"
        "当用户想看正在进行的刀塔对局、当前有哪些比赛在打时调用。"
        "返回数据后，请根据以下要点为用户生成简洁分析：\n"
        "1. 如果有高 MMR 或职业选手的比赛，重点提及\n"
        "2. 指出正在进行的精彩对决"
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

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        from ..core.templates import render_live_games

        try:
            games = await self.client.get_live_games()
            markdown = render_live_games(games)
        except Exception as exc:
            return f"查询实时比赛失败：{exc}"

        return await deliver_result(context, markdown, text_only=bool(kwargs.get("text_only")))
