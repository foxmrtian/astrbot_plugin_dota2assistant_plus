"""pytest 引导：让 tests/ 能以运行时包名导入插件模块。

插件内部统一使用包相对导入（见 main.py 顶部注释），因此测试也必须按
``astrbot_plugin_dota2assistant_plus.<子模块>`` 的形式导入，才能和运行时保持同一套模块身份。

本地开发时仓库目录名未必叫 ``astrbot_plugin_dota2assistant_plus``（例如可能是 ``src``），
这里把仓库根目录注册成一个同名命名空间包，使两种布局下导入路径都成立。

注意：这里不做 ``sys.path.insert``；热重载时 star_manager._purge_modules() 只清理
带 ``data.plugins.<目录名>.*`` 前缀的模块，sys.path 插入会让某些模块注册到顶层
命名空间而永远不被清理（v0.3.x 之前的老写法留下的坑）。用命名空间包注册更安全。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_PKG = "astrbot_plugin_dota2assistant_plus"

# 目录名不匹配时，手动注册一个命名空间包指向仓库根
if _PKG not in sys.modules:
    _mod = types.ModuleType(_PKG)
    _mod.__path__ = [str(_ROOT)]
    _mod.__package__ = _PKG
    sys.modules[_PKG] = _mod
else:
    # 若已存在，把本仓库根目录追加到包路径里（兼容已安装包或测试引导）
    existing = sys.modules[_PKG]
    if not hasattr(existing, "__path__"):
        existing.__path__ = [str(_ROOT)]
    elif str(_ROOT) not in list(existing.__path__):
        existing.__path__.insert(0, str(_ROOT))

# 测试环境没有安装 AstrBot 时，注入一个最小 stub，让 main.py 能导入 Image/Plain。
# 真实运行时 AstrBot 已安装，这段不会执行（import 成功即跳过）。
try:
    import astrbot.core.message.components  # noqa: F401
except Exception:
    for _name in (
        "astrbot",
        "astrbot.core",
        "astrbot.core.message",
        "astrbot.core.message.components",
    ):
        if _name not in sys.modules:
            _m = types.ModuleType(_name)
            _m.__path__ = []
            sys.modules[_name] = _m
    _components = sys.modules["astrbot.core.message.components"]

    class _Image:
        @staticmethod
        def fromFileSystem(path: str):
            return ("image", path)

    class _Plain:
        def __init__(self, text: str = ""):
            self.text = text

    _components.Image = _Image
    _components.Plain = _Plain
