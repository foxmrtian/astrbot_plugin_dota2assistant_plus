"""配置 schema 与主题配色的完整性校验。

AstrBot 加载插件时会用 `_conf_schema.json` 生成默认配置。它的解析器对
`type: object` **硬取 `v["items"]`**（`astrbot_config.py` 的 `_parse_schema`），
没有 `.get()` 兜底。因此把子字段写成 `properties` 会让解析抛
`KeyError: 'items'`，**整个插件加载失败**：

    ----- Failed to load plugin astrbot_plugin_dota2assistant_plus -----
    KeyError: 'items'

真实事故：`card_theme` 用 `type: object` + `properties` 描述子项，
插件在插件管理页重载时直接失败。AstrBot 生态里的其他插件（self_learning、
dailylimit、context_aware 等）一律用 `items`，`properties` 是唯一写法错误。

第二组测试针对「同一份配置在两条渲染路径下外观不一致」：
`theme.accent` 同时被网络模板（CSS 变量）与本地 Pillow 渲染器消费，
若有任一路径漏读，用户会看到「有时变色、有时不变色」。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = _ROOT / "_conf_schema.json"

# AstrBot 支持的配置类型（见 astrbot_config.DEFAULT_VALUE_MAP）
_SUPPORTED_TYPES = {
    "string", "int", "float", "bool", "object", "list", "template_list",
}


class TestConfigSchema(unittest.TestCase):
    def _schema(self) -> dict:
        return json.loads(_SCHEMA.read_text(encoding="utf-8"))

    def test_schema_is_valid_json_object(self):
        schema = self._schema()
        self.assertIsInstance(schema, dict)
        self.assertTrue(schema, "配置 schema 不应为空")

    def test_object_entries_declare_items_not_properties(self):
        """`type: object` 必须用 `items` —— 写成 `properties` 会让插件加载失败。"""
        for key, spec in self._schema().items():
            with self.subTest(field=key):
                self.assertIn("type", spec, f"{key} 缺少 type")
                if spec["type"] != "object":
                    continue
                self.assertIn(
                    "items", spec,
                    f"配置项 {key} 是 object 却没有 items："
                    "AstrBot 的 _parse_schema 会抛 KeyError: 'items'，"
                    "导致整个插件无法加载；子字段要写在 items 里",
                )
                self.assertNotIn(
                    "properties", spec,
                    f"配置项 {key} 用了 properties，AstrBot 不识别该写法",
                )
                self.assertIsInstance(spec["items"], dict)
                self.assertTrue(spec["items"], f"{key}.items 不应为空")

    def test_every_type_is_supported_by_astrbot(self):
        for key, spec in self._schema().items():
            with self.subTest(field=key):
                self.assertIn(
                    spec.get("type"), _SUPPORTED_TYPES,
                    f"{key} 的类型 {spec.get('type')!r} 不受 AstrBot 支持",
                )

    def test_nested_entries_are_recursively_valid(self):
        """嵌套 object 同样要用 items —— 解析器是递归的。"""

        def walk(node: dict, path: str) -> None:
            for key, spec in node.items():
                if not isinstance(spec, dict) or "type" not in spec:
                    continue
                if spec["type"] == "object":
                    self.assertIn("items", spec, f"{path}{key} 缺 items")
                    walk(spec["items"], f"{path}{key}.")

        walk(self._schema(), "")

    def test_card_theme_preset_is_a_dropdown(self):
        """主题改为下拉框选预设名，不再开放逐色自定义。"""
        spec = self._schema()["card_theme_preset"]
        self.assertEqual(spec["type"], "string")
        self.assertEqual(spec["default"], "light")
        self.assertEqual(set(spec["options"]), {"light", "dark-gold", "pink", "navy-gold"})
        # 旧的逐色 card_theme 对象不应再出现在配置页
        self.assertNotIn("card_theme", self._schema())


class TestThemeReachesBothRenderPaths(unittest.TestCase):
    """theme 的每一项都要被两条渲染路径读到。

    漏读不会报错，只会让外观在两条路径间不一致（网络 t2i 可用时走网络，
    端点半途失败时退回本地），很难复现，因此用测试钉住。
    """

    def test_network_injects_all_theme_keys(self):
        import astrbot_plugin_dota2assistant_plus.core.card_renderer as cr

        theme = {
            "analysis_bg": "#f4f6f8",
            "analysis_line": "#d6dbe0",
            "accent": "#3b556f",
            "bg": "#ffffff",
            "panel": "#f7f9fc",
            "panel2": "#eef2f7",
            "line": "#e2e8f0",
            "ink": "#0f172a",
            "ink2": "#1e293b",
            "muted": "#475569",
            "faint": "#64748b",
            "gold": "#1e3a8a",
            "win": "#15803d",
            "lose": "#b91c1c",
        }
        html = cr._template(theme)
        self.assertIsNotNone(html, "模板应能读取到 card.html")
        for var, val in (
            ("--analysis-bg", "#f4f6f8"),
            ("--analysis-line", "#d6dbe0"),
            ("--accent", "#3b556f"),
            ("--bg", "#ffffff"),
            ("--panel", "#f7f9fc"),
            ("--ink", "#0f172a"),
            ("--gold", "#1e3a8a"),
            ("--win", "#15803d"),
            ("--lose", "#b91c1c"),
        ):
            with self.subTest(var=var):
                self.assertIn(
                    f"{var}:{val}", html, f"网络模板未把 {var} 换成配置值"
                )

    def test_network_template_keeps_default_without_theme(self):
        import astrbot_plugin_dota2assistant_plus.core.card_renderer as cr

        for theme in (None, {}):
            with self.subTest(theme=theme):
                html = cr._template(theme)
                # 默认浅色主题
                self.assertIn("--bg:#ffffff", html)
                self.assertIn("--panel:#f7f9fc", html)
                self.assertIn("--analysis-bg:#f7f9fc", html)
                self.assertIn("--accent:#1d4ed8", html)

    def test_local_renderer_applies_theme(self):
        """本地路径必须把三项都画出来，而不是只认底纹色。"""
        import tempfile

        from PIL import Image

        import astrbot_plugin_dota2assistant_plus.core.image_renderer as ir

        accent = (0x3B, 0x55, 0x6F)
        md = "# 玩家资料：x\n\n| 项目 | 数据 |\n|---|---|\n| 段位 | 万古流芳 |\n\n## 点评\n\n正文。\n"

        def render(theme):
            with tempfile.TemporaryDirectory() as d:
                out = str(Path(d) / "t.png")
                ir.render_text_to_image(md, out, theme=theme)
                with Image.open(out) as im:
                    return im.convert("RGB").copy()

        themed = render({"accent": "#3b556f"})
        default = render(None)

        # 顶部 3px 主色条是最稳定的探针：整行都是 accent 色
        self.assertEqual(themed.getpixel((themed.width // 2, 1)), accent)
        self.assertNotEqual(
            default.getpixel((default.width // 2, 1)), accent,
            "未配置 theme 时不应使用配置色",
        )

    def test_invalid_hex_falls_back_to_default(self):
        """配置写错颜色时退回默认值，不能抛异常、也不能画出异常色。"""
        import tempfile

        from PIL import Image

        import astrbot_plugin_dota2assistant_plus.core.image_renderer as ir

        # _hex_to_rgb 的边界
        self.assertIsNone(ir._hex_to_rgb(""))
        self.assertIsNone(ir._hex_to_rgb("not-a-color"))
        self.assertIsNone(ir._hex_to_rgb("#12345"))
        self.assertEqual(ir._hex_to_rgb("#abc"), (0xAA, 0xBB, 0xCC))
        self.assertEqual(ir._hex_to_rgb("3b556f"), (0x3B, 0x55, 0x6F))

        md = "## 点评\n\n正文。\n"
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "t.png")
            ir.render_text_to_image(md, out, theme={"accent": "垃圾值", "analysis_bg": "#zzz"})
            with Image.open(out) as im:
                img = im.convert("RGB")
            # 退回默认深蓝，而不是崩溃或黑色
            self.assertEqual(img.getpixel((img.width // 2, 1)), ir._ACCENT)


class TestIconPreheatBudget(unittest.TestCase):
    """prepare_icons 必须真的把预算传给 icon_cache.ensure。

    这里曾经删掉了模块级预算常量却留着引用，`NameError` 又被
    `except Exception` 吞成一行 warning —— 图标永远不预热，
    出图时只能退化成中文名，且日志里看不出是代码错误。
    """

    def test_budget_is_passed_through(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        import astrbot_plugin_dota2assistant_plus.core.card_renderer as cr
        import astrbot_plugin_dota2assistant_plus.core.icon_cache as icon_cache

        md = "![英雄](https://cdn.example.com/a.png)"
        ensure = AsyncMock(return_value={})
        with patch.object(icon_cache, "image_urls", return_value=["https://cdn.example.com/a.png"]), \
                patch.object(icon_cache, "ensure", new=ensure):
            asyncio.run(cr.prepare_icons(md))

        self.assertTrue(ensure.await_count, "prepare_icons 没有调用 icon_cache.ensure")
        budget = ensure.await_args.kwargs.get("budget")
        self.assertIsInstance(
            budget, (int, float),
            f"budget 应为数字，实际是 {budget!r}；"
            "若是 None/AttributeError 说明预算常量未定义",
        )
        self.assertGreater(budget, 0)

    def test_module_budget_constant_exists(self):
        import astrbot_plugin_dota2assistant_plus.core.card_renderer as cr

        self.assertTrue(
            hasattr(cr, "_ICON_BUDGET_SECONDS"),
            "_ICON_BUDGET_SECONDS 被引用但未定义，会让 prepare_icons 静默失败",
        )


class TestRenderCardAcceptsTheme(unittest.TestCase):
    """render_card 必须接受 theme 并向下透传。

    main.py 一直按 `render_card(md, host=self, theme=...)` 调用，
    而 render_card 曾漏掉该形参，导致每次出图都
    `TypeError: unexpected keyword argument 'theme'`，被 except 吞掉后
    静默退化成纯文本 —— 用户看到的是「图片没了」。
    """

    def test_signature_accepts_theme(self):
        import inspect

        import astrbot_plugin_dota2assistant_plus.core.card_renderer as cr

        params = inspect.signature(cr.render_card).parameters
        self.assertIn("theme", params, "render_card 缺少 theme 形参")
        self.assertIsNone(params["theme"].default)

    def test_theme_is_passed_to_both_paths(self):
        import asyncio
        import tempfile
        from unittest.mock import patch

        import astrbot_plugin_dota2assistant_plus.core.card_renderer as cr

        theme = {"accent": "#3b556f"}
        seen = {}

        class FakeHost:
            async def html_render(self, tmpl, data, return_url=False, options=None):
                seen["network"] = data
                return None  # 强制退回本地

        async def fake_network(markdown, host, theme=None):
            seen["network_theme"] = theme
            return None  # 强制退回本地

        def fake_local(markdown, theme=None):
            seen["local"] = theme
            with tempfile.TemporaryDirectory() as d:
                return None

        with patch.object(cr, "_render_network", new=fake_network), \
                patch.object(cr, "_render_local", new=fake_local):
            asyncio.run(cr.render_card("# 玩家资料：x\n", host=FakeHost(), theme=theme))

        # _normalize_theme 会把用户配置补全成完整令牌，但原始 accent 必须保留
        self.assertEqual(
            seen.get("network_theme", {}).get("accent"),
            theme["accent"],
            "网络路径未收到 theme.accent",
        )
        self.assertEqual(
            seen.get("local", {}).get("accent"),
            theme["accent"],
            "本地路径未收到 theme.accent",
        )


if __name__ == "__main__":
    unittest.main()
