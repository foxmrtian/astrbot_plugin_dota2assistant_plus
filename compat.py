from __future__ import annotations

import logging
from dataclasses import dataclass as stdlib_dataclass
from dataclasses import field as stdlib_field
from typing import Any, Generic, TypeVar

try:
    from astrbot.api import logger as logger
except Exception:
    logger = logging.getLogger("astrbot_plugin_dota2assistant_plus")


try:
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star
except Exception:

    class _Filter:
        @staticmethod
        def command(*_args: Any, **_kwargs: Any):
            def decorator(func: Any) -> Any:
                return func
            return decorator

        @staticmethod
        def command_group(*_args: Any, **_kwargs: Any):
            def decorator(func: Any) -> Any:
                return _CommandGroupDecorator(func)
            return decorator

        @staticmethod
        def llm_tool(*_args: Any, **_kwargs: Any):
            def decorator(func: Any) -> Any:
                return func
            return decorator

        @staticmethod
        def on_decorating_result(*_args: Any, **_kwargs: Any):
            """兼容 AstrBot 的 on_decorating_result 装饰器 stub。"""
            def decorator(func: Any) -> Any:
                return func
            return decorator

    class _CommandGroupDecorator:
        def __init__(self, func):
            self.func = func

        def __call__(self, *args, **kwargs):
            return self.func(*args, **kwargs)

        def command(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    class AstrMessageEvent:
        message_str = ""

        @staticmethod
        def plain_result(message: str) -> str:
            return message

    class Context:
        pass

    class Star:
        def __init__(self, context: Context):
            self.context = context

    filter = _Filter()


T = TypeVar("T")

try:
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.tool import FunctionTool, ToolExecResult
    from astrbot.core.astr_agent_context import AstrAgentContext
    from pydantic import Field
    from pydantic.dataclasses import dataclass
except Exception:

    class FunctionTool(Generic[T]):
        pass

    class ContextWrapper(Generic[T]):
        pass

    class AstrAgentContext:
        pass

    ToolExecResult = str
    dataclass = stdlib_dataclass

    def Field(default: Any = None, default_factory: Any = None, **_kwargs: Any) -> Any:
        if default_factory is not None:
            return stdlib_field(default_factory=default_factory)
        return stdlib_field(default=default)


# ---------------------------------------------------------------- 主动推送
#
# ``context.send_message`` 需要一条 MessageChain。不同 AstrBot 版本的模块
# 路径不同（4.28 在 ``message_event_result``，更早的在 ``message_chain``），
# 因此这里做一次兼容解析。
#
# 注意：早先 main.py 里写死了 ``astrbot.core.message.message_chain``，在
# 4.28 上直接 ImportError —— 补发卡片的后台任务每次都挂在这里，
# 表现就是「图渲染好了却发不出去」，且日志只有一行 error。
try:
    from astrbot.core.message.message_event_result import MessageChain
except Exception:  # pragma: no cover - 取决于 AstrBot 版本
    try:
        from astrbot.core.message.message_chain import MessageChain  # type: ignore
    except Exception:
        MessageChain = None  # type: ignore[assignment]
