"""图片输出路径的单元测试：渲染器、字体探测、交付层与降级行为。"""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_dota2assistant_plus.core import image_renderer as ir
from astrbot_plugin_dota2assistant_plus.core.image_renderer import FontSet, find_cjk_font, render_text_to_image
from astrbot_plugin_dota2assistant_plus.core.templates import markdown_to_plain
from astrbot_plugin_dota2assistant_plus.tools import delivery

try:
    from astrbot.core.message.components import Plain
except Exception:  # 脱离 AstrBot 运行时的本地测试
    class Plain:  # type: ignore[no-redef]
        def __init__(self, text=""):
            self.text = text

        def __eq__(self, other):
            return isinstance(other, Plain) and other.text == self.text

        def __hash__(self):
            return hash(self.text)


SAMPLE_MD = """# 玩家资料：KAKA

| 项目 | 数据 |
|------|------|
| 段位 | 中军 3星 |

## 近期战绩

**总体**: 5胜 5负（胜率 50%）

| 结果 | 英雄 | KDA | 时长 |
|------|------|-----|------|
| ❌ | Ember Spirit | 10/10/33 | 52:22 |
| ✅ | Ember Spirit | 15/23/27 | 1:05:40 |
"""


class FakeEvent:
    """最小可用的 AstrMessageEvent 替身。"""

    def __init__(self, message_str="", sender_id="1"):
        self.message_str = message_str
        self._sender_id = sender_id
        self.sent = []
        self.tracked = []
        self.result = None
        self._extras = {}

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def set_result(self, r):
        self.result = r

    def get_result(self):
        return self.result

    def clear_result(self):
        self.result = None

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def track_temporary_local_file(self, path):
        self.tracked.append(path)

    async def send(self, chain):
        self.sent.append(chain)


class FakeContext:
    def __init__(self, event):
        self.event = event


class FakeWrapper:
    def __init__(self, event):
        self.context = FakeContext(event)


class TestFontDiscovery(unittest.TestCase):
    def test_find_cjk_font_covers_chinese(self):
        path = find_cjk_font()
        if path is None:
            self.skipTest("运行环境未安装中文字体")
        self.assertTrue(ir._covers(path, "中文"))

    def test_find_cjk_font_ignores_missing_files(self):
        with patch.object(ir, "_CJK_FONT_CANDIDATES", ["/nonexistent/font.ttf"]), \
                patch.object(ir, "_glyph_cache", {}):
            self.assertIsNone(find_cjk_font())

    def test_fontset_usable_flag(self):
        self.assertTrue(FontSet(18).usable)

    def test_fontset_picks_different_font_for_emoji(self):
        fs = FontSet(18)
        if fs.cjk is None or fs.emoji is None:
            self.skipTest("缺少中文字体或 emoji 字体")
        # 若环境里没有独立 emoji 字体，emoji 会退回与中文字体同一文件；
        # 此时 pick 结果相同是合理 fallback，不是 bug。
        if fs.cjk == fs.emoji:
            self.skipTest("当前环境无独立 emoji 字体，emoji 与中文字体共用")
        self.assertIsNot(fs.pick("中"), fs.pick("✅"))


