"""工具结果的统一交付层：把卡片数据暂存到 event，交给 on_decorating_result 出图。

设计取舍（相比 v0.2.0 的重要变化）：

* v0.2.0 由工具自己调用 ``event.send()`` 发图。实测在真实流水线里不可靠：
  工具拿到的 ``context.context.event`` 未必可用，失败时静默退化成文本，
  且图片与 LLM 的点评是两条独立消息，无法把点评排进图片底部。
* 现在工具只负责「暂存 Markdown + 返回一句提示」，真正出图由
  ``main.py`` 的 ``on_decorating_result`` 钩子完成。该钩子在 LLM 产出
  最终点评 *之后* 运行，因此可以把点评作为最后一个区块拼进卡片，
  天然满足「总结放最下面、不与图表重叠」。
* ``--text`` / ``text_only=True`` 时完全不暂存，按纯文本返回，
  并让钩子放过这条消息（绑定/解绑等命令同理）。
"""
from __future__ import annotations

import re

from ..core.templates import markdown_to_plain

# 暂存键：事件级 extras，随事件生命周期存在
CARD_EXTRA_KEY = "dota2_card_pending"

# 用户点名关注的英雄。工具在 call() 里写，on_decorating_result 钩子读取后
# 传给 summarize()：总结是钩子发起的第二次模型调用，拿不到工具参数。
FOCUS_HERO_EXTRA_KEY = "dota2_focus_hero"

_TEXT_FLAGS = ("--text",)
# 注意：``--image`` 是历史遗留的兼容写法，出图本是默认行为，无需任何标志
# （见 main.py 的 _extract_output_mode）。此处不再定义常量，避免被误当成开关。

# 返回给 LLM 的提示。
#
# 赛后总结由**插件自己**调大模型生成（main.py 的 on_decorating_result 钩子 →
# core.summarizer），并渲染进图片底部的「综合分析」区块。工具这里并没有把
# 十名玩家的 KDA / 经济 / 经验回传给主模型，因此**明确不要**让主模型去写
# 逐玩家的数据点评 —— 它拿不到数据，只能写定性判断，反而与图片里的总结打架。
# 早先的提示写的是「请仅补充 1-3 句简要分析」，主模型据此抱怨
# 「具体数值没有回传到我这边」，已修正。
_HINT = (
    "Dota2 数据卡片已交给插件发送给用户（默认发图片；"
    "若管理员在配置里关闭了图片输出，则发 Markdown 文档）。"
    "末尾的评价也已由插件自动生成并附在卡片最下方 —— "
    "查**具体某一局**时是「综合分析」（天辉方的表现 / 夜魇方的表现 / "
    "一句话总结三个小栏目），其余查询（近期战绩 / 英雄 / 物品 / 实时 / "
    "职业等）是与该份数据对应的「点评」。"
    "你只需用一句话简短确认即可，不要复述卡片数据、"
    "不要另写逐玩家的数据点评（你手上没有这些明细）。"
    "切勿自己生成图片：不要调用 astrbot_execute_python / astrbot_execute_shell "
    "或其它工具绘图，卡片与图片都已由插件输出。"
)


def wants_text(event, text_only: bool = False) -> bool:
    """判断本次请求是否要求纯文本输出。

    优先采用 LLM 传入的结构化参数；同时直接检查原始消息，
    这样即使用户写了 ``--text`` 而模型没传参也能生效。
    参数需作为独立词出现，避免 ``--textual`` 之类的误匹配。
    """
    if text_only:
        return True

    try:
        message = str(getattr(event, "message_str", "") or "").strip().lower()
    except Exception:
        return False
    if not message:
        return False

    return any(re.search(rf"(?:^|\s){re.escape(flag)}(?:\s|$)", message) for flag in _TEXT_FLAGS)


def stash_card(event, markdown: str) -> None:
    """把待渲染的 Markdown 追加暂存到事件上，供 on_decorating_result 钩子取用。

    用列表累积：一轮对话里模型可能连续调用多个工具（例如同时查比赛和玩家），
    这些卡片需要合并成一张图片，而不是互相覆盖。
    """
    try:
        pending = event.get_extra(CARD_EXTRA_KEY)
        if not isinstance(pending, list):
            pending = []
        pending = [*pending, markdown]
        event.set_extra(CARD_EXTRA_KEY, pending)
    except Exception:
        pass


def pop_cards(event) -> list[str]:
    """取出并清除暂存的 Markdown 列表；没有则返回空列表。"""
    try:
        pending = event.get_extra(CARD_EXTRA_KEY)
    except Exception:
        return []
    # 及时清除：后续无关回复不应被误判成卡片请求
    try:
        event.set_extra(CARD_EXTRA_KEY, None)
    except Exception:
        pass
    if not pending:
        return []
    if isinstance(pending, str):
        return [pending]
    if isinstance(pending, list):
        return [str(item) for item in pending if item]
    return []


def pop_focus_hero(event) -> str:
    """取出并清除用户点名的关注英雄；没有则返回空串。

    与 pop_cards 一样在读取后立即清除：下一轮无关对话不应继续带着
    上一轮的英雄名，否则会莫名其妙多点评一段。
    """
    try:
        focus = event.get_extra(FOCUS_HERO_EXTRA_KEY)
    except Exception:
        return ""
    try:
        event.set_extra(FOCUS_HERO_EXTRA_KEY, None)
    except Exception:
        pass
    return str(focus or "").strip()


async def deliver_result(
    context,
    markdown: str,
    *,
    text_only: bool = False,
) -> str:
    """交付工具结果。

    Args:
        context: LLM 工具的 ContextWrapper。
        markdown: 已渲染好的 Markdown 文本。
        text_only: 强制纯文本输出（来自工具参数）。

    Returns:
        返回给 LLM 的字符串：出图路径下是简短提示，纯文本路径下是完整文本。
    """
    text = (markdown or "").strip()
    if not text:
        return "没有查询到可展示的数据。"

    event = getattr(getattr(context, "context", None), "event", None)

    # 用户要求纯文本：降级掉图片/链接语法后交给 LLM
    if wants_text(event, text_only):
        return markdown_to_plain(text)

    # 事件不可用（测试或异常上下文）：退回纯文本，保证数据不丢
    if event is None:
        return markdown_to_plain(text)

    stash_card(event, text)
    return _HINT
