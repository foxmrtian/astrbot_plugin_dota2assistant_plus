"""卡片投递可靠性：钩子有界等待 + 超时转后台主动推送。

背景（用户报障）：战绩查询卡片经常因 ``on_decorating_result`` 钩子超时而发不出去。
根因是钩子里**同步**调模型写点评再渲染，钩子被拖到超时后本轮事件被提前关闭，
``set_result`` 静默失效 —— 图片明明已渲染好却发不出去。

修法：钩子只等 ``card_hook_budget`` 秒（实测 GLM-4-Flash 写点评只要 2~3 秒），
窗口内完成照旧 ``set_result``；超时则立刻让出，由后台任务用
``context.send_message`` 主动推送（不依赖本轮事件的生命周期）。

本文件同时守住两个曾真实踩到的坑：
* ``_push_image`` 写死了 ``astrbot.core.message.message_chain``，在 AstrBot 4.28
  上是 ImportError，补发任务每次都在这一步挂掉（图渲染好了却发不出去）；
* 钩子重新变回同步等模型（``await summarize``）。
"""
from __future__ import annotations

import asyncio
import inspect
import time
import types
import unittest

from astrbot_plugin_dota2assistant_plus import main
from astrbot_plugin_dota2assistant_plus.main import Dota2AssistantPlugin as Plugin


class _FakeEvent:
    """只实现投递路径真正用到的事件接口。"""

    def __init__(self):
        self.result = None
        self.unified_msg_origin = "aiocqhttp:group:10086"

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def set_result(self, result):
        self.result = result

    def track_temporary_local_file(self, path):  # pragma: no cover - 仅兼容
        pass


def _harness(budget: float):
    """构造一个只绑定投递相关方法的轻量插件替身（不构造完整插件）。"""
    h = types.SimpleNamespace()
    h.card_hook_budget = budget
    h.pushed = []
    h.feedback = []

    for name in ("_deliver_with_budget", "_emit_image", "_push_generated", "_spawn"):
        setattr(h, name, types.MethodType(getattr(Plugin, name), h))
    # staticmethod：类上取到的已是普通函数，直接挂上（不注入 self）
    h._clear_result = Plugin._clear_result

    async def _emit_feedback(event, success):
        h.feedback.append(success)

    async def _push_image(event, path):
        h.pushed.append(("image", str(path)))
        return True

    async def _push_text(event, text):
        h.pushed.append(("text", text))
        return True

    h._emit_feedback = _emit_feedback
    h._push_image = _push_image
    h._push_text = _push_text
    return h


async def _wait_until(cond, timeout: float = 3.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(step)
    return cond()


class TestCardHookBudget(unittest.IsolatedAsyncioTestCase):
    """钩子必须有界等待，慢任务不能被它拖住。"""

    async def test_fast_producer_uses_set_result(self):
        """窗口内完成：照旧 set_result（单条消息，不发后台推送）。"""
        h = _harness(budget=2.0)
        ev = _FakeEvent()

        async def producer():
            return None, "# 卡片\n测试", "点评", "模型生成"

        await h._deliver_with_budget(ev, producer)

        self.assertIsNotNone(ev.result, "快路径应把结果写回事件")
        self.assertEqual(ev.result[0], "plain")
        self.assertIn("卡片", ev.result[1])
        self.assertEqual(h.pushed, [], "快路径不应额外推送")
        self.assertEqual(h.feedback, [True])

    async def test_slow_producer_does_not_block_the_hook(self):
        """慢任务：钩子必须很快返回，并清空占位结果，改由后台推送。"""
        h = _harness(budget=0.05)
        ev = _FakeEvent()
        gate = asyncio.Event()

        async def producer():
            await gate.wait()          # 模拟模型迟迟不返回
            return None, "# 卡片\n完整数据", "点评", "模型生成"

        started = time.monotonic()
        await h._deliver_with_budget(ev, producer)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0, f"钩子被慢任务阻塞了 {elapsed:.2f}s")
        self.assertEqual(ev.result, ("chain", []), "应清空占位文本，避免与卡片重复")
        self.assertEqual(h.pushed, [], "此时后台还没推送")

        gate.set()                      # 放行后，后台任务应把卡片推出去
        self.assertTrue(
            await _wait_until(lambda: bool(h.pushed)), "后台没有补发卡片"
        )
        self.assertEqual(h.pushed[0][0], "text")
        self.assertIn("完整数据", h.pushed[0][1])

    async def test_slow_producer_still_delivers_image_when_ready(self):
        """图片路径同样走后台推送（此处用文本替身验证分支被走到）。"""
        h = _harness(budget=0.02)
        ev = _FakeEvent()
        gate = asyncio.Event()

        async def producer():
            await gate.wait()
            return None, "# 卡片", "点评", "模型生成"

        await h._deliver_with_budget(ev, producer)
        self.assertEqual(ev.result, ("chain", []))
        gate.set()
        self.assertTrue(await _wait_until(lambda: bool(h.pushed)))

    async def test_producer_error_reports_instead_of_silence(self):
        """生成异常时要有可见反馈，不能静默丢消息。"""
        h = _harness(budget=2.0)
        ev = _FakeEvent()

        async def producer():
            raise RuntimeError("boom")

        await h._deliver_with_budget(ev, producer)

        self.assertIsNotNone(ev.result, "异常时应有兜底回复")
        self.assertEqual(ev.result[0], "plain")
        self.assertEqual(h.feedback, [False])

    async def test_timeout_keeps_background_task_alive(self):
        """超时不能取消后台任务（否则卡片永远生不出来）。"""
        h = _harness(budget=0.02)
        ev = _FakeEvent()
        finished = []

        async def producer():
            await asyncio.sleep(0.15)
            finished.append(True)
            return None, "# 卡片", "点评", "模型生成"

        await h._deliver_with_budget(ev, producer)
        self.assertTrue(
            await _wait_until(lambda: bool(finished)), "后台任务被取消了"
        )
        self.assertTrue(await _wait_until(lambda: bool(h.pushed)))


