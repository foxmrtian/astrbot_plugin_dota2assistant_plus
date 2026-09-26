"""测试表情反馈模块。

由于该模块依赖运行时 event.bot 上的 set_msg_emoji_like，测试中全部用
伪对象代替。重点验证：

* 查询开始时发送 ❤；
* 同一查询多次调用 start 只发一次；
* 成功/失败时分别追加 👌 / 😢；
* event 结构缺失时静默跳过，不抛异常。
"""

import sys
import types
import unittest
from pathlib import Path

# 让独立运行 unittest 时也能用包名导入
_pkg = "astrbot_plugin_dota2assistant_plus"
if _pkg not in sys.modules:
    _mod = types.ModuleType(_pkg)
    _mod.__path__ = [str(Path(__file__).resolve().parent.parent)]
    _mod.__package__ = _pkg
    sys.modules[_pkg] = _mod

from astrbot_plugin_dota2assistant_plus.core.emoji_feedback import (
    _bot,
    _message_id,
    on_query_failure,
    on_query_start,
    on_query_success,
    reset,
)


class FakeBot:
    def __init__(self):
        self.calls = []

    async def set_msg_emoji_like(self, *, message_id, emoji, is_add):
        self.calls.append((message_id, emoji, is_add))


class FakeRawMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeEvent:
    def __init__(self, session_id="s1", message_id=42, bot=None, raw_mid=None):
        self.session_id = session_id
        self.message_id = message_id
        self.bot = bot
        self.message_obj = None
        if raw_mid is not None:
            self.message_obj = FakeRawMessage(raw_mid)


class TestEmojiFeedback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()

    async def test_start_sends_heart(self):
        bot = FakeBot()
        event = FakeEvent(bot=bot)
        await on_query_start(event)
        self.assertEqual(bot.calls, [(42, "❤", True)])

    async def test_start_deduplicates(self):
        bot = FakeBot()
        event = FakeEvent(bot=bot)
        await on_query_start(event)
        await on_query_start(event)
        self.assertEqual(len(bot.calls), 1)

    async def test_success_appends_ok(self):
        bot = FakeBot()
        event = FakeEvent(bot=bot)
        await on_query_start(event)
        await on_query_success(event)
        self.assertEqual(bot.calls, [(42, "❤", True), (42, "👌", True)])

    async def test_failure_appends_cry(self):
        bot = FakeBot()
        event = FakeEvent(bot=bot)
        await on_query_start(event)
        await on_query_failure(event)
        self.assertEqual(bot.calls, [(42, "❤", True), (42, "😢", True)])

    async def test_no_bot_noop(self):
        event = FakeEvent()
        # 不应该抛异常
        await on_query_start(event)
        await on_query_success(event)
        await on_query_failure(event)

    async def test_no_message_id_noop(self):
        bot = FakeBot()
        event = FakeEvent(bot=bot, message_id=None)
        await on_query_start(event)
        self.assertEqual(bot.calls, [])

    def test_message_id_from_raw(self):
        event = FakeEvent(message_id=None, raw_mid=99)
        self.assertEqual(_message_id(event), 99)

    def test_bot_from_astrbot_platform(self):
        class Platform:
            pass

        platform = Platform()
        platform.bot = FakeBot()
        event = FakeEvent()
        event.astrbot_platform = platform
        self.assertIs(_bot(event), platform.bot)


if __name__ == "__main__":
    unittest.main()
