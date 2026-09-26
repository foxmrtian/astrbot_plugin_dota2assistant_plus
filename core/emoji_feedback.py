"""为 Dota2 查询消息提供表情反馈。

协议层（aiocqhttp / QQ）的会话里，用户触发查询后先给一颗 ❤ 表示收到；
查询成功并发出图片后再补一个 👌；失败时发出 😢。

生命周期选择「累加」：❤ 在调用时给出后一直保留，后续状态不再移除；
成功/失败时再在旁追加对应表情。这样最稳 —— 某些平台/版本不支持删除表
情，也不支持幂等更新，累加不存在时安全且语义清晰。

所有查询类型统一走这套反馈：比赛详情、玩家资料、英雄、物品、实时、职业等。
"""

from __future__ import annotations

from typing import Any, Optional

from ..compat import logger


# 生命周期：调用时累加的心；成功后追加 OK；失败后追加哭脸
_EMOJI_HEART = "❤"
_EMOJI_OK = "👌"
_EMOJI_CRY = "😢"

# 已发送表情的缓存键：event.session_id + message_id（如果拿不到 message_id
# 则退回到 session_id，保证同一轮不会重复发送）
_heart_sent: set[str] = set()


def _key(event: Any) -> str:
    """构造一个会话级 key 用于去重。"""
    session_id = getattr(event, "session_id", None) or ""
    message_id = getattr(event, "message_id", None) or ""
    if session_id and message_id:
        return f"{session_id}:{message_id}"
    return session_id or "global"


def _bot(event: Any) -> Optional[Any]:
    """从 event 里拿到 bot 实例。不同平台挂载点不同，优先常见路径。"""
    bot = getattr(event, "bot", None)
    if bot is not None:
        return bot
    # 有些版本把 bot 挂在 astrbot_platform 或 platform 上
    platform = getattr(event, "astrbot_platform", None) or getattr(event, "platform", None)
    if platform is not None:
        bot = getattr(platform, "bot", None)
        if bot is not None:
            return bot
    # AstrBot 事件可能在 message_event 上提供 bot
    raw = getattr(event, "message_event", None)
    if raw is not None:
        bot = getattr(raw, "bot", None)
        if bot is not None:
            return bot
    return None


def _message_id(event: Any) -> Optional[int]:
    """拿到用户触发消息的消息 ID；表情反馈需要它作为目标。"""
    # 优先 event.message_id，再尝试从原始消息对象里取
    mid = getattr(event, "message_id", None)
    if isinstance(mid, int):
        return mid
    raw = getattr(event, "message_obj", None) or getattr(event, "message_event", None)
    if raw is not None:
        mid = getattr(raw, "message_id", None) or getattr(raw, "msg_id", None)
        if isinstance(mid, int):
            return mid
    return None


async def _send_emoji(bot: Any, message_id: Optional[int], emoji: str) -> bool:
    """调用底层 set_msg_emoji_like；失败只记日志，不影响主流程。"""
    if not message_id:
        return False
    method = getattr(bot, "set_msg_emoji_like", None)
    if not callable(method):
        return False
    try:
        await method(message_id=message_id, emoji=emoji, is_add=True)
        return True
    except Exception as exc:
        logger.debug(f"Dota2 表情反馈发送失败 ({emoji}): {exc}")
        return False


async def on_query_start(event: Any) -> None:
    """用户刚触发查询时调用：累加一个 ❤。"""
    bot = _bot(event)
    if bot is None:
        return
    mid = _message_id(event)
    if mid is None:
        return
    key = _key(event)
    if key in _heart_sent:
        return
    if await _send_emoji(bot, mid, _EMOJI_HEART):
        _heart_sent.add(key)


async def on_query_success(event: Any) -> None:
    """查询成功并发出结果后调用：追加 👌。"""
    bot = _bot(event)
    if bot is None:
        return
    mid = _message_id(event)
    if mid is None:
        return
    await _send_emoji(bot, mid, _EMOJI_OK)


async def on_query_failure(event: Any) -> None:
    """查询失败或异常时调用：追加 😢。"""
    bot = _bot(event)
    if bot is None:
        return
    mid = _message_id(event)
    if mid is None:
        return
    await _send_emoji(bot, mid, _EMOJI_CRY)


def reset() -> None:
    """清理已发送缓存；主要用于测试间隔离，不建议业务代码调用。"""
    _heart_sent.clear()
