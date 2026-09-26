"""图标/立绘本地缓存层与英雄立绘层的单元测试。

这一层是「t2i 产物每次都有效」的关键：远端端点里的浏览器访问不了 Valve CDN，
所以图标必须由插件自己下载、缓存、再内联成 data URI。这里的用例都**不联网**：
下载函数被替换成假实现，验证的是「缓存命中/未命中」「URL 归一化」
「Markdown 内联」这些纯逻辑。
"""

import base64
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_dota2assistant_plus.core import hero_icons, icon_cache

ITEM_URL = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/items/"
    "black_king_bar.png"
)
VERT_URL = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/heroes/antimage_vert.jpg"
)
REACT_URL = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/"
    "dawnbreaker.png"
)


def _png_bytes(size=(16, 12), color=(200, 40, 40, 255)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


def _jpeg_bytes(size=(235, 272), color=(30, 90, 200)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


class TestUrlClassification(unittest.TestCase):
    def test_recognises_item_and_hero_urls(self):
        for url in (ITEM_URL, VERT_URL, REACT_URL):
            with self.subTest(url=url):
                self.assertTrue(icon_cache.is_icon_url(url))

    def test_rejects_unrelated_urls(self):
        for url in ("", "https://example.com/a.png", "https://cdn.com/x/y.svg"):
            with self.subTest(url=url):
                self.assertFalse(icon_cache.is_icon_url(url))

    def test_cache_relpath_groups_by_kind(self):
        # 装备图标保留原始扩展名；英雄立绘统一存成 heroes/<key>.png
        self.assertEqual(
            icon_cache.cache_relpath(ITEM_URL).as_posix(), "items/black_king_bar.png")
        self.assertEqual(
            icon_cache.cache_relpath(VERT_URL).as_posix(), "heroes/antimage.png")

    def test_hero_asset_key_strips_suffixes(self):
        self.assertEqual(icon_cache.hero_asset_key(VERT_URL), "antimage")
        self.assertEqual(icon_cache.hero_asset_key(REACT_URL), "dawnbreaker")

    def test_react_is_a_fallback_for_missing_vertical_art(self):
        """新英雄没有 legacy 竖版图，候选里必须带横版兜底（用同一个 key）。"""
        candidates = icon_cache.candidate_urls(VERT_URL)
        self.assertEqual(candidates[0], VERT_URL)
        self.assertIn(
            "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/"
            "heroes/antimage.png",
            candidates)
        # 横版本身就是最终形态，不该再递归出别的候选
        self.assertEqual(icon_cache.candidate_urls(REACT_URL), [REACT_URL])

    def test_both_sources_share_one_cache_file(self):
        """同一英雄的竖版与横版指向同一个缓存文件，不重复占空间。"""
        react_antimage = REACT_URL.replace("dawnbreaker", "antimage")
        self.assertEqual(icon_cache.cache_relpath(VERT_URL),
                         icon_cache.cache_relpath(react_antimage))


class TestMarkdownInlining(unittest.TestCase):
    def test_image_urls_preserve_order_and_dedupe(self):
        md = f"![a]({ITEM_URL}) ![b]({VERT_URL}) ![a2]({ITEM_URL})"
        self.assertEqual(icon_cache.image_urls(md), [ITEM_URL, VERT_URL])

    def test_image_urls_on_empty(self):
        self.assertEqual(icon_cache.image_urls(""), [])
        self.assertEqual(icon_cache.image_urls(None), [])

    def test_inline_replaces_only_cached_urls(self):
        icon_cache.reset_caches()
        with patch.object(icon_cache, "data_uri",
                          side_effect=lambda url: "data:image/png;base64,AAA"
                          if url == ITEM_URL else ""):
            out = icon_cache.inline_data_uris(f"装备：![黑皇杖]({ITEM_URL})")
        self.assertIn("data:image/png;base64,AAA", out)
        self.assertNotIn(ITEM_URL, out)
        # alt 必须原样保留：拿不到真图时它就是降级显示的中文名
        self.assertIn("![黑皇杖]", out)

    def test_uncached_url_survives_unchanged(self):
        icon_cache.reset_caches()
        with patch.object(icon_cache, "data_uri", return_value=""):
            out = icon_cache.inline_data_uris(f"![黑皇杖]({ITEM_URL})")
        self.assertEqual(out, f"![黑皇杖]({ITEM_URL})")

    def test_data_uri_is_base64_of_file(self):
        icon_cache.reset_caches()
        tmp = Path("/tmp") / "d2_icon_test"
        (tmp / "items").mkdir(parents=True, exist_ok=True)
        raw = _png_bytes()
        (tmp / "items" / "black_king_bar.png").write_bytes(raw)
        with patch.object(icon_cache, "cache_dir", return_value=tmp):
            icon_cache.reset_caches()
            uri = icon_cache.data_uri(ITEM_URL)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(uri.split(",", 1)[1]), raw)

    def test_local_path_returns_none_when_missing(self):
        tmp = Path("/tmp") / "d2_icon_empty"
        (tmp / "items").mkdir(parents=True, exist_ok=True)
        with patch.object(icon_cache, "cache_dir", return_value=tmp):
            icon_cache.reset_caches()
            self.assertIsNone(icon_cache.local_path(ITEM_URL))

    def test_local_path_ignores_non_icon_urls(self):
        self.assertIsNone(icon_cache.local_path("https://example.com/a.png"))


class TestNormalisation(unittest.TestCase):
    """归一化决定请求体大小：10 人战报要内联 60 多张图。"""

    def test_item_icon_is_quantised_smaller(self):
        """真图标是带 alpha 的色块图：量化到 64 色应显著缩小。

        用渐变+透明图案而不是纯色小块 —— 纯色小图的 PNG 本来就只有几百字节，
        量化后不会更小，_normalize 会（正确地）保留原图。
        """
        import math

        from PIL import Image

        # 真图标是平滑动漫的彩色图形 + 一圈透明边，量化到 64 色能省下约 10-20%
        img = Image.new("RGBA", (88, 64))
        px = img.load()
        for y in range(64):
            for x in range(88):
                px[x, y] = (
                    int(127 + 120 * math.sin(x / 7.0)),
                    int(127 + 120 * math.cos(y / 5.0)),
                    (x * y) % 256,
                    0 if (x < 4 or y < 4) else 255,
                )
        buf = io.BytesIO()
        img.save(buf, "PNG")
        raw = buf.getvalue()

        out = icon_cache._normalize(ITEM_URL, raw)
        self.assertIsNotNone(out)
        self.assertLess(len(out), len(raw))
        # 归一化后的图仍可解码，且保持原尺寸（图标不缩放，交给 CSS 定位）
        decoded = Image.open(io.BytesIO(out))
        self.assertEqual(decoded.size, (88, 64))

    def test_hero_art_is_cropped_to_vertical_aspect(self):
        from PIL import Image

        # 横版 256x144 会超出 235:272 的比例，必须被裁成竖版
        out = icon_cache._normalize(REACT_URL, _png_bytes(size=(256, 144)))
        self.assertIsNotNone(out)
        img = Image.open(io.BytesIO(out))
        ratio = img.size[0] / img.size[1]
        self.assertAlmostEqual(ratio, icon_cache._HERO_ASPECT, places=2)

    def test_garbage_is_rejected(self):
        self.assertIsNone(icon_cache._normalize(ITEM_URL, b"<html>502</html>"))
        self.assertIsNone(icon_cache._normalize(ITEM_URL, b""))


class TestHeroIcons(unittest.TestCase):
    def test_asset_key_by_chinese_name(self):
        self.assertEqual(hero_icons.asset_key("敌法师"), "antimage")
        self.assertEqual(hero_icons.asset_key("影魔"), "nevermore")

    def test_asset_key_by_hero_id(self):
        # 英雄名缺失时只能靠 ID（比赛数据里 id 更可靠）
        self.assertEqual(hero_icons.asset_key("", 1), "antimage")

    def test_portrait_url_uses_vertical_art(self):
        url = hero_icons.portrait_url("敌法师")
        self.assertTrue(url.endswith("/antimage_vert.jpg"))
        self.assertIn(icon_cache._HERO_VERT_MARKER, url)

    def test_portrait_markdown_keeps_alt(self):
        md = hero_icons.portrait_markdown("敌法师", 1, alt="敌法师")
        self.assertTrue(md.startswith("![敌法师]("))
        self.assertIn("antimage_vert.jpg", md)

    def test_unknown_hero_degrades_to_plain_text(self):
        """认不出来就返回纯文本名，绝不返回一个空字符串把内容吞掉。"""
        self.assertEqual(hero_icons.portrait_markdown("完全不存在的英雄", 99999,
                                                      alt="完全不存在的英雄"),
                         "完全不存在的英雄")

    def test_all_known_heroes_have_ids(self):
        """hero_ids.json 是英雄接口不可用时的兜底，不能是空表。"""
        heroes = hero_icons._load_ids()
        self.assertGreater(len(heroes), 100)
        self.assertIn("1", heroes)


if __name__ == "__main__":
    unittest.main()
