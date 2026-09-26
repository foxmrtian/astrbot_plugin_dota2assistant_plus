from __future__ import annotations

from ..compat import AstrAgentContext, ContextWrapper, Field, FunctionTool, ToolExecResult, dataclass

from .delivery import FOCUS_HERO_EXTRA_KEY, deliver_result
from .triggers import SCOPE_RULE, SPORTS_MISJUDGEMENT_RULE, WAKE_RULE


@dataclass
class DotaMatchTool(FunctionTool[AstrAgentContext]):
    name: str = "dota_match_detail"
    description: str = (
        WAKE_RULE + SCOPE_RULE + SPORTS_MISJUDGEMENT_RULE +
        "查询 Dota2（刀塔2）单场对局详情，包括双方阵容、KDA、经济、伤害等数据。"
        "用户给出比赛编号并要求「分析比赛 / 查询比赛 / 看看这局 / 复盘」时调用；"
        "裸编号（如「丹丹 8987081176」）也算，必须调用。\n"
        "赛后总结由插件自动生成（天辉方的表现、夜魇方的表现、一句话总结三个小栏目，"
        "并逐项给出经济、经验、KDA、参战率）。"
        "若用户点名关心某个英雄（如「我朋友用的火猫怎么样」），"
        "必须把该英雄名传给 focus_hero 参数，插件会额外单独点评它。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "match_id": {
                    "type": "integer",
                    "description": (
                        "Dota2 对局编号（8~10 位纯数字），例如 8987081176、8831125663。"
                        "用户消息里的那串数字即为该参数。"
                    ),
                },
                "focus_hero": {
                    "type": "string",
                    "description": (
                        "仅当用户点名关注某个英雄时填该英雄名（中文/英文/俗称均可，"
                        "如「火猫」「Ember Spirit」）。例如「8831125663 我朋友用的火猫怎么样」"
                        "应传 focus_hero=\"火猫\"。用户没点名任何英雄时不要填。"
                    ),
                },
                "text_only": {
                    "type": "boolean",
                    "description": "仅当用户明确要求「纯文字」「不要图片」或输入以 --text 结尾时设为 true",
                },
            },
            "required": ["match_id"],
        }
    )
    client: object = Field(default=None, exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        from ..core.templates import render_match_detail

        match_id = kwargs.get("match_id")
        if not match_id:
            return "请提供比赛 ID。"

        try:
            match_id = int(match_id)
        except (TypeError, ValueError):
            return "比赛 ID 必须是数字。"

        try:
            match = await self.client.get_match_detail(match_id)
            if not match:
                return f"获取比赛 #{match_id} 的数据失败，比赛可能不存在或数据尚未解析。"

            markdown = render_match_detail(match)
        except Exception as exc:
            return f"查询比赛失败：{exc}"

        # 把关注英雄挂到 event 上：赛后总结由 on_decorating_result 钩子调用
        # summarize() 生成，那次调用拿不到工具参数，只能靠事件传递。
        focus = str(kwargs.get("focus_hero") or "").strip()
        if focus:
            event = getattr(getattr(context, "context", None), "event", None)
            if event is not None:
                try:
                    event.set_extra(FOCUS_HERO_EXTRA_KEY, focus)
                except Exception:
                    pass

        return await deliver_result(context, markdown, text_only=bool(kwargs.get("text_only")))
