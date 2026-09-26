from __future__ import annotations

from ..compat import AstrAgentContext, ContextWrapper, Field, FunctionTool, ToolExecResult, dataclass

from .delivery import deliver_result
from .triggers import SCOPE_RULE, WAKE_RULE


@dataclass
class DotaItemTool(FunctionTool[AstrAgentContext]):
    name: str = "dota_item_query"
    description: str = (
        WAKE_RULE + SCOPE_RULE +
        "查询刀塔物品价格、效果、合成配方。"
        "当用户想了解某个刀塔物品时调用，例如「跳刀多少钱」「黑皇杖效果」。"
        "返回数据后，请根据以下要点为用户生成简洁分析：\n"
        "1. 简要说明该物品的核心作用\n"
        "2. 推荐适合什么类型的英雄/什么局势出\n"
        "3. 如果是合成件，说明合成路线的性价比"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "物品名称，中文或英文均可，例如：Blink Dagger、跳刀、黑皇杖",
                },
                "text_only": {
                    "type": "boolean",
                    "description": "仅当用户明确要求「纯文字」「不要图片」或输入以 --text 结尾时设为 true",
                },
            },
            "required": ["item_name"],
        }
    )
    client: object = Field(default=None, exclude=True)
    item_name_map: object = Field(default=None, exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        from ..core.templates import render_item_info

        item_name = str(kwargs.get("item_name") or "").strip().lower()
        if not item_name:
            return "请提供物品名称。"

        try:
            items = await self.client.get_items()
            if not items:
                return "获取物品数据失败。"

            # Search by display name or internal name
            item = None
            for key, val in items.items():
                if (item_name == val.display_name.lower()
                        or item_name == key.lower()
                        or item_name in val.display_name.lower()):
                    item = val
                    break

            # Try Chinese name mapping
            if not item and self.item_name_map:
                internal = self.item_name_map.get(item_name)
                if internal and internal in items:
                    item = items[internal]

            if not item:
                # Fuzzy match
                matches = [
                    val for val in items.values()
                    if item_name in val.display_name.lower() or item_name in val.name.lower()
                ]
                if len(matches) == 1:
                    item = matches[0]
                elif len(matches) > 1:
                    names = ", ".join(m.display_name for m in matches[:5])
                    return f"找到多个匹配的物品：{names}。请提供更精确的名称。"

            if not item:
                return f"找不到名为 '{item_name}' 的物品。"

            markdown = render_item_info(item)
        except Exception as exc:
            return f"查询物品失败：{exc}"

        return await deliver_result(context, markdown, text_only=bool(kwargs.get("text_only")))
