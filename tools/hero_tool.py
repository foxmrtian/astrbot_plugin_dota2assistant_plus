from __future__ import annotations

from ..compat import AstrAgentContext, ContextWrapper, Field, FunctionTool, ToolExecResult, dataclass

from .delivery import deliver_result
from .triggers import SCOPE_RULE, WAKE_RULE


@dataclass
class DotaHeroTool(FunctionTool[AstrAgentContext]):
    name: str = "dota_hero_query"
    description: str = (
        WAKE_RULE + SCOPE_RULE +
        "查询刀塔英雄属性、技能数据、天梯胜率和职业选取率。"
        "当用户想了解某个刀塔英雄时调用，例如「敌法师属性」「水人胜率」「火猫定位」。"
        "返回数据后，请根据以下要点为用户生成简洁分析：\n"
        "1. 根据属性成长分析该英雄的核心定位（前期/中期/后期强势）\n"
        "2. 结合天梯胜率和职业数据点评当前版本强度\n"
        "3. 推荐适合的打法思路（1-2 句话）"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "hero_name": {
                    "type": "string",
                    "description": "英雄名称，中文或英文均可，例如：反恐怖利刃、Anti-Mage、火猫",
                },
                "text_only": {
                    "type": "boolean",
                    "description": "仅当用户明确要求「纯文字」「不要图片」或输入以 --text 结尾时设为 true",
                },
            },
            "required": ["hero_name"],
        }
    )
    client: object = Field(default=None, exclude=True)
    hero_map: object = Field(default=None, exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        from ..core.hero_names import match_hero, match_heroes_fuzzy
        from ..core.templates import render_hero_info

        query = str(kwargs.get("hero_name") or "").strip()
        if not query:
            return "请提供英雄名称。"
        hero_name = query.lower()

        try:
            heroes = await self.client.get_heroes()
            if not heroes:
                return "获取英雄数据失败。"

            # 中文官方名 / 内部名 / 英文名（会自动翻译）匹配
            hero = match_hero(heroes, query)

            # 中文俗称（火猫、水人…）通过 hero_map 落到内部名
            if not hero and self.hero_map:
                internal = self.hero_map.get(hero_name)
                if internal:
                    hero = next((h for h in heroes if h.name == internal), None)

            if not hero:
                matches = match_heroes_fuzzy(heroes, query)
                if len(matches) > 1:
                    names = ", ".join(h.localized_name for h in matches[:5])
                    return f"找到多个匹配的英雄：{names}。请提供更精确的名称。"

            if not hero:
                return f"找不到名为 '{query}' 的英雄。"

            markdown = render_hero_info(hero)
        except Exception as exc:
            return f"查询英雄失败：{exc}"

        return await deliver_result(context, markdown, text_only=bool(kwargs.get("text_only")))


@dataclass
class DotaHeroListTool(FunctionTool[AstrAgentContext]):
    name: str = "dota_hero_list"
    description: str = (
        WAKE_RULE + SCOPE_RULE +
        "列出刀塔英雄，可按属性或定位筛选。"
        "当用户想看刀塔英雄列表、某个属性的英雄、某个定位的英雄时调用。"
        "返回数据后，请根据以下要点为用户生成简洁分析：\n"
        "1. 如果有筛选条件，简要说明筛选结果的特点\n"
        "2. 如果用户问「推荐英雄」，根据列表推荐 2-3 个适合当前版本的英雄"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "text_only": {
                    "type": "boolean",
                    "description": "仅当用户明确要求「纯文字」「不要图片」或输入以 --text 结尾时设为 true",
                },
                "attribute": {
                    "type": "string",
                    "description": "主属性筛选：str(力量)、agi(敏捷)、int(智力)、all(全能)，留空则全部",
                    "enum": ["str", "agi", "int", "all", ""],
                },
                "role": {
                    "type": "string",
                    "description": "定位筛选：Carry、Support、Nuker、Disabler、Initiator、Escape、Pusher，留空则全部",
                },
            },
            "required": [],
        }
    )
    client: object = Field(default=None, exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        from ..core.templates import render_hero_list

        attribute = str(kwargs.get("attribute") or "").strip()
        role = str(kwargs.get("role") or "").strip()

        try:
            heroes = await self.client.get_heroes()
            if not heroes:
                return "获取英雄数据失败。"

            markdown = render_hero_list(heroes, filter_attr=attribute, filter_role=role)
        except Exception as exc:
            return f"查询英雄列表失败：{exc}"

        return await deliver_result(context, markdown, text_only=bool(kwargs.get("text_only")))
