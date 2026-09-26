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