class TestPushReturnValue(unittest.TestCase):
    """推送返回值必须只反映「有没有送出去」，不能被表情反馈之类的旁枝带偏。

    实测踩到过：``_emit_feedback`` 写在 try 块里，它一抛异常就被外层
    ``except`` 捕获，于是「推送成功」被误判成失败，日志还写成「补发异常」。
    """

    def _run(self, send_result: bool, feedback_raises: bool):
        from astrbot_plugin_dota2assistant_plus import compat

        original = compat.MessageChain

        class _FakeChain:
            def __init__(self, chain=None):
                self.chain = list(chain or [])

            def message(self, text):
                self.chain.append(text)
                return self

        compat.MessageChain = _FakeChain
        try:
            sent = []

            class _Ctx:
                async def send_message(self, session, chain):
                    sent.append(session)
                    return send_result

            h = types.SimpleNamespace(context=_Ctx())

            async def _feedback(event, success):
                if feedback_raises:
                    raise RuntimeError("emoji backend down")

            h._emit_feedback = _feedback
            h._push_text = types.MethodType(Plugin._push_text, h)
            ev = _FakeEvent()
            ok = asyncio.run(h._push_text(ev, "测试文本"))
            return ok, sent, ev
        finally:
            compat.MessageChain = original

    def test_success_reported_even_if_feedback_fails(self):
        ok, sent, ev = self._run(send_result=True, feedback_raises=True)
        self.assertTrue(ok, "表情反馈失败不该把「推送成功」判成失败")
        self.assertEqual(sent, [ev.unified_msg_origin])

    def test_missing_platform_reports_false(self):
        ok, sent, _ev = self._run(send_result=False, feedback_raises=False)
        self.assertFalse(ok, "没找到平台时应如实返回 False")
        self.assertEqual(len(sent), 1)


class TestBackgroundTaskReferences(unittest.TestCase):
    """后台任务必须保留强引用（asyncio 只持弱引用，否则任务可能被 GC）。"""

    def test_spawn_keeps_reference_and_cleans_up(self):
        async def main_flow():
            h = types.SimpleNamespace()
            h._spawn = types.MethodType(Plugin._spawn, h)
            ran = []

            async def worker():
                await asyncio.sleep(0)
                ran.append(True)

            task = h._spawn(worker())
            self.assertIn(task, h._bg_tasks, "任务没有被登记，随时可能被 GC")
            await task
            await asyncio.sleep(0)
            self.assertEqual(ran, [True])
            self.assertNotIn(task, h._bg_tasks, "完成后应从集合移除，避免泄漏")

        asyncio.run(main_flow())

    def test_no_unreferenced_create_task_in_main(self):
        """main.py 里不允许裸 create_task（返回值没人接手＝弱引用）。"""
        src = inspect.getsource(main)
        self.assertNotIn(
            "asyncio.create_task(",
            src,
            "裸 create_task 的返回值无人引用，任务可能在跑完前被 GC",
        )


class TestPushChannelCompatibility(unittest.TestCase):
    """补发通道的导入兼容性（曾因写死模块路径而全线失效）。"""

    def test_push_image_does_not_hardcode_removed_module(self):
        src = inspect.getsource(Plugin._push_image)
        self.assertNotIn(
            "astrbot.core.message.message_chain",
            src,
            "该模块在 AstrBot 4.28 已不存在，写死会让补发任务每次 ImportError",
        )
        self.assertIn("compat import MessageChain", src)

    def test_push_text_uses_compat_import(self):
        src = inspect.getsource(Plugin._push_text)
        self.assertNotIn("astrbot.core.message.message_chain", src)
        self.assertIn("compat import MessageChain", src)

    def test_compat_exposes_message_chain(self):
        from astrbot_plugin_dota2assistant_plus import compat

        self.assertTrue(hasattr(compat, "MessageChain"))
        # 本地无 astrbot 时允许为 None，有则必须是可调用的类
        self.assertTrue(compat.MessageChain is None or callable(compat.MessageChain))


class TestHookStaysNonBlocking(unittest.TestCase):
    """钩子本体不得再同步等待模型/渲染。"""

    def test_hook_goes_through_budget_helper(self):
        src = inspect.getsource(Plugin._decorate_dota_card)
        self.assertIn("_deliver_with_budget", src)

    def test_hook_does_not_await_summarize_directly(self):
        src = inspect.getsource(Plugin._decorate_dota_card)
        self.assertNotIn(
            "await summarize",
            src,
            "钩子里同步等模型正是「卡片发不出去」的根因",
        )

    def test_parsed_match_also_uses_budget(self):
        """单场（已解析）路径也不能同步等待录像总结。"""
        src = inspect.getsource(Plugin._deliver_match_card)
        self.assertIn("_deliver_with_budget", src)

    def test_default_budget_is_sane(self):
        self.assertGreater(main._CARD_HOOK_BUDGET, 0)
        self.assertLessEqual(
            main._CARD_HOOK_BUDGET, 15,
            "预算过大等于没有预算：钩子仍可能被拖到超时",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