class TestLocalRenderer(unittest.TestCase):
    def test_renders_file(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.png")
            render_text_to_image(SAMPLE_MD, out)
            self.assertTrue(os.path.exists(out))
            self.assertGreater(os.path.getsize(out), 1024)

    def test_card_width_fits_content(self):
        import tempfile
        import os
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.png")
            render_text_to_image(SAMPLE_MD, out)
            w, h = Image.open(out).size
            self.assertLessEqual(w, 890)  # 不会无脑用满最大宽度
            self.assertGreater(h, 80)

    def test_long_content_is_not_truncated(self):
        import tempfile
        import os
        from PIL import Image
        rows = "\n".join(f"| ❌ | H{i} | {i}/1/1 | 52:22 |" for i in range(60))
        md = ("# 战绩\n\n| 结果 | 英雄 | KDA | 时长 |\n|---|---|---|---|\n" + rows)
        with tempfile.TemporaryDirectory() as d:
            small = os.path.join(d, "s.png")
            large = os.path.join(d, "l.png")
            render_text_to_image("# 战绩\n", small)
            render_text_to_image(md, large)
            self.assertGreater(Image.open(large).size[1], Image.open(small).size[1] * 3)

    def test_empty_text_still_produces_image(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.png")
            render_text_to_image("", out)
            self.assertTrue(os.path.exists(out))


class TestOutputMode(unittest.TestCase):
    def test_default_is_image(self):
        ev = FakeEvent("糖糖 查一下奇迹哥的战绩")
        self.assertFalse(delivery.wants_text(ev))

    def test_text_flag(self):
        self.assertTrue(delivery.wants_text(FakeEvent("糖糖 查战绩 --text")))

    def test_text_flag_with_spacing(self):
        self.assertTrue(delivery.wants_text(FakeEvent("糖糖 查战绩   --text")))

    def test_text_flag_case_insensitive(self):
        self.assertTrue(delivery.wants_text(FakeEvent("糖糖 查战绩 --TEXT")))

    def test_image_flag_is_not_text(self):
        self.assertFalse(delivery.wants_text(FakeEvent("糖糖 查战绩 --image")))

    def test_structured_param_wins(self):
        self.assertTrue(delivery.wants_text(FakeEvent("糖糖 查战绩"), text_only=True))

    def test_substring_is_not_a_flag(self):
        # "--textual" 不应被当成 --text
        self.assertFalse(delivery.wants_text(FakeEvent("糖糖 查战绩 --textual")))


class TestDelivery(unittest.TestCase):
    """交付层只负责「暂存卡片 + 返回提示」，出图由 on_decorating_result 完成。"""

    def test_stashes_card_by_default(self):
        ev = FakeEvent("糖糖 查战绩")
        ret = asyncio.run(delivery.deliver_result(FakeWrapper(ev), SAMPLE_MD))
        # 返回提示而不是全文，避免模型把明细再抄一遍
        self.assertIn("图片", ret)
        self.assertNotEqual(ret, SAMPLE_MD.strip())
        # 卡片已暂存，等待钩子渲染
        self.assertEqual(delivery.pop_cards(ev), [SAMPLE_MD.strip()])

    def test_multiple_cards_merge(self):
        ev = FakeEvent("糖糖 查战绩")
        asyncio.run(delivery.deliver_result(FakeWrapper(ev), SAMPLE_MD))
        asyncio.run(delivery.deliver_result(FakeWrapper(ev), "# 第二张卡\n"))
        self.assertEqual(len(delivery.pop_cards(ev)), 2)

    def test_pop_clears_pending(self):
        ev = FakeEvent("糖糖 查战绩")
        asyncio.run(delivery.deliver_result(FakeWrapper(ev), SAMPLE_MD))
        self.assertEqual(len(delivery.pop_cards(ev)), 1)
        # 取过一次后不应再次返回，避免影响后续无关回复
        self.assertEqual(delivery.pop_cards(ev), [])

    def test_text_only_skips_stash(self):
        ev = FakeEvent("糖糖 查战绩")
        ret = asyncio.run(delivery.deliver_result(FakeWrapper(ev), SAMPLE_MD, text_only=True))
        # 纯文本路径会把行内标记降级掉（用户不该看到 ** 或装备的 CDN 链接）
        self.assertEqual(ret, markdown_to_plain(SAMPLE_MD))
        self.assertNotIn("**", ret)
        self.assertEqual(delivery.pop_cards(ev), [])

    def test_flag_skips_stash(self):
        ev = FakeEvent("糖糖 查战绩 --text")
        ret = asyncio.run(delivery.deliver_result(FakeWrapper(ev), SAMPLE_MD))
        self.assertEqual(ret, markdown_to_plain(SAMPLE_MD))
        self.assertEqual(delivery.pop_cards(ev), [])

    def test_text_mode_strips_item_icon_urls(self):
        """装备图标在纯文本路径必须只剩物品名，不能把 CDN 链接发给用户。"""
        ev = FakeEvent("糖糖 查比赛 --text")
        md = "装备：![黑皇杖](https://cdn.example/black_king_bar.png)"
        ret = asyncio.run(delivery.deliver_result(FakeWrapper(ev), md))
        self.assertEqual(ret, "装备：黑皇杖")
        self.assertNotIn("http", ret)

    def test_empty_result(self):
        ev = FakeEvent("糖糖 查战绩")
        ret = asyncio.run(delivery.deliver_result(FakeWrapper(ev), "   "))
        self.assertTrue(ret)
        self.assertEqual(delivery.pop_cards(ev), [])

    def test_no_event_falls_back(self):
        class NoEvent:
            context = FakeContext(None)
        ret = asyncio.run(delivery.deliver_result(NoEvent(), "数据"))
        self.assertEqual(ret, "数据")


class TestComposeCard(unittest.TestCase):
    """main.py 的卡片拼装：LLM 评价必须落在最下方。"""

    def test_analysis_is_last_block(self):
        from astrbot_plugin_dota2assistant_plus.main import Dota2AssistantPlugin

        plugin = object.__new__(Dota2AssistantPlugin)
        md = plugin._compose_card_markdown(["# 数据\n\n| a | b |"], "打得很稳")
        self.assertTrue(md.endswith("打得很稳"))
        self.assertIn("## 综合分析", md)
        # 数据在前、评价在后
        self.assertLess(md.index("数据"), md.index("综合分析"))

    def test_multiple_cards_kept_in_order(self):
        from astrbot_plugin_dota2assistant_plus.main import Dota2AssistantPlugin

        plugin = object.__new__(Dota2AssistantPlugin)
        md = plugin._compose_card_markdown(["# 卡片A", "# 卡片B"], "评价")
        self.assertLess(md.index("卡片A"), md.index("卡片B"))
        self.assertTrue(md.endswith("评价"))

    def test_no_analysis_keeps_cards_only(self):
        from astrbot_plugin_dota2assistant_plus.main import Dota2AssistantPlugin

        plugin = object.__new__(Dota2AssistantPlugin)
        md = plugin._compose_card_markdown(["# 数据"], "")
        self.assertEqual(md, "# 数据")

    def test_analysis_truncated_when_too_long(self):
        from astrbot_plugin_dota2assistant_plus.main import _MAX_ANALYSIS_CHARS, Dota2AssistantPlugin

        plugin = object.__new__(Dota2AssistantPlugin)

        class Ev:
            def get_result(self):
                return None

        self.assertEqual(plugin._extract_analysis(Ev()), "")
        self.assertGreater(_MAX_ANALYSIS_CHARS, 0)


class TestWhiteTheme(unittest.TestCase):
    """白底主题：背景 #ffffff，分栏目标题深蓝色，详情正文黑色。"""

    BG = (255, 255, 255)    # #ffffff
    HEAD = (30, 58, 138)    # #1e3a8a 深蓝
    INK = (0, 0, 0)         # 黑色正文
    ANALYSIS_BG = (255, 248, 239)  # #fff8ef 浅暖色底纹

    def test_background_is_white(self):
        import tempfile

        from PIL import Image

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "d.png")
            render_text_to_image("# 战绩\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", out)
            with Image.open(out).convert("RGB") as img:
                w, h = img.size
                corner = img.getpixel((w - 6, h // 2))
            self.assertEqual(corner, self.BG, f"背景应为 #ffffff，实际 {corner}")

    def test_heading_is_deep_blue(self):
        """分栏目标题必须用深蓝色 #1e3a8a。"""
        import tempfile

        from PIL import Image

        card = "# 战绩\n\n## 双方阵容\n\n| 英雄 | KDA |\n|---|---|\n| 灰烬之灵 | 12/1/9 |\n"
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "h.png")
            render_text_to_image(card, out)
            with Image.open(out).convert("RGB") as img:
                w, h = img.size
                px = img.load()
            found = False
            for y in range(h):
                for x in range(w):
                    r, g, b = px[x, y]
                    # 深蓝：蓝通道显著高于红通道，且不是白/灰（蓝通道足够低）
                    if b >= 100 and b <= 200 and b - r >= 40 and g <= 200:
                        found = True
                        break
                if found:
                    break
            self.assertTrue(found, "未找到深蓝色标题像素（应为 #1e3a8a）")

    def test_body_text_is_black(self):
        """详情正文必须是黑色（白底下深色墨字）。

        判据用 max(通道) <= 40：深蓝标题 (30,58,138) 的 max 是 138，
        因此该计数天然只命中黑色正文，不会把蓝色标题算进来。
        """
        import tempfile

        from PIL import Image

        card = "# 标题\n\n这是一段正文说明文字，应当渲染为黑色。\n"
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "w.png")
            render_text_to_image(card, out)
            with Image.open(out).convert("RGB") as img:
                px = img.load()
                w, h = img.size
                black = sum(
                    1 for y in range(h) for x in range(w) if max(px[x, y]) <= 40
                )
        self.assertGreater(
            black, 200, f"正文黑色像素过少（{black}），文字可能不是黑色"
        )

    def test_body_is_black_while_heading_is_blue(self):
        """正文与分栏目标题必须是两种颜色：正文黑、标题深蓝。"""
        import tempfile

        from PIL import Image

        card = "# 标题\n\n这是一段正文说明文字。\n"
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "b.png")
            render_text_to_image(card, out)
            with Image.open(out).convert("RGB") as img:
                px = img.load()
                w, h = img.size
                black = sum(
                    1 for y in range(h) for x in range(w) if max(px[x, y]) <= 40
                )
                blue = sum(
                    1
                    for y in range(h)
                    for x in range(w)
                    if px[x, y][2] >= 100
                    and px[x, y][2] - px[x, y][0] >= 40
                    and px[x, y][1] <= 200
                )
        self.assertGreater(black, 200, "正文应为黑色")
        self.assertGreater(blue, 200, "标题应为深蓝色")
        # 两者不能是同一种颜色
        self.assertNotEqual((0, 0, 0), (30, 58, 138), "标题与正文颜色必须区分开")

    def test_contrast_against_white_bg(self):
        """黑色正文与深蓝标题在白底上的对比度必须足够高（WCAG >= 4.5）。"""
        def lum(c):
            def ch(v):
                v /= 255.0
                return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

            r, g, b = c
            return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)

        def ratio(a, b):
            la, lb = lum(a), lum(b)
            hi, lo = max(la, lb), min(la, lb)
            return (hi + 0.05) / (lo + 0.05)

        self.assertGreaterEqual(ratio(self.INK, self.BG), 4.5)
        # 深蓝标题在白底上同样要清晰
        self.assertGreaterEqual(ratio(self.HEAD, self.BG), 4.5)

    def test_analysis_block_rendered_at_bottom(self):
        import tempfile

        from PIL import Image

        card = "# 战绩\n\n| 结果 | 英雄 |\n|---|---|\n| ✅ | 灰烬之灵 |\n" + "\n## 综合分析\n\n中期节奏很好\n"
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "a.png")
            render_text_to_image(card, out)
            with Image.open(out).convert("RGB") as img:
                w, h = img.size
                # 扫描浅暖色分析块底纹（#fff8ef）
                band_ys = [
                    y
                    for y in range(h)
                    if self._is_band(img.getpixel((w // 2, y)))
                ]
            self.assertTrue(band_ys, "未渲染出综合分析区块")
            # 区块必须位于图片下半部分
            self.assertGreater(min(band_ys), h // 2)
            # 数据区（顶部附近）不应是分析块底色
            with Image.open(out).convert("RGB") as img:
                top = img.getpixel((w // 2, 4))
            self.assertFalse(self._is_band(top))

    @staticmethod
    def _is_band(px) -> bool:
        r, g, b = px
        return r >= 246 and 232 <= g <= 253 and 220 <= b <= 250 and (r - b) >= 5

    @staticmethod
    def _is_table_fill(px) -> bool:
        """表头 #eef2f7 与斑马纹 #fafbfc 两种区块填充色。"""
        r, g, b = px
        return (225 <= r <= 242 and 232 <= g <= 246 and b >= r) or (
            246 <= r <= 253 and 248 <= g <= 254 and b >= r
        )

    def test_no_overlap_between_table_and_analysis(self):
        import tempfile

        from PIL import Image

        rows = "\n".join(f"| ✅ | 英雄{i} | {i}/1/2 |" for i in range(8))
        card = (
            "# 战绩\n\n| 结果 | 英雄 | KDA |\n|---|---|---|\n"
            + rows
            + "\n\n## 综合分析\n\n总结文字\n"
        )
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "n.png")
            render_text_to_image(card, out)
            with Image.open(out).convert("RGB") as img:
                w, h = img.size
                px = img.load()
                band_ys = [y for y in range(h) if self._is_band(px[w // 2, y])]
                # 表格行的填充色（表头 #eef2f7 / 斑马纹 #fafbfc）出现在图片右侧
                fill_ys = [y for y in range(h) if self._is_table_fill(px[w - 40, y])]
            self.assertTrue(band_ys, "未渲染出综合分析区块")
            self.assertTrue(fill_ys, "未渲染出表格填充色")
            self.assertGreater(
                band_ys[0],
                max(fill_ys),
                "综合分析块与表格重叠",
            )


class TestNetworkProductGuards(unittest.TestCase):
    """网络 t2i 产物的内容级校验。

    端点内的浏览器若拿不到 marked，模板会退化成把 Markdown 当纯文本塞进
    一个 div：页面高度塌缩、卡片尾部（含「综合分析」）被裁掉。产物本身是
    合法图片，只能靠版式判据识别，因此这道校验必须能区分两者。

    判据改用「高度过矮」（见 :func:`card_renderer._looks_degenerate`）：
    原先靠浅暖色底纹 (#fff8ef) 占比识别，但底纹已可配置，颜色不再能作为
    退化信号。退化本质是全页未渲染（marked 未执行），表现为高度塌缩，
    因此高度判据既与配色解耦，又保留对「疑似模板脚本未加载」产物的回退。
    """

    # 退化阈值（见 card_renderer._DEGENERATE_MAX_HEIGHT）。正常卡片 900~1600px，
    # 退化产物塌缩到 ~120px 以下。
    _DEGENERATE_MAX_HEIGHT = 200

    # 白底主题：页面底色 #ffffff
    _PAGE_BG = (255, 255, 255)

    @classmethod
    def _render_like(cls, height, *, width=400):
        """造一张高度为 height 的纯白图（模拟退化/正常产物）。"""
        from PIL import Image as PILImage

        return PILImage.new("RGB", (width, height), cls._PAGE_BG)

    def _save(self, img, d, name):
        path = os.path.join(d, name)
        img.save(path)
        return path

    def test_normal_card_not_degenerate(self):
        import tempfile

        from astrbot_plugin_dota2assistant_plus.core import card_renderer as cr

        with tempfile.TemporaryDirectory() as d:
            path = self._save(self._render_like(1200), d, "ok.png")
            self.assertFalse(
                cr._looks_degenerate(path), "正常高度卡片不能是退化产物"
            )

    def test_degenerate_product_rejected(self):
        """模拟 CDN 失败后的退化产物：高度塌缩。"""
        import tempfile

        from astrbot_plugin_dota2assistant_plus.core import card_renderer as cr

        with tempfile.TemporaryDirectory() as d:
            path = self._save(self._render_like(120), d, "deg.png")
            self.assertTrue(
                cr._looks_degenerate(path),
                "高度过矮的退化产物必须被识别出来",
            )

    def test_blank_product_rejected(self):
        import tempfile

        from astrbot_plugin_dota2assistant_plus.core import card_renderer as cr

        with tempfile.TemporaryDirectory() as d:
            path = self._save(
                self._render_like(1200, width=400), d, "blank.png"
            )
            self.assertFalse(cr._looks_degenerate(path))

    def test_guard_does_not_crash_on_broken_file(self):
        from astrbot_plugin_dota2assistant_plus.core import card_renderer as cr

        # 判不出来时应放行（返回 False），避免误伤正常渲染
        self.assertFalse(cr._looks_degenerate("/nonexistent/x.jpg"))

    def test_template_has_no_remote_script(self):
        """模板不得再依赖外部 CDN：这是本次线上问题的根因。"""
        from pathlib import Path

        from astrbot_plugin_dota2assistant_plus.core import card_renderer as cr

        tmpl_path = Path(cr.__file__).resolve().parent.parent / "assets" / "card.html"
        html = tmpl_path.read_text(encoding="utf-8")
        self.assertNotIn(
            "cdn.jsdelivr.net",
            html,
            "card.html 仍引用外部 CDN，渲染结果会随端点网络状况波动",
        )
        self.assertIn("marked", html, "card.html 应内联 marked")
        # 内联脚本不能含 Jinja2 标记，否则模板渲染会直接报错
        start = html.find("/* marked inlined */")
        self.assertGreater(start, -1)
        end = html.find("</script>", start)
        inline = html[start:end]
        for token in ("{{", "{%", "{#"):
            self.assertNotIn(token, inline, f"内联脚本含 Jinja2 标记 {token}")

    def test_fallback_branch_keeps_line_breaks(self):
        """退化分支必须保留换行，避免再次出现高度塌缩。"""
        from pathlib import Path

        from astrbot_plugin_dota2assistant_plus.core import card_renderer as cr

        tmpl_path = Path(cr.__file__).resolve().parent.parent / "assets" / "card.html"
        html = tmpl_path.read_text(encoding="utf-8")
        self.assertIn('whiteSpace = "pre-wrap"', html)


class TestHeroNameTranslation(unittest.TestCase):
    """英雄英文名 -> 官方中文名。"""

    def test_english_to_official_chinese(self):
        from astrbot_plugin_dota2assistant_plus.core.hero_names import to_chinese

        cases = {
            "Ember Spirit": "灰烬之灵",
            "ember_spirit": "灰烬之灵",
            "npc_dota_hero_ember_spirit": "灰烬之灵",
            "Anti-Mage": "敌法师",
            "antimage": "敌法师",
            "Windrunner": "风行者",
            "Windranger": "风行者",
            "Necrolyte": "瘟疫法师",
            "Necrophos": "瘟疫法师",
            "Outworld Destroyer": "殁境神蚀者",
            "Shadow Fiend": "影魔",
            "Sniper": "狙击手",
        }
        for en, zh in cases.items():
            with self.subTest(en=en):
                self.assertEqual(to_chinese(en), zh)

    def test_chinese_passthrough(self):
        from astrbot_plugin_dota2assistant_plus.core.hero_names import to_chinese

        self.assertEqual(to_chinese("灰烬之灵"), "灰烬之灵")
        self.assertEqual(to_chinese("敌法"), "敌法")

    def test_unknown_kept(self):
        from astrbot_plugin_dota2assistant_plus.core.hero_names import to_chinese

        self.assertEqual(to_chinese("Hero#35"), "Hero#35")

    def test_match_hero_by_many_names(self):
        from astrbot_plugin_dota2assistant_plus.core.hero_names import match_hero
        from astrbot_plugin_dota2assistant_plus.core.models import HeroInfo

        heroes = [
            HeroInfo(id=106, name="npc_dota_hero_ember_spirit",
                     localized_name="灰烬之灵", primary_attr="agi", attack_type="Melee"),
            HeroInfo(id=1, name="npc_dota_hero_antimage",
                     localized_name="敌法师", primary_attr="agi", attack_type="Melee"),
        ]
        for query in ("Ember Spirit", "ember_spirit", "灰烬之灵", "敌法", "Anti-Mage"):
            with self.subTest(query=query):
                self.assertIsNotNone(match_hero(heroes, query))
        self.assertIsNone(match_hero(heroes, "不存在的英雄"))


class TestSummarizerClean(unittest.TestCase):
    """模型输出清洗：标题与「总结：」前缀要剥掉，避免图片里出现双重标题。"""

    def test_strips_markdown_heading(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _clean

        self.assertEqual(_clean("# 总结\n\n中期节奏很好"), "中期节奏很好")

    def test_strips_colon_prefix(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _clean

        for raw in ("总结：夜魇中期更强", "赛后总结:夜魇中期更强", "综合分析：夜魇中期更强"):
            with self.subTest(raw=raw):
                self.assertEqual(_clean(raw), "夜魇中期更强")

    def test_keeps_plain_text(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _clean

        self.assertEqual(_clean("  天辉前期压制力不足  "), "天辉前期压制力不足")
        self.assertEqual(_clean(""), "")
        self.assertEqual(_clean(None), "")


class FakeProvider:
    """最小可用的 Provider 替身。"""

    def __init__(self, text="", error=None, delay=0.0, model_name="fake-model"):
        self._text = text
        self._error = error
        self._delay = delay
        self.model_name = model_name
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(completion_text=self._text)


class FakeSummaryContext:
    def __init__(self, by_id=None, using=None, all_providers=None):
        self._by_id = by_id or {}
        self._using = using
        self._all = all_providers or []

    def get_provider_by_id(self, provider_id):
        return self._by_id.get(provider_id)

    async def get_using_provider_async(self, umo=None):
        return self._using

    def get_all_providers(self):
        return self._all


class TestSummarizer(unittest.TestCase):
    """赛后总结：由插件自己调用本机对话大模型生成。"""

    CARD = "# 比赛 #8987081176\n\n| 结果 | 英雄 | KDA |\n|---|---|---|\n| ✅ | 狙击手 | 12/1/9 |"

    def _host(self, context=None, **over):
        from astrbot_plugin_dota2assistant_plus.main import Dota2AssistantPlugin

        host = object.__new__(Dota2AssistantPlugin)
        host.enable_llm_summary = True
        host.summary_provider_id = ""
        host.summary_timeout = 30
        host.summary_max_chars = 600
        host.context = context
        for key, value in over.items():
            setattr(host, key, value)
        return host

    def _run(self, host, cards=None):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import summarize

        return asyncio.run(summarize(host, cards if cards is not None else [self.CARD]))

    def test_item_icon_urls_do_not_waste_prompt_budget(self):
        """装备图标 URL 不该挤占 4000 字的输入上限，也不该出现在 prompt 里。

        卡片正文从 ~1.7k 字被 URL 撑到 ~9.4k 字；不先降级的话，
        模型只能看到前 1/5 的比赛数据。
        """
        prov = FakeProvider(text="点评")
        host = self._host(FakeSummaryContext(using=prov))

        card = (
            "## 天辉\n\n### ✅ 敌法师 · Lv.19\n"
            "装备：![黑皇杖](https://cdn.cloudflare.steamstatic.com/apps/dota2/"
            "images/dota_react/items/black_king_bar.png) "
            "![相位鞋](https://cdn.cloudflare.steamstatic.com/apps/dota2/"
            "images/dota_react/items/phase_boots.png)\n"
        ) * 8
        self._run(host, [card])

        sent = prov.calls[0]["prompt"]
        self.assertNotIn("cdn.cloudflare", sent)
        self.assertNotIn("![" , sent)
        # 物品名保留，点评才能看出出了什么装备
        self.assertIn("黑皇杖", sent)

    def test_generates_summary_from_default_model(self):
        """英雄名由代码加粗后返回 —— 不依赖模型自觉（见 _match_terms）。

        这张夹具卡片是旧格式（没有 ``### ![英雄](立绘)`` 头像行），
        英雄名只出现在表格的「英雄」列里，因此走的是按官方英雄名表
        认名字的兜底路径。
        """
        prov = FakeProvider(text="夜魇中期团战占优，狙击手发育顺畅。")
        host = self._host(FakeSummaryContext(using=prov))
        self.assertEqual(self._run(host), "夜魇中期团战占优，**狙击手**发育顺畅。")
        self.assertEqual(len(prov.calls), 1)

    def test_disabled_by_config(self):
        prov = FakeProvider(text="不该被调用")
        host = self._host(FakeSummaryContext(using=prov), enable_llm_summary=False)
        self.assertEqual(self._run(host), "")
        self.assertEqual(prov.calls, [])

    def test_empty_cards_skip_model(self):
        prov = FakeProvider(text="不该被调用")
        host = self._host(FakeSummaryContext(using=prov))
        self.assertEqual(self._run(host, []), "")
        self.assertEqual(self._run(host, ["   "]), "")
        self.assertEqual(prov.calls, [])

    def test_no_provider_returns_empty(self):
        host = self._host(FakeSummaryContext())
        self.assertEqual(self._run(host), "")

    def test_provider_error_returns_empty(self):
        prov = FakeProvider(error=RuntimeError("503 no healthy account"))
        host = self._host(FakeSummaryContext(using=prov))
        self.assertEqual(self._run(host), "")

    def test_timeout_returns_empty(self):
        prov = FakeProvider(text="太慢了", delay=0.5)
        host = self._host(FakeSummaryContext(using=prov), summary_timeout=0.05)
        self.assertEqual(self._run(host), "")

    def test_empty_completion_returns_empty(self):
        host = self._host(FakeSummaryContext(using=FakeProvider(text="   ")))
        self.assertEqual(self._run(host), "")

    def test_truncated_to_max_chars(self):
        host = self._host(
            FakeSummaryContext(using=FakeProvider(text="字" * 2000)),
            summary_max_chars=100,
        )
        out = self._run(host)
        self.assertEqual(len(out), 101)  # 100 字 + 省略号
        self.assertTrue(out.endswith("…"))

    def test_prompt_contains_card_data(self):
        prov = FakeProvider(text="点评")
        host = self._host(FakeSummaryContext(using=prov))
        self._run(host)
        prompt = prov.calls[0]["prompt"]
        self.assertIn("8987081176", prompt)
        self.assertIn("狙击手", prompt)
        # 不给工具，避免又触发一轮工具调用
        self.assertNotIn("func_tool", prov.calls[0])

    def test_long_card_is_clipped_before_prompt(self):
        from astrbot_plugin_dota2assistant_plus.core.summarizer import _MAX_INPUT_CHARS

        prov = FakeProvider(text="点评")
        host = self._host(FakeSummaryContext(using=prov))
        self._run(host, ["长" * (_MAX_INPUT_CHARS * 2)])
        self.assertLessEqual(len(prov.calls[0]["prompt"]), _MAX_INPUT_CHARS + 200)

    def test_configured_provider_wins(self):
        preferred = FakeProvider(text="指定模型的点评")
        fallback = FakeProvider(text="默认模型的点评")
        ctx = FakeSummaryContext(by_id={"GLM/GLM-4.7-Flash": preferred}, using=fallback)
        host = self._host(ctx, summary_provider_id="GLM/GLM-4.7-Flash")
        self.assertEqual(self._run(host), "指定模型的点评")
        self.assertEqual(fallback.calls, [])

    def test_missing_configured_provider_falls_back(self):
        fallback = FakeProvider(text="默认模型的点评")
        ctx = FakeSummaryContext(by_id={}, using=fallback)
        host = self._host(ctx, summary_provider_id="不存在的模型")
        self.assertEqual(self._run(host), "默认模型的点评")

    def test_falls_back_to_first_available_provider(self):
        only = FakeProvider(text="唯一可用模型的点评")
        ctx = FakeSummaryContext(by_id={}, using=None, all_providers=[only])
        host = self._host(ctx)
        self.assertEqual(self._run(host), "唯一可用模型的点评")

    def test_broken_context_returns_empty(self):
        class Exploding:
            def get_provider_by_id(self, provider_id):
                raise RuntimeError("boom")

            async def get_using_provider_async(self, umo=None):
                raise RuntimeError("boom")

            def get_all_providers(self):
                raise RuntimeError("boom")

        host = self._host(Exploding())
        self.assertEqual(self._run(host), "")

    def test_no_context_returns_empty(self):
        self.assertEqual(self._run(self._host(None)), "")


class TestSummaryConfigDefaults(unittest.TestCase):
    """赛后总结的开关与超时默认值。

    背景（线上真实故障）：插件改名后 enable_llm_summary 被关成 false、
    且 summary_timeout 仍是 30 秒，而配置里选的 DeepSeek-R1 是推理模型、
    实测写三点总结要 88 秒 —— 于是每次必定超时，总结静默退回成
    「主对话模型的回复文字」，而工具返回给主模型的提示又只让它
    「补充 1-3 句」，用户最终看到的总结因此只有定性判断、没有数据。
    """

    @staticmethod
    def _schema():
        import json
        from pathlib import Path

        # conftest 注册的是命名空间包，没有 __file__；
        # 用真实子模块的 __file__ 反推包目录（它一定在包内 core/ 下）。
        from astrbot_plugin_dota2assistant_plus.core import summarizer

        pkg_dir = Path(summarizer.__file__).resolve().parent.parent
        return json.loads((pkg_dir / "_conf_schema.json").read_text(encoding="utf-8"))

    def test_schema_defaults_keep_summary_working(self):
        schema = self._schema()

        self.assertIs(schema["enable_llm_summary"]["default"], True)
        # 30 秒装不下推理模型，默认必须是 180
        self.assertEqual(schema["summary_timeout"]["default"], 180)

    def test_provider_field_renders_as_model_picker(self):
        """summary_provider_id 要用 AstrBot 的模型下拉框，方便手动指定。"""
        schema = self._schema()
        field = schema["summary_provider_id"]
        self.assertEqual(field["_special"], "select_provider")
        self.assertEqual(field["default"], "")
        # 两个字段都要有说明，避免用户再踩同样的坑
        self.assertTrue(field.get("hint"))
        self.assertTrue(schema["summary_timeout"].get("hint"))

    def test_hint_warns_against_reasoning_models(self):
        """说明里要提醒别选推理模型 —— 正文会返回空串。"""
        hint = self._schema()["summary_provider_id"]["hint"]
        self.assertIn("推理", hint)
        self.assertIn("R1", hint)
        # deepseek-v4-flash 名字带 flash 但也是推理模型，必须点名警示
        self.assertIn("deepseek-v4-flash", hint)

    def test_hint_recommends_a_verified_model(self):
        """要给出一个实测可用的具体模型，用户才知道该选什么。"""
        hint = self._schema()["summary_provider_id"]["hint"]
        self.assertIn("GLM/GLM-4-Flash-250414", hint)


class TestSummarizeTimeoutDefault(unittest.TestCase):
    """summarize() 的超时默认值必须与 schema 一致，且容得下推理模型。"""

    def test_getattr_defaults_are_180(self):
        import inspect

        from astrbot_plugin_dota2assistant_plus.core import summarizer

        src = inspect.getsource(summarizer.summarize)
        self.assertIn('"summary_timeout", 180', src)
        # 500 字：提示词要求只点评每方最突出/最落后的 1-2 人，已够写清
        self.assertIn('"summary_max_chars", 500', src)

    def test_does_not_pass_max_tokens_as_kwarg(self):
        """AstrBot 会丢弃未知 kwargs，传 max_tokens 是无效的，不应再传。

        ``_prepare_chat_payload`` 只把 ``messages`` 和 ``model`` 放进请求体，
        实测传入 max_tokens 后最终 payload 里并没有它 —— 想加预算只能改
        AstrBot 全局的 custom_extra_body，插件不该动那个。
        因此规避推理模型只能靠「选对模型」，不能靠加 max_tokens。
        """
        import inspect

        from astrbot_plugin_dota2assistant_plus.core import summarizer

        src = inspect.getsource(summarizer.summarize)
        self.assertNotIn("max_tokens=", src)

    def test_slow_model_completes_under_new_default(self):
        """实测 R1 需 88 秒；新默认 180 秒必须能等到结果。"""
        import asyncio

        from astrbot_plugin_dota2assistant_plus.core import summarizer

        class SlowProvider:
            model_name = "slow-r1"

            async def text_chat(self, **kw):
                from types import SimpleNamespace

                await asyncio.sleep(0.05)  # 缩短等待，只验证不会被超时打断
                return SimpleNamespace(completion_text="1. 天辉\n2. 夜魇\n3. 总结")

        class Host:
            enable_llm_summary = True
            summary_provider_id = ""
            summary_timeout = 180
            summary_max_chars = 900
            context = None

        async def fake_resolve(host, event):
            return SlowProvider()

        from unittest.mock import patch

        with patch.object(summarizer, "_resolve_provider", fake_resolve):
            out = asyncio.run(summarizer.summarize(Host(), ["# 比赛数据"], None))
        self.assertTrue(out)


class TestHintTellsAgentNotToFakeAnalysis(unittest.TestCase):
    """工具返回给主模型的提示，不能再要求它写它拿不到数据的分析。"""

    def test_hint_no_longer_requests_player_stats_summary(self):
        from astrbot_plugin_dota2assistant_plus.tools.delivery import _HINT

        # 旧文案让主模型「补充 1-3 句分析」，主模型据此抱怨拿不到数据
        self.assertNotIn("请仅补充 1-3 句", _HINT)
        # 新文案要说明总结已由插件生成
        self.assertIn("插件自动生成", _HINT)
        self.assertIn("不要另写逐玩家的数据点评", _HINT)

    def test_hint_still_forbids_self_rendering_images(self):
        from astrbot_plugin_dota2assistant_plus.tools.delivery import _HINT

        self.assertIn("切勿自己生成图片", _HINT)
        self.assertIn("astrbot_execute_python", _HINT)


class TestHookSummaryPriority(unittest.TestCase):
    """钩子取总结的优先级：模型总结 > Agent 回复；两者都无则照样出图。"""

    CARD = "# 比赛数据\n\n| 结果 | 英雄 |\n|---|---|\n| ✅ | 狙击手 |"
    AGENT_TEXT = "Agent 自己写的点评"

    def _plugin(self):
        from astrbot_plugin_dota2assistant_plus.main import Dota2AssistantPlugin

        plugin = object.__new__(Dota2AssistantPlugin)
        plugin.enable_llm_summary = True
        plugin.summary_provider_id = ""
        plugin.summary_timeout = 30
        plugin.summary_max_chars = 600
        plugin.context = None
        plugin.card_theme = {}
        # 录像解析相关属性：_decorate_dota_card 会读它们；本类用的卡片不是
        # 「比赛详情」卡（_card_match_id 返回 0），不会进录像解析分支，
        # 但属性必须存在，否则 getattr 落空。
        plugin.enable_replay_summary = True
        plugin.prefer_local_render = True
        return plugin

    def _event(self, agent_text=AGENT_TEXT):
        from astrbot_plugin_dota2assistant_plus.tools import delivery

        ev = FakeEvent("糖糖 分析比赛 8987081176")
        delivery.stash_card(ev, self.CARD)
        if agent_text:
            # 真实 event.get_result() 返回的 MessageEventResult 带 .chain
            ev.set_result(SimpleNamespace(chain=[Plain(agent_text)]))
        return ev

    def _run_hook(self, plugin, ev, summary):
        """跑钩子，返回 (render_card 收到的 markdown, 事件结果)。"""
        seen = {}

        async def fake_render(markdown, host=None, theme=None, prefer_local=False):
            seen["markdown"] = markdown
            seen["theme"] = theme
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "card.png")
                from PIL import Image as PILImage

                PILImage.new("RGB", (60, 60), "white").save(path)
                seen["path"] = path
                return path

        async def fake_summarize(host, cards, event=None, focus_hero=""):
            # 真实 summarize 现在多一个 focus_hero 关键字参数（点名英雄时传入），
            # 测试替身必须同签名，否则钩子调用会 TypeError。
            if isinstance(summary, Exception):
                raise summary
            return summary

        target = "astrbot_plugin_dota2assistant_plus.core.card_renderer.render_card"
        smod = "astrbot_plugin_dota2assistant_plus.core.summarizer.summarize"
        with patch(target, new=AsyncMock(side_effect=fake_render)), \
                patch(smod, new=AsyncMock(side_effect=fake_summarize)):
            asyncio.run(plugin._decorate_dota_card(ev))
        return seen.get("markdown", ""), ev.get_result()

    def test_model_summary_wins(self):
        md, result = self._run_hook(self._plugin(), self._event(), "模型写的点评")
        self.assertIn("模型写的点评", md)
        self.assertNotIn(self.AGENT_TEXT, md)
        # 总结必须落在图片最下方
        self.assertTrue(md.rstrip().endswith("模型写的点评"))
        self.assertEqual(result[0], "chain")

    def test_falls_back_to_agent_text(self):
        md, _ = self._run_hook(self._plugin(), self._event(), "")
        self.assertIn(self.AGENT_TEXT, md)
        self.assertTrue(md.rstrip().endswith(self.AGENT_TEXT))

    def test_summarizer_crash_still_falls_back(self):
        md, result = self._run_hook(
            self._plugin(), self._event(), RuntimeError("provider exploded")
        )
        self.assertIn(self.AGENT_TEXT, md)
        self.assertEqual(result[0], "chain")

    def test_no_summary_at_all_still_renders_card(self):
        md, result = self._run_hook(self._plugin(), self._event(agent_text=""), "")
        self.assertIn("比赛数据", md)
        self.assertNotIn("综合分析", md)
        self.assertEqual(result[0], "chain")

    def test_hook_ignores_unrelated_turns(self):
        """没有暂存卡片时，钩子不得改写消息。"""
        plugin = self._plugin()
        ev = FakeEvent("糖糖 你好")
        untouched = SimpleNamespace(chain=[Plain("你好呀")])
        ev.set_result(untouched)
        asyncio.run(plugin._decorate_dota_card(ev))
        self.assertIs(ev.get_result(), untouched)


class TestToolDescriptionTriggers(unittest.TestCase):
    """工具描述必须能引导模型正确命中，并排除「体育赛事/赔率」误判。

    背景（真实事故）：群聊里同时聊到「赔率/盘口」时，用户发
    「丹丹 分析比赛 8987081176」，模型把它当成足球赛果预测而拒答，
    完全没有调用 dota_match_detail。工具描述里显式排除该误判后应回归。
    """

    def _desc(self, cls_name: str) -> str:
        from astrbot_plugin_dota2assistant_plus.tools import (
            live_tool,
            match_tool,
            pro_tool,
        )

        cls = {
            "match": match_tool.DotaMatchTool,
            "pro": pro_tool.DotaProTool,
            "live": live_tool.DotaLiveTool,
        }[cls_name]
        return cls.description

    def test_match_tool_mentions_dota2_explicitly(self):
        """必须写明是 Dota2 对局，避免被当成泛指的比赛。"""
        desc = self._desc("match")
        self.assertIn("Dota2", desc)
        for kw in ("分析比赛", "查询比赛"):
            self.assertIn(kw, desc)

    def test_match_tool_rules_out_sports_betting_misjudgement(self):
        """必须排除「体育赛事 / 赔率 / 盘口 / 投注」这一误判路径。"""
        desc = self._desc("match")
        for kw in ("赔率", "盘口", "投注", "足球"):
            self.assertIn(kw, desc)
        self.assertIn("无关", desc)

    def test_match_tool_states_number_pattern(self):
        """必须说明编号形态，帮助模型把数字串对应到 match_id。"""
        desc = self._desc("match")
        self.assertIn("数字", desc)

    def test_match_id_param_description_mentions_digits(self):
        from astrbot_plugin_dota2assistant_plus.tools.match_tool import DotaMatchTool

        # 类属性上是 pydantic/dataclass FieldInfo，取实例才是真实 dict
        param = DotaMatchTool(client=None).parameters["properties"]["match_id"]["description"]
        self.assertIn("数字", param)
        self.assertIn("8987081176", param)

    def test_other_match_tools_also_rule_out_sports(self):
        """职业比赛/实时对局同样要排除体育赛事误判。"""
        for key in ("pro", "live"):
            with self.subTest(tool=key):
                desc = self._desc(key)
                self.assertIn("Dota2", desc)
                self.assertIn("赔率", desc)

    def test_descriptions_stay_compact(self):
        """描述不能无限膨胀：过长会挤占上下文预算。"""
        for key in ("match", "pro", "live"):
            with self.subTest(tool=key):
                self.assertLess(len(self._desc(key)), 600)

    def test_every_tool_carries_the_wake_rule(self):
        """9 个工具都要带唤醒条件，否则会被其他游戏/体育话题误触发。"""
        from astrbot_plugin_dota2assistant_plus.tools import (
            hero_build_tool, hero_tool, item_tool, live_tool, match_tool,
            my_profile_tool, player_tool, pro_tool,
        )

        names = [
            match_tool.DotaMatchTool, pro_tool.DotaProTool, live_tool.DotaLiveTool,
            my_profile_tool.DotaMyProfileTool, player_tool.DotaPlayerTool,
            hero_tool.DotaHeroTool, hero_tool.DotaHeroListTool,
            item_tool.DotaItemTool, hero_build_tool.DotaHeroBuildTool,
        ]
        self.assertEqual(len(names), 9)
        for cls in names:
            with self.subTest(tool=cls.name):
                desc = cls.description
                self.assertIn("【唤醒】", desc)
                self.assertIn("【范围】", desc)
                # 三个唤醒信号都要写明
                self.assertIn("刀塔", desc)
                self.assertIn("比赛编号", desc)
                self.assertIn("英雄名", desc)
                # 编号形态要写清（8~10 位），否则模型认不出裸编号
                self.assertIn("8~10 位", desc)

    def test_wake_rule_lists_game_markers(self):
        """唤醒信号必须列出可识别的刀塔标识。"""
        from astrbot_plugin_dota2assistant_plus.tools.triggers import WAKE_RULE

        for marker in ("刀塔", "刀塔2", "dota", "dota2"):
            with self.subTest(marker=marker):
                self.assertIn(marker, WAKE_RULE)

    def test_wake_rule_requires_a_signal(self):
        """没有信号时不能调用，并要反问——这是与「其他游戏插件」的分界。"""
        from astrbot_plugin_dota2assistant_plus.tools.triggers import WAKE_RULE

        self.assertIn("不要调用", WAKE_RULE)
        self.assertIn("反问", WAKE_RULE)

    def test_scope_rule_excludes_other_games(self):
        """必须显式排除其他游戏，避免未来接入同类插件时互相抢话。"""
        from astrbot_plugin_dota2assistant_plus.tools.triggers import SCOPE_RULE

        for other in ("王者荣耀", "英雄联盟", "足球"):
            with self.subTest(other=other):
                self.assertIn(other, SCOPE_RULE)
        self.assertIn("无关", SCOPE_RULE)

    def test_binding_section_requires_game_name(self):
        """绑定说明必须写明自然语言触发要带「刀塔」。"""
        from pathlib import Path

        readme = Path(__file__).resolve().parent.parent / "docs" / "README.md"
        text = readme.read_text(encoding="utf-8")
        self.assertIn("查一下我的刀塔战绩", text)
        # 触发规则说明（自然语言要带「刀塔」）必须还在，措辞可调
        self.assertIn("刀塔", text)
        # 旧措辞会让用户照着说「查一下我的战绩」而唤不醒插件
        self.assertNotIn('绑定后可以说"查一下我的战绩"', text)

    def test_readme_states_upstream_origin(self):
        """文末必须说明来源项目与原文作者。"""
        from pathlib import Path

        readme = Path(__file__).resolve().parent.parent / "docs" / "README.md"
        text = readme.read_text(encoding="utf-8")
        self.assertIn("## 致谢", text)
        self.assertIn("yarizm/astrbot_plugin_dota2assistant", text)
        self.assertIn(
            "https://github.com/yarizm/astrbot_plugin_dota2assistant", text)

    def test_root_and_docs_readme_stay_in_sync(self):
        """根目录 README（插件详情页显示的内容）与 docs/README.md 必须一致。

        两份曾经分叉：根目录那份停留在旧版本，功能表里还是不带「刀塔」的
        示例，用户在插件详情页看到的就是过期文档。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        self.assertEqual(
            (root / "README.md").read_text(encoding="utf-8"),
            (root / "docs" / "README.md").read_text(encoding="utf-8"),
        )

    def test_documented_example_uses_game_name(self):
        """README 的自然语言示例要带游戏名，用户照着说才唤得醒。"""
        from pathlib import Path

        readme = Path(__file__).resolve().parent.parent / "docs" / "README.md"
        text = readme.read_text(encoding="utf-8")
        self.assertIn("查询我的最近一场刀塔比赛", text)
        self.assertIn("唤醒规则", text)


if __name__ == "__main__":
    unittest.main()
