from __future__ import annotations

from ..compat import AstrAgentContext, ContextWrapper, Field, FunctionTool, ToolExecResult, dataclass

from .delivery import deliver_result
from .triggers import SCOPE_RULE, WAKE_RULE


@dataclass
class DotaProTool(FunctionTool[AstrAgentContext]):
    name: str = "dota_pro_matches"
    description: str = (
        WAKE_RULE + SCOPE_RULE +
        "查看近期 Dota2（刀塔2）职业比赛结果，包括战队、比分、赛事名称。"
        "当用户想了解刀塔职业比赛、赛事结果、战队表现时调用。"
        "返回数据后，请根据以下要点为用户生成简洁分析：\n"
        "1. 指出重要的比赛结果（决赛、爆冷等）\n"
        "2. 如果用户关注特定战队，重点点评该战队表现"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回比赛数量，默认 10",
                },
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
        from ..core.templates import render_pro_matches

        limit = kwargs.get("limit", 10)
        try:
            limit = int(limit) if limit else 10
        except (TypeError, ValueError):
            limit = 10

        try:
            matches = await self.client.get_pro_matches(limit=limit)
            markdown = render_pro_matches(matches)
        except Exception as exc:
            return f"查询职业比赛失败：{exc}"

        return await deliver_result(context, markdown, text_only=bool(kwargs.get("text_only")))
