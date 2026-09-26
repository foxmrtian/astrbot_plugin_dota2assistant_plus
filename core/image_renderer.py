"""本地文本转图片渲染器（Pillow 兜底方案）。

网络 t2i 不可用时，用本模块把 Dota2 查询结果（Markdown）渲染成白色卡片
（背景 #ffffff、分栏目标题深蓝色、详情正文黑色）。
两个难点：

* 容器内通常没有 CJK 字体：按 *字形覆盖* 动态选择字体，并对单行文本做
  字符级字体回退（中文字体 + emoji 字体混排）；
* 装备图标与英雄头像要画成**真图**：图片来自 ``core.icon_cache``
  （本地缓存，网络 t2i 走内联 data URI，这里直接读文件），
  拿不到文件时才降级成 alt 文本。
"""
from __future__ import annotations

import os
import re
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .icon_cache import is_icon_url, local_path

# ---------------------------------------------------------------- 字体发现

# 候选字体路径：按“排版质量”排序，是否可用由字形覆盖检测决定。
_CJK_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    # pillowmd 自带（AstrBot 镜像内实际存在）
    "/usr/local/lib/python3.12/site-packages/pillowmd/data/fonts/yahei.ttf",
    "/usr/local/lib/python3.12/site-packages/pillowmd/data/fonts/smSans.ttf",
    # 其它插件可能自带的字体
    "/AstrBot/data/plugins/astrbot_plugin_outputpro/t2i_style/fonts/OPPOSans-Regular.ttf",
    # 全域覆盖字体（含 emoji），作为最后手段
    "/usr/local/lib/python3.12/site-packages/pillowmd/data/fonts/unifont.ttf",
]

# 兜底拉丁/符号字体
_LATIN_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/local/lib/python3.12/site-packages/pillowmd/data/fonts/unifont.ttf",
]

# 用于判定“这个字体能不能排版中文”的探针字符
_CJK_PROBE = "中文英雄段位战绩"
_EMOJI_PROBE = "✅❌"

# 字形覆盖缓存，避免重复打开字体文件
_glyph_cache: dict[tuple[str, int], frozenset[int]] = {}
_font_file_cache: dict[str, Optional[ImageFont.FreeTypeFont]] = {}


def _font_codepoints(path: str) -> frozenset[int]:
    """读取字体的 cmap；fontTools 缺失时退化为空集合（仅按文件存在性判断）。"""
    try:
        from fontTools.ttLib import TTFont
    except Exception:
        return frozenset()

    try:
        tt = TTFont(path, fontNumber=0, lazy=True)
        try:
            codepoints: set[int] = set()
            for table in tt["cmap"].tables:
                codepoints |= set(table.cmap.keys())
            return frozenset(codepoints)
        finally:
            tt.close()
    except Exception:
        return frozenset()


def _covers(path: str, probe: str) -> bool:
    codepoints = _glyph_cache.get((path, 0))
    if codepoints is None:
        codepoints = _font_codepoints(path)
        _glyph_cache[(path, 0)] = codepoints
    if not codepoints:
        # 无法读取 cmap，只能乐观假设可用
        return True
    return all(ord(ch) in codepoints for ch in probe)


def _first_font_covering(probe: str, candidates: list[str]) -> Optional[str]:
    for path in candidates:
        if os.path.exists(path) and _covers(path, probe):
            return path
    return None


def find_cjk_font() -> Optional[str]:
    """返回可排版中文的字体路径。"""
    return _first_font_covering(_CJK_PROBE, _CJK_FONT_CANDIDATES)


def find_emoji_font() -> Optional[str]:
    """返回可排版 ✅/❌ 的字体路径。"""
    return _first_font_covering(_EMOJI_PROBE, _CJK_FONT_CANDIDATES + _LATIN_FONT_CANDIDATES)


def find_latin_font() -> Optional[str]:
    for path in _LATIN_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _load(path: Optional[str], size: int) -> Optional[ImageFont.FreeTypeFont]:
    if not path:
        return None
    key = (path, size)
    if key not in _font_file_cache:
        try:
            _font_file_cache[key] = ImageFont.truetype(path, size)
        except Exception:
            _font_file_cache[key] = None
    return _font_file_cache[key]


class FontSet:
    """一组针对特定字号解析好的字体，支持按字符回退。"""

    def __init__(self, size: int):
        self.size = size
        self.cjk = _load(find_cjk_font(), size)
        self.emoji = _load(find_emoji_font(), size)
        self.latin = _load(find_latin_font(), size)
        self._emoji_codepoints = None
        if self.emoji is not None:
            self._emoji_codepoints = frozenset()

    @property
    def usable(self) -> bool:
        return any(f is not None for f in (self.cjk, self.latin, self.emoji))

    def _emoji_set(self) -> frozenset[int]:
        if self._emoji_codepoints is None:
            self._emoji_codepoints = frozenset()
        return self._emoji_codepoints

    def pick(self, ch: str) -> Optional[ImageFont.FreeTypeFont]:
        """为单个字符挑选最合适的字体。"""
        # 胜/负标记交给 emoji 字体（中文字体通常缺这两个字形）
        if ch in "✅❌✔✖⚠":
            font = self.emoji or self.cjk or self.latin
        elif ord(ch) > 0x2000 and not (0x3000 <= ord(ch) <= 0x9FFF):
            # 箭头、破折号等符号：优先中文字体，其次 emoji 字体
            font = self.cjk or self.emoji or self.latin
        else:
            font = self.cjk or self.latin or self.emoji
        return font or ImageFont.load_default()

    def segments(self, text: str) -> list[tuple[ImageFont.FreeTypeFont, str]]:
        """把一行文本切成 (字体, 子串) 序列，用于混排绘制。"""
        runs: list[tuple[ImageFont.FreeTypeFont, str]] = []
        for ch in text:
            font = self.pick(ch)
            if runs and runs[-1][0] is font:
                runs[-1] = (font, runs[-1][1] + ch)
            else:
                runs.append((font, ch))
        return runs

    def width(self, text: str) -> int:
        total = 0.0
        for font, chunk in self.segments(text):
            try:
                total += font.getlength(chunk)
            except Exception:
                total += font.getsize(chunk)[0]
        return int(total)

    def height(self) -> int:
        return int(self.size * 1.42)


# ---------------------------------------------------------------- Markdown 排版

# 白底黑字配色：底色纯白，分栏目标题深蓝，详情正文黑色。
_BG = (255, 255, 255)       # #ffffff  页面底色
_PANEL = (238, 242, 247)    # #eef2f7  表头
_INK = (0, 0, 0)            # 正文/详情：黑色
_INK2 = (0, 0, 0)
_MUTED = (90, 102, 117)     # #5a6675  次要文字
_ACCENT = (30, 58, 138)     # #1e3a8a  深蓝（分栏目标题，可被 theme.accent 覆盖）
_WIN = (26, 127, 55)        # #1a7f37  白底上的绿
_LOSE = (207, 34, 46)       # #cf222e  白底上的红
_LINE = (223, 229, 236)     # #dfe5ec
_ZEBRA = (250, 251, 252)    # #fafbfc
_ANALYSIS_BG = (255, 248, 239)  # #fff8ef  浅暖色底纹（默认；可被 theme 覆盖）
_ANALYSIS_LINE = (240, 196, 138)  # #f0c48a


def _hex_to_rgb(hex_str: Optional[str]) -> Optional[tuple]:
    """把 ``#rrggbb`` / ``#rgb`` 解析成 RGB 元组；非法返回 None。"""
    h = (hex_str or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return None
_ANALYSIS_INK = (0, 0, 0)   # 评价正文黑色

# 赛后总结正文字号。用户反馈与数据表格同为 17px 时偏小，这里放大到与
# 分栏目标题（## 天辉 / ## 综合分析）一致的 20px；卡片因此变长是可接受的。
_ANALYSIS_FONT_SIZE = 20
# 综合分析里三个小栏目（天辉方的表现 / 夜魇方的表现 / 一句话总结）的标题字号。
# 比正文（20px）小一号：小标题若比它统领的正文还大，层级就反了。
# 与网络模板的 19px 保持一致，两条渲染路径外观才相同。
_ANALYSIS_SUBHEAD_SIZE = 19
# 小栏目标题与上方正文之间的留白（网络模板用 padding-top 实现同样的间隔）
_ANALYSIS_SUBHEAD_PAD = 14
# 通用粗体文本的放大倍数（用于非分析块的 ``**…**``）。
# 注意：综合分析里的粗体**只加粗、不放大** —— 曾把英雄名同时加粗并放大
# 一号（1.1em），同一段里字号忽大忽小反而难读，现在字号与正文一致。
_ANALYSIS_BOLD_SCALE = 1.08

# 对战数据里每人第一行的字号倍数：正文 17px → 24px，整行统一放大。
# 整行同号是有意的 —— 曾试过「等级小号 + 昵称/KDA 大号」的行内混排，
# 但实现代价（排版引擎要按片段带字号）与收益不成比例：
# 一行之内字号不齐还会让基线看起来飘。等级的信息量本来就低，
# 与昵称、KDA 同号也不会抢焦点。
_TITLE_SCALE = 24 / 17
# 第一行按空格/中点断行的词数上限，避免把对手昵称切成两行
_TITLE_WORDS_PER_LINE = 4

# 英雄立绘 URL 的路径特征（用于把「玩家区块」与其他 ### 标题区分开）
_HERO_MARKER = "/apps/dota2/images/heroes/"

# 玩家区块：头像占左侧、竖跨右侧三行
_AVATAR_HEIGHT = 96
_AVATAR_GAP = 10          # 头像与右侧数据之间的间距
# 玩家区块包含的行数：标题 + 数据 + 装备（与网络模板的三行一致）
_PLAYER_BLOCK_LINES = 3
# 数据行超宽时最多丢到只剩这几项指标（金钱/GPM/… 前三项绝不动）
_MIN_METRIC_GROUPS = 4
# 玩家区块字号缩放下限。再小就很难读，宁可截断超长昵称：
# 一个玩家的俄文长昵称不该把整张卡的字都缩小。
_MIN_FONT_SCALE = 0.8
_ITEM_ICON_H = 26         # 装备图标高度（原图 88x64）
_NEUTRAL_ICON_H = 32      # 中立物品图标略大，与装备区分
_NEUTRAL_GAP = 14

_ACCENT_HEX = "#1f6feb"
_WIN_HEX = "#1a7f37"
_LOSE_HEX = "#cf222e"

# 卡片末尾评价区块的标题文案（与 main.py / templates.ANALYSIS_TITLES 一致）。
# 对局是赛后复盘，叫「综合分析」；其余查询（近期战绩 / 英雄 / 物品 /
# 实时 / 职业等）对的是上方那份列表，叫「点评」。两条路径都要认这两个标题，
# 否则换成「点评」后本地渲染会认不出这一块，把它当普通 h2 排版。
_ANALYSIS_TITLE = "综合分析"
_REVIEW_TITLE = "点评"
_ANALYSIS_TITLES = (_ANALYSIS_TITLE, _REVIEW_TITLE)


def _is_analysis_title(text: str) -> bool:
    """该 h2 文本是否为末尾评价区块的标题。"""
    return (text or "").strip() in _ANALYSIS_TITLES

# Markdown 图片语法。内联成 data URI 的图也走同一条规则
# （data URI 里没有空格与右括号，字符类足够）。
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")



def _classify_line(line: str) -> tuple[str, str]:
    """返回 (行类型, 去标记后的文本)。"""
    stripped = line.strip()
    if not stripped:
        return "blank", ""
    if stripped.startswith("###"):
        return "h3", stripped.lstrip("#").strip()
    if stripped.startswith("##"):
        return "h2", stripped.lstrip("#").strip()
    if stripped.startswith("#"):
        return "h1", stripped.lstrip("#").strip()
    if set(stripped) <= {"-", "*", "_"} and len(stripped) >= 3:
        return "hr", ""
    if stripped.startswith(("- ", "* ", "+ ")):
        return "li", "• " + stripped[2:].strip()
    # 有序列表。赛后总结固定输出 1./2./3. 三点（天辉 / 夜魇 / 一句话），本地兜底渲染时若按普通
    # 段落处理，会丢掉序号、三点挤成连续段落而无法分辨。
    # 分两条规则，避免误判：
    #   半角 ``1.`` / ``1)`` 之后**必须**有空格 —— 否则「1.5倍」会被当成列表；
    #   中文顿号 ``1、`` 之后可以没有空格（中文书写习惯），且顿号不可能是小数点。
    ordered = (re.match(r"^(\d{1,2})[.)]\s+(.*)$", stripped)
               or re.match(r"^(\d{1,2})、\s*(.*)$", stripped))
    if ordered:
        return "li", f"{ordered.group(1)}. {ordered.group(2).strip()}"
    if stripped.startswith("|"):
        return "table", stripped
    return "p", stripped


def _inline_runs(text: str) -> list[tuple[str, bool]]:
    """把一行 Markdown 拆成 ``[(文本, 是否加粗)]``（图片降级为 alt 文本）。

    用于**不画图**的场景：表格单元格、纯文本降级、行内测量。
    装备图标在这里退化成中文物品名（``![黑皇杖](url)`` → ``黑皇杖``），
    因为表格单元格里塞图片不现实。

    加粗必须**保留下来**而不是像早期那样直接删掉 ``**``：赛后总结里
    关键英雄与玩家 ID 靠它突出，全删掉就只剩一片同样粗细的黑字。
    容器内没有中文粗体字体（只有 yahei.ttf），所以粗体由调用方用
    ``stroke_width`` 描边模拟。
    """
    # 先把 ``**粗体**`` 切成片段，其余标记在片段内清理
    runs: list[tuple[str, bool]] = []
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text, flags=re.S)):
        bold = bool(i % 2)
        if not part:
            continue
        # 片段内：图片保留 alt、链接保留文字，行内代码去掉反引号
        seg = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", part)
        seg = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", seg)
        seg = seg.replace("`", "").replace("__", "").replace("*", "")
        # 装备图标之间的连续空格压缩成一个，避免 alt 文本被拉开
        seg = re.sub(r"[ \t]{2,}", " ", seg)
        if seg:
            runs.append((seg, bold))
    if not runs:
        return [("", False)]
    return runs


def _runs_from(text: str) -> list[tuple[str, str, bool, str]]:
    """把一行 Markdown 拆成**图文混排**片段，供绘图使用。

    返回 ``(kind, value, bold, alt)``：

    * ``("text", 文本, bold, "")``；
    * ``("img", 图片URL, bold, alt文本)`` —— 渲染时用本地缓存里的真图，
      拿不到文件才退化成 ``alt`` 文本。

    与 :func:`_inline_runs` 的区别只有一点：图片不再被压成 alt 文本，
    而是作为独立片段交给绘制层。装备图标、英雄头像因此能在本地兜底路径
    上真正画出来（网络 t2i 那条路走的是另一套 HTML 模板）。
    """
    runs: list[tuple[str, str, bool, str]] = []
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text, flags=re.S)):
        bold = bool(i % 2)
        if not part:
            continue
        pos = 0
        for match in _MD_IMAGE_RE.finditer(part):
            head = part[pos:match.start()]
            if head:
                runs.append(("text", _clean_segment(head), bold, ""))
            runs.append(("img", match.group(2), bold, match.group(1)))
            pos = match.end()
        tail = part[pos:]
        if tail:
            runs.append(("text", _clean_segment(tail), bold, ""))
    if not runs:
        runs.append(("text", "", False, ""))
    return runs


def _clean_segment(text: str) -> str:
    """清理文本片段内的残留标记：链接保留文字、去反引号与残余星号，
    并压缩装备图标之间的连续空格。"""
    seg = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    seg = seg.replace("`", "").replace("__", "").replace("*", "")
    return re.sub(r"[ \t]{2,}", " ", seg)


def _runs_text(runs: list[tuple[str, str, bool, str]]) -> str:
    """片段序列 → 纯文本（图片按 alt 表示）。"""
    return "".join(value if kind == "text" else alt for kind, value, _bold, alt in runs)


def _has_image(runs: list[tuple[str, str, bool, str]], marker: str = "") -> Optional[str]:
    """片段里是否有图片（可用 ``marker`` 限定 URL 特征）；返回首个 URL。"""
    for kind, value, _bold, _alt in runs:
        if kind == "img" and (not marker or marker in value):
            return value
    return None


def _clean_inline(text: str) -> str:
    """移除 Markdown 行内标记，返回纯文本（用于测量与旧调用点）。"""
    return "".join(chunk for chunk, _ in _inline_runs(text)).strip()


def _bold_mask(plain: str, runs: list[tuple[str, bool]]) -> list[bool]:
    """把片段级加粗展开成与 ``plain`` 等长的逐字符掩码。

    ``_wrap_text`` 折行后字符顺序不变，因此掩码可以直接按下标对齐到折行结果。
    """
    mask: list[bool] = []
    for chunk, bold in runs:
        mask.extend([bold] * len(chunk))
    # 长度不一致时（不该发生）补 False，避免下标错位
    if len(mask) < len(plain):
        mask.extend([False] * (len(plain) - len(mask)))
    return mask[:len(plain)]


def _wrap_masked(plain: str, mask: list[bool], fs: "FontSet",
                 max_width: int) -> list[tuple[str, list[bool]]]:
    """折行并同步切分粗体掩码，返回 ``[(视觉行, 该行掩码)]``。"""
    out: list[tuple[str, list[bool]]] = []
    for raw_line in plain.split("\n"):
        buf = ""
        buf_mask: list[bool] = []
        for i, ch in enumerate(raw_line):
            if buf and fs.width(buf + ch) > max_width:
                out.append((buf, buf_mask))
                buf, buf_mask = ch, []
            else:
                buf += ch
            buf_mask.append(mask[i] if i < len(mask) else False)
        out.append((buf, buf_mask))
    return out or [("", [])]


def _wrap_text(text: str, fs: "FontSet", max_width: int) -> list[str]:
    """按像素宽度折行，返回每一段视觉行（纯文本，不含粗体信息）。"""
    return [line for line, _ in _wrap_masked(text, [False] * len(text), fs, max_width)]


def _parse_table_row(line: str) -> list[str]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return [_clean_inline(c) for c in cells]


def _is_separator_row(line: str) -> bool:
    body = line.strip().strip("|")
    if not body:
        return False
    return all(set(cell.strip()) <= {"-", ":", " "} and "-" in cell for cell in body.split("|"))
def _open_image(url: str):
    """按 URL 取本地缓存的图片；未缓存 / 读不出来返回 None。

    只查本地缓存（不下载）：本地渲染是同步的，而图标在渲染前已由
    ``core.icon_cache.ensure`` 统一拉取过一轮。
    """
    if not url or not is_icon_url(url):
        return None
    path = local_path(url)
    if path is None:
        return None
    try:
        img = Image.open(path)
        img.load()
        return img.convert("RGBA")
    except Exception:
        return None


def _scaled(image, height: int):
    """按给定高度等比缩放（图标原始比例差异很大，统一以高度对齐）。"""
    w, h = image.size
    if h <= 0:
        return image
    width = max(1, int(round(w * height / h)))
    return image.resize((width, height), Image.LANCZOS)


# 折行断点：空格、全角空格、中点、全角竖线。分隔符留在前一个词的尾部，
# 于是「金钱 18.6k ｜ GPM 631」断行后仍是「…18.6k ｜」/「GPM 631」。
_BREAK_CHARS = " \t\u3000\u00b7\uff5c"


def _split_tokens(text: str) -> list[str]:
    """按折行断点把文本切成词。"""
    tokens: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _BREAK_CHARS:
            tokens.append(buf)
            buf = ""
    if buf:
        tokens.append(buf)
    return tokens


def _run_width(fs: "FontSet", value: str) -> int:
    return fs.width(value)


def _layout_runs(
    runs: list[tuple[str, str, bool, str]],
    fs: "FontSet",
    max_width: int,
    *,
    img_height: int = _ITEM_ICON_H,
    max_lines: int = 0,
) -> list[list[dict]]:
    """把图文片段排成若干行（贪心算法，与浏览器一致）。

    每个片段先切成不可再分的「词」，再按 ``max_width`` 依次塞入行内：

    * 文本超宽时按字符硬切（中文长句没有空格可断）；
    * 图片是原子片段，按固定高度等比缩放后参与排版，
      本地缓存里没有这张图时退化成 alt 文本（保证信息不丢）；
    * 断行优先落在词尾（含空格/中点/竖线），不会把昵称、物品名切成两半。

    ``max_lines`` 大于 0 时只排到这么多行就停：调用方据此判断「放不下」，
    再换个字号重排（见 :func:`_measure_player` 的等比缩放）。

    返回每行 ``[{"kind", "value", "bold", "w", "h", "img", "fs"}]``。
    """
    tokens: list[dict] = []
    for kind, value, bold, alt in runs:
        if kind == "img":
            image = _open_image(value)
            if image is not None:
                scaled = _scaled(image, img_height)
                tokens.append({"kind": "img", "value": value, "bold": bold,
                               "alt": alt or "", "w": scaled.size[0],
                               "h": img_height, "img": scaled, "fs": fs})
            else:
                # 图标没缓存：退回物品/英雄名，不能只剩空白
                for part in _split_tokens(alt or ""):
                    tokens.append({"kind": "text", "value": part, "bold": bold,
                                   "w": _run_width(fs, part), "h": fs.height(),
                                   "fs": fs})
            continue

        for token in _split_tokens(value):
            width = _run_width(fs, token)
            if width <= max_width or len(token) <= 1:
                tokens.append({"kind": "text", "value": token, "bold": bold,
                               "w": width, "h": fs.height(), "fs": fs})
                continue
            # 单个词就超过整行宽度（中文长句）：按字符硬切
            buf = ""
            for ch in token:
                if buf and _run_width(fs, buf + ch) > max_width:
                    tokens.append({"kind": "text", "value": buf, "bold": bold,
                                   "w": _run_width(fs, buf), "h": fs.height(),
                                   "fs": fs})
                    buf = ch
                else:
                    buf += ch
            if buf:
                tokens.append({"kind": "text", "value": buf, "bold": bold,
                               "w": _run_width(fs, buf), "h": fs.height(),
                               "fs": fs})

    lines: list[list[dict]] = []
    current: list[dict] = []
    width = 0
    for token in tokens:
        if current and width + token["w"] > max_width:
            # max_lines 只是**提前刹车**：超长昵称没必要把整段都排完，
            # 反正调用方看到「超过上限」就会换个字号重排。
            if max_lines and len(lines) >= max_lines:
                return lines or [[]]
            lines.append(current)
            current, width = [], 0
        current.append(token)
        width += token["w"]
    if current:
        lines.append(current)
    return lines or [[]]


def _layout_height(lines: list[list[dict]], fs: "FontSet",
                   line_gap: int = 0) -> int:
    """若干排版行的总高度。"""
    total = 0
    for line in lines:
        tall = max([fs.height(), *(item["h"] for item in line)], default=fs.height())
        total += tall + line_gap
    return total


def _draw_mixed(draw: ImageDraw.ImageDraw, x: int, y: int, text: str,
                fs: "FontSet", fill: tuple) -> int:
    """按字符级字体回退绘制一行文本，返回结束 x 坐标。"""
    for font, chunk in fs.segments(text):
        draw.text((x, y), chunk, font=font, fill=fill)
        try:
            x += font.getlength(chunk)
        except Exception:
            x += font.getsize(chunk)[0]
    return x


def _draw_masked(draw: ImageDraw.ImageDraw, x: int, y: int, text: str,
                 mask: list[bool], fs: "FontSet", fill: tuple,
                 bold_fs: Optional["FontSet"] = None) -> int:
    """绘制一行文本，``mask[i]`` 为真的字符加粗。

    容器内没有中文粗体字体（候选里只有 yahei.ttf），因此粗体用
    ``stroke_width`` 描边模拟 —— 描边与填充同色，视觉上就是更重的字。
    加粗字符若另有 ``bold_fs``（更大字号），则整段切到该字号绘制。
    """
    i = 0
    while i < len(text):
        bold = bool(mask[i]) if i < len(mask) else False
        j = i
        while j < len(text) and (bool(mask[j]) if j < len(mask) else False) == bold:
            j += 1
        chunk = text[i:j]
        # 加粗片段用描边模拟，并按需切换到放大字号
        seg_fs = (bold_fs or fs) if bold else fs
        stroke = 1 if bold else 0
        for font, part in seg_fs.segments(chunk):
            if stroke:
                draw.text((x, y), part, font=font, fill=fill,
                          stroke_width=stroke, stroke_fill=fill)
            else:
                draw.text((x, y), part, font=font, fill=fill)
            try:
                x += font.getlength(part)
            except Exception:
                x += font.getsize(part)[0]
        i = j
    return x


def _wrap_runs(
    runs: list[tuple[str, str, bool, str]],
    fs: "FontSet",
    max_width: int,
    *,
    img_height: int = _ITEM_ICON_H,
    max_lines: int = 0,
) -> list[tuple[list[dict], list[bool], str]]:
    """图文混排折行：返回 ``[(items, bold_mask, visual_text)]``。

    ``items`` 是 :func:`_layout_runs` 排好的行内片段（每个片段自带字号），
    ``bold_mask`` 与 ``visual_text`` 一一对应（图片在纯文本里按 alt 展示），
    这样既有的「按下标加粗」逻辑不用改。
    """
    out: list[tuple[list[dict], list[bool], str]] = []
    for line in _layout_runs(runs, fs, max_width, img_height=img_height,
                             max_lines=max_lines):
        mask: list[bool] = []
        text = ""
        for item in line:
            value = item["value"] if item["kind"] == "text" \
                else (item.get("alt") or "")
            text += value
            mask.extend([bool(item["bold"])] * len(value))
        out.append((line, mask, text))
    return out


def _wrap_plain_runs(
    runs: list[tuple[str, str, bool, str]],
    fs: "FontSet",
    max_width: int,
) -> list[tuple[str, list[bool]]]:
    """只有文本（无图片）时的折行，保持与旧版一致的返回值形态。"""
    return [(text, mask) for _items, mask, text in _wrap_runs(runs, fs, max_width)]


def _measure_player(item: dict, fonts, max_width: int, body_size: int,
                    title_size: int) -> None:
    """测量一个玩家区块：头像 + 右侧三行。

    版式与 ``assets/card.html`` 的网络渲染一致 —— 头像占左侧、竖跨三行，
    右侧依次是「等级/玩家ID/KDA」「数据行」「装备行」。

    **三行是硬约束**：头像只跨三行，右侧一旦折行到第四行，版式就会散架
    （头像旁边留出一大片空白）。第一行含玩家昵称、长度完全不可控
    （英文/俄文/表情符号都见过），因此这里用**等比缩放**兜底：
    正常数据用不到缩放，只有在昵称极长时才整体缩一点字号，
    换来「永远三行」。缩到下限仍放不下时，再把昵称截断。

    头像拿不到（未缓存 / 该英雄官方没有竖版立绘 / 本轮下载失败）时不占位：
    标题行里的占位符会自动退化成英雄名文本，版式退回普通三行段落。
    """
    title = item["title"]
    avatar_url = _has_image(title["runs"], _HERO_MARKER)
    avatar = None
    if avatar_url:
        opened = _open_image(avatar_url)
        avatar = _scaled(opened, _AVATAR_HEIGHT) if opened is not None else None
    avatar_w = (avatar.size[0] + _AVATAR_GAP) if avatar is not None else 0

    # 头像已经作为独立一列画在左侧，所以要从标题行里把它摘掉，
    # 否则同一张立绘会被画两遍。拿不到立绘时**保留**该片段：
    # 它会退化成英雄名文本，信息不丢。
    title_runs = title["runs"]
    if avatar is not None:
        title_runs = [run for run in title_runs
                      if not (run[0] == "img" and _HERO_MARKER in run[1])]
        # 立绘从标题里摘走后，紧随其后的分隔符会变成行首悬空的「· 」。
        # 同时去掉左侧空白，得到「Lv.19 · miracle · 10/2/5」。
        title_runs = _strip_leading_sep(title_runs)
    body_runs = [part["runs"] for part in item["parts"][1:]]

    def build(scale: float) -> tuple[list[dict], float]:
        """按给定缩放比例排一次版，返回 (行列表, 右侧实际宽度)。"""
        t_size = max(1, int(round(title_size * scale)))
        b_size = max(1, int(round(body_size * scale)))
        right = max(max_width - avatar_w, int(max_width * 0.55))
        rows: list[dict] = []
        # 每段最多排「上限 + 1」行：只要超过上限，调用方就会换字号重排，
        # 因此不必把超长昵称的后续文本也排完。
        cap = _PLAYER_BLOCK_LINES + 1
        for line, _mask, _text in _wrap_runs(title_runs, fonts(t_size), right,
                                            img_height=_AVATAR_HEIGHT,
                                            max_lines=cap):
            rows.append(_render_row(line, fonts(t_size), is_title=True))
        for idx, runs in enumerate(body_runs):
            if idx == 0:
                # 数据行：先按可用宽度丢掉放不下的次要指标，再排版。
                # 必须先丢再排 —— 排完再丢，折行位置已经错了。
                runs, _dropped = _fit_metric_runs(runs, fonts(b_size), right)
            for line, _mask, _text in _wrap_runs(runs, fonts(b_size), right,
                                                 max_lines=cap):
                rows.append(_render_row(line, fonts(b_size), is_title=False))
        widest = max((row["w"] for row in rows), default=0)
        return rows, widest

    # 1) 先按正常字号排；行数正好三行就收工（绝大多数对局走这条路径）
    scale = 1.0
    rows, widest = build(scale)

    if len(rows) > _PLAYER_BLOCK_LINES:
        # 2) 折行几乎总是标题行（昵称太长）。先**保持字号**把标题压成一行，
        #    牺牲昵称长度而不是字号 —— 缩小字号会让整张卡看起来不一致。
        clamped, width = _clamp_title_row(title_runs, fonts, title_size, scale,
                                          avatar_w, max_width)
        # 只替换标题行，数据行/装备行必须原样保留
        rows = clamped + [row for row in rows if not row["title"]]
        widest = max([width, *(row["w"] for row in rows)], default=width)

    if len(rows) > _PLAYER_BLOCK_LINES:
        # 3) 连标题压成一行都还超（数据行/装备行本身折了）：
        #    小幅缩小字号重排，下限 _MIN_FONT_SCALE 保证仍然好读。
        for candidate in (0.95, 0.9, 0.85, _MIN_FONT_SCALE):
            rows, widest = build(candidate)
            scale = candidate
            if len(rows) <= _PLAYER_BLOCK_LINES:
                break

    gap = max(2, int(round(body_size * 0.35 * scale)))
    rows_h = sum(row["h"] for row in rows) + gap * max(0, len(rows) - 1)

    item["avatar"] = avatar
    item["avatar_w"] = avatar_w
    item["rows"] = rows
    item["row_gap"] = gap
    item["font_scale"] = scale
    item["h"] = max(rows_h, avatar.size[1] if avatar is not None else 0) + int(body_size * 0.4)
    item["content_w"] = avatar_w + widest
    item["block_w"] = item["content_w"]


def _ellipsize(text: str, fs: "FontSet", max_width: int) -> str:
    """把 ``text`` 截到 ``max_width`` 内（超出部分换成省略号）。"""
    if fs.width(text) <= max_width:
        return text
    cut = text
    while cut and fs.width(cut + "…") > max_width:
        cut = cut[:-1]
    return (cut + "…") if cut else "…"


def _clamp_title_row(title_runs, fonts, title_size: int, scale: float,
                     avatar_w: int, max_width: int) -> tuple[list[dict], int]:
    """把玩家标题压成**一行**：只截断其中最长的那一段文本。

    昵称长度完全不可控（见过几十个西里尔字母的），但「等级 / KDA / 英雄名」
    必须完整保留 —— 因此这里只动最长的那段（通常正是昵称），
    其余字段原样保留，字号也不变。

    关键细节：标题里的「Lv.19 · 昵称 · 7/1/5」在 Markdown 里是**一整段**
    非粗体文本（只有 KDA 的数值是粗体），所以不能按 run 截断，否则会把
    斜杠数字一起吃掉。这里按「·」再切成字段，只对最长的**字段**打省略号。

    返回 ``(行列表, 最宽行宽)``。
    """
    fs = fonts(max(1, int(round(title_size * scale))))
    right = max(max_width - avatar_w, int(max_width * 0.55))

    # 把 run 拆成「字段」粒度：文本按「·」切，图片保持原子
    atoms: list[tuple[str, str, bool, str]] = []
    for kind, value, bold, alt in title_runs:
        if kind == "img":
            atoms.append((kind, value, bold, alt))
            continue
        parts = re.split(r"(·)", value)
        for part in parts:
            if part:
                atoms.append(("text", part, bold, ""))

    def atom_width(atom) -> int:
        _kind, value, _bold, alt = atom
        return fs.width(value if _kind == "text" else (alt or ""))

    # 候选 = 不含分隔符「·」且有内容的文本字段里最宽的那个（昵称）
    candidates = [idx for idx, atom in enumerate(atoms)
                  if atom[0] == "text" and atom[1].strip() and atom[1].strip() != "·"]
    if not candidates:
        line = _run_tokens(title_runs, fs)
        row = _render_row(line, fs, is_title=True)
        return [row], row["w"]

    target = max(candidates, key=lambda idx: atom_width(atoms[idx]))
    others = sum(atom_width(atom) for idx, atom in enumerate(atoms) if idx != target)
    budget = max(40, right - others)
    # 只截断字段的正文，保留两侧空格：否则省略号会贴到「·」上，
    # 变成「·Lv.30 ·」这种缺空格的排版（Markdown 原文是「 · Lv.30 · 」）。
    raw = atoms[target][1]
    core = raw.strip()
    lead = raw[:len(raw) - len(raw.lstrip())]
    trail = raw[len(raw.rstrip()):]
    atoms[target] = ("text", lead + _ellipsize(core, fs, budget) + trail,
                     atoms[target][2], "")

    # 相邻文本字段合并回 run，减少绘制调用
    rebuilt: list[tuple[str, str, bool, str]] = []
    for kind, value, bold, alt in atoms:
        if kind == "text" and rebuilt and rebuilt[-1][0] == "text" \
                and rebuilt[-1][2] == bold:
            rebuilt[-1] = ("text", rebuilt[-1][1] + value, bold, "")
        else:
            rebuilt.append((kind, value, bold, alt))

    line = _run_tokens(rebuilt, fs)
    row = _render_row(line, fs, is_title=True)
    return [row], row["w"]


def _run_tokens(runs, fs: "FontSet") -> list[dict]:
    """片段 → 排版 token（不做折行），用于「单行」场景的宽度计算。"""
    tokens: list[dict] = []
    for kind, value, bold, alt in runs:
        if kind == "img":
            image = _open_image(value)
            if image is not None:
                scaled = _scaled(image, _AVATAR_HEIGHT)
                tokens.append({"kind": "img", "value": value, "bold": bold,
                               "alt": alt or "", "w": scaled.size[0],
                               "h": _AVATAR_HEIGHT, "img": scaled, "fs": fs})
            else:
                text = alt or ""
                tokens.append({"kind": "text", "value": text, "bold": bold,
                               "alt": "", "w": fs.width(text),
                               "h": fs.height(), "fs": fs})
            continue
        tokens.append({"kind": "text", "value": value, "bold": bold,
                       "alt": "", "w": fs.width(value),
                       "h": fs.height(), "fs": fs})
    return tokens


def _strip_leading_sep(runs: list[tuple[str, str, bool, str]],
                       ) -> list[tuple[str, str, bool, str]]:
    """去掉片段序列开头的分隔符「·」及其两侧空白。

    用于「英雄立绘被摘出标题行」之后：``![英雄](...) · Lv.19 · …``
    摘掉图片后剩下 `` · Lv.19 · …``，第一个「·」就成了行首悬空的分隔符。
    """
    out = list(runs)
    for idx, (kind, value, bold, alt) in enumerate(out):
        if kind != "text":
            break
        stripped = value.lstrip()
        if stripped.startswith("·"):
            stripped = stripped[1:].lstrip()
        if stripped:
            out[idx] = ("text", stripped, bold, alt)
            break
        # 这一段整段都是分隔符/空白：删掉后继续看下一段
        out[idx] = ("text", "", bold, alt)
        continue
    return [run for run in out if not (run[0] == "text" and not run[1])] or out[-1:]


def _render_row(items: list[dict], fs: "FontSet", *, is_title: bool) -> dict:
    """把排版行转成绘制行（保留图片片段与整行着色标记）。

    行高取行内**最高**的片段（图片片段可能比文字高），
    矮的片段在 :func:`_draw_player` 里按行高垂直居中。
    """
    width = sum(part["w"] for part in items)
    height = max([fs.height(), *(part["h"] for part in items)], default=fs.height())
    return {"items": items, "w": width, "h": height, "fs": fs, "title": is_title}


# 数据行的分隔符，与 core.templates._stats_row 保持一致
_METRIC_SEP = "｜"


def _fit_metric_runs(runs: list[tuple[str, str, bool, str]],
                     fs: "FontSet", max_width: int,
                     ) -> tuple[list[tuple[str, str, bool, str]], int]:
    """数据行超宽时，从末尾按重要性依次丢掉**整项指标**。

    指标顺序即重要性顺序（见 ``core.templates._METRIC_ORDER``），所以丢掉的
    是尾巴上的「治疗 → 推塔 → 参战 …」，不会出现「GPM 还在、金钱没了」。

    为什么不改字号：同一张卡里所有玩家的字号必须一致，只为某一行的长数字
    缩小字号会看起来像排版错误；少一项指标则完全不破坏版式对齐。

    返回 ``(处理后的 runs, 丢弃的项数)``；``max_width`` 足够宽时原样返回。
    """
    text = "".join(value for _kind, value, _bold, _alt in runs if _kind == "text")
    groups = [g for g in text.split(_METRIC_SEP) if g.strip()]
    if len(groups) <= 1:
        return runs, 0

    dropped = 0
    while len(groups) > _MIN_METRIC_GROUPS:
        if fs.width(_METRIC_SEP.join(groups)) <= max_width:
            break
        groups.pop()
        dropped += 1
    if not dropped:
        return runs, 0

    rebuilt: list[tuple[str, str, bool, str]] = []
    for idx, group in enumerate(groups):
        if idx:
            rebuilt.append(("text", _METRIC_SEP, False, ""))
        # 指标组形如「金钱 18.6k」：标签与数值都是常规字重（详见
        # core.templates._stats_row —— 数值不加粗是刻意的）。
        # 这里仍兼容带 ``**`` 的历史卡片：把值拆出来时**一律按不粗体**处理，
        # 免得旧缓存内容在新版本里画出忽粗忽细的一行。
        match = re.match(r"^(.*?)\s*\*\*(.+?)\*\*\s*$", group.strip())
        if match:
            rebuilt.append(("text", f"{match.group(1)} {match.group(2)}", False, ""))
        else:
            rebuilt.append(("text", group.strip(), False, ""))
    return rebuilt, dropped


def _draw_player(img: "Image.Image", draw: ImageDraw.ImageDraw, item: dict,
                 y: int, padding: int, text_color: tuple,
                 accent: tuple = _ACCENT) -> None:
    """绘制玩家区块：左侧头像（竖跨三行）+ 右侧各行。"""
    avatar = item.get("avatar")
    if avatar is not None:
        img.paste(avatar, (padding, y), avatar)

    left = padding + item["avatar_w"]
    cursor = y
    for row in item["rows"]:
        tall = row["h"]
        is_title = bool(row.get("title"))
        color = accent if is_title else text_color
        x = left
        for part in row["items"]:
            fs = part["fs"]
            # 图片可能比文字高，矮的片段按行高垂直居中
            offset = max(0, (tall - part["h"]) // 2)
            if part["kind"] == "img":
                image = part.get("img")
                if image is not None:
                    img.paste(image, (x, cursor + offset), image)
                x += part["w"]
                continue
            # 标题行整行加粗：网络 t2i 里 h3 是 ``font-weight:600``，整行
            # （等级、分隔符、昵称、KDA）本来就是同一个偏重的字重，实测
            # 600 与 700 渲染完全一致 —— 所以昵称/KDA 上那对 ``**`` 在网络
            # 路径里并无视觉作用。本地 Pillow 若只按 ``**`` 描边，等级与
            # 分隔符就是常规字重，同一条标题行的两条渲染路径字重不一致。
            # 这里整行给真值，与网络路径对齐。
            mask = ([True] * len(part["value"]) if is_title
                    else [bool(part["bold"])] * len(part["value"]))
            _draw_masked(draw, x, cursor + offset, part["value"], mask, fs, color)
            x += part["w"]
        cursor += tall + item["row_gap"]


def _draw_table(img_w: int, padding: int, draw: ImageDraw.ImageDraw, item: dict,
                y: int, fonts, body_size: int, text_color: tuple) -> int:
    """绘制表格，返回新的 y。"""
    ncols = item["ncols"]
    widths = item["widths"]
    fs = fonts(body_size)
    row_h = int(body_size * 1.95)
    col_gap = 18
    xs = []
    cx = padding
    for c in range(ncols):
        xs.append(cx)
        cx += int(widths[c] * body_size * 0.56) + col_gap

    if item["header"]:
        draw.rectangle([padding - 6, y, img_w - padding + 6, y + row_h], fill=_PANEL)
        for c, cell in enumerate(item["header"]):
            if c < ncols:
                _draw_mixed(draw, xs[c], y + 6, cell, fs, _MUTED)
        y += row_h
    for r_i, row in enumerate(item["rows"]):
        if r_i % 2 == 1:
            draw.rectangle([padding - 6, y, img_w - padding + 6, y + row_h], fill=_ZEBRA)
        for c, cell in enumerate(row):
            if c >= ncols:
                continue
            color = text_color
            if cell.strip() == "✅":
                color = _WIN
            elif cell.strip() == "❌":
                color = _LOSE
            _draw_mixed(draw, xs[c], y + 6, cell, fs, color)
        y += row_h
    return y + 12


def render_text_to_image(
    text: str,
    output_path: str,
    font_size: int = 17,
    line_spacing: int = 6,
    padding: int = 26,
    bg_color: tuple = _BG,
    text_color: tuple = _INK2,
    max_width: int = 838,
    theme: Optional[dict] = None,
) -> str:
    """将 Markdown 文本渲染为白底卡片图片，返回文件路径。

    本地兜底渲染器的两条要点：

    * **真图标**：装备图标与英雄头像从 ``core.icon_cache`` 本地缓存里取图并绘制
      （网络 t2i 走内联 data URI，这里走文件），拿不到时才退化成 alt 文本；
    * **玩家区块**：``### ![英雄](立绘URL) · Lv.25 · 昵称 · **12/6/12**``
      连同其后两行（数据行、装备行）被识别为一个区块，头像占左侧竖跨三行，
      数据排在右侧 —— 与网络 t2i 模板的版式一致。
      标题里的立绘会被摘到左列，同时去掉因此悬在行首的「· 」分隔符。

    采用「先测量、后绘制」两遍布局：只有先算出每个区块的高度，
    才能给末尾的「综合分析」整块画上背景并保证它紧贴图片底部、
    不与上方表格重叠。
    """
    # 战报卡走**独立版式**（深色 + 横幅 + 两行指标条），与其余卡片的信息层级
    # 不同构，见文件末尾「战报卡（深色版式）」一节。其余卡片继续走下面的通用
    # 区块排版，互不影响 —— 这是把改造风险限制在单一卡片类型内的关键。
    if is_match_card(text):
        return render_match_card(text, output_path, theme=theme,
                                 body_size=max(12, int(font_size)))

    body_size = max(12, int(font_size))
    _theme = theme or {}
    _analysis_bg = _hex_to_rgb(_theme.get("analysis_bg")) or _ANALYSIS_BG
    _analysis_line = _hex_to_rgb(_theme.get("analysis_line")) or _ANALYSIS_LINE
    # accent 覆盖卡片主色：顶栏/波浪线/分栏目标题/粗体字全用它。
    _accent = _hex_to_rgb(_theme.get("accent")) or _ACCENT
    title_size = int(round(body_size * _TITLE_SCALE))
    fonts_by_size: dict[int, FontSet] = {}

    def fonts(size: int) -> FontSet:
        if size not in fonts_by_size:
            fonts_by_size[size] = FontSet(size)
        return fonts_by_size[size]

    # ---------------- 解析：收集需要绘制的“行对象” ----------------
    lines: list[dict] = []
    raw_lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    # 是否已进入末尾的「综合分析」区块。综合分析里的 ### 是三个小栏目
    # （天辉方的表现 / 夜魇方的表现 / 一句话总结），不是玩家标题行，
    # 不能走下面的玩家区块分支（那会去找立绘、并按 24px 画成大标题）。
    in_analysis = False
    while i < len(raw_lines):
        raw = raw_lines[i]
        kind, content = _classify_line(raw)
        if kind == "h2" and _is_analysis_title(content):
            in_analysis = True

        if kind == "table":
            # 收集连续的表格行
            block: list[str] = []
            while i < len(raw_lines) and raw_lines[i].strip().startswith("|"):
                block.append(raw_lines[i])
                i += 1
            header: Optional[list[str]] = None
            rows: list[list[str]] = []
            for idx, row in enumerate(block):
                if _is_separator_row(row):
                    continue
                cells = _parse_table_row(row)
                if header is None and idx == 0:
                    header = cells
                else:
                    rows.append(cells)
            lines.append({"kind": "table", "header": header, "rows": rows})
            continue

        if kind == "blank":
            lines.append({"kind": "blank"})
            i += 1
            continue

        runs = _runs_from(content)
        plain = _runs_text(runs)
        stripped = plain.strip()
        if not stripped:
            lines.append({"kind": "blank"})
            i += 1
            continue

        item = {"kind": kind, "runs": runs, "text": stripped}
        if kind == "h3" and not in_analysis:
            # 玩家区块：h3 之后紧跟的「数据行 + 装备行」也要一起交给绘制层，
            # 头像才能跨三行；遇到表格 / 标题即结束。
            #
            # 行与行之间**允许有空行**：Markdown 里连续两行同属一个段落，
            # 单换行会被合并成一个 <p>（网络模板下数据行与装备行会挤成一行），
            # 所以 core.templates 用空行分隔三行。这里跳过空行继续收集，
            # 但连续两个空行视为区块结束 —— 免得把下一段正文也吞进来。
            block: list[dict] = [item]
            j = i + 1
            blanks = 0
            while j < len(raw_lines) and len(block) < _PLAYER_BLOCK_LINES:
                nxt_kind, nxt_content = _classify_line(raw_lines[j])
                if nxt_kind in ("table", "h1", "h2", "h3", "hr"):
                    break
                if nxt_kind == "blank":
                    blanks += 1
                    if blanks >= 2:
                        break
                    j += 1
                    continue
                blanks = 0
                nxt_runs = _runs_from(nxt_content)
                if not _runs_text(nxt_runs).strip():
                    break
                block.append({"kind": nxt_kind, "runs": nxt_runs,
                              "text": _runs_text(nxt_runs).strip()})
                j += 1
            item = {"kind": "player", "title": block[0], "parts": block}
            i = j
            lines.append(item)
            continue

        lines.append(item)
        i += 1

    # 表格列宽（按字符显示宽度估算，中文按 2 个宽度计）
    def display_width(s: str) -> int:
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)

    for item in lines:
        if item["kind"] != "table":
            continue
        all_rows = []
        if item["header"]:
            all_rows.append(item["header"])
        all_rows.extend(item["rows"])
        ncols = max((len(r) for r in all_rows), default=0)
        widths = [0] * ncols
        for r in all_rows:
            for c, cell in enumerate(r):
                if c < ncols:
                    widths[c] = max(widths[c], display_width(cell))
        item["widths"] = widths
        item["ncols"] = ncols

    # 标记末尾「综合分析」区块：从该标题起直到文末
    analysis_from: Optional[int] = None
    for idx, item in enumerate(lines):
        if item["kind"] == "h2" and _is_analysis_title(item.get("text", "")):
            analysis_from = idx
    if analysis_from is not None:
        for item in lines[analysis_from:]:
            item["analysis"] = True

    # ---------------- 第一遍：测量每个区块的高度 ----------------
    def seg_h(size: int) -> int:
        return int(size * 1.62)

    for item in lines:
        kind = item["kind"]
        item["wrapped"] = []

        if kind == "blank":
            item["h"] = int(body_size * 0.6)
            continue

        if kind == "hr":
            item["h"] = 22
            continue

        if kind in ("li", "p"):
            size = _ANALYSIS_FONT_SIZE if item.get("analysis") else body_size
            item["font_size"] = size
            item["wrapped_masked"] = _wrap_plain_runs(item["runs"], fonts(size), max_width)
            item["wrapped"] = [w for w, _ in item["wrapped_masked"]]
            item["h"] = seg_h(size) * len(item["wrapped"])
            continue

        if kind == "h1":
            item["h"] = int(body_size * 1.85) + 10
            continue

        if kind == "h2":
            item["h"] = int(body_size * 1.6) + 12
            continue

        if kind == "h3":
            # 只有综合分析里的 ### 会走到这里（玩家标题行走 "player" 分支）。
            size = _ANALYSIS_SUBHEAD_SIZE
            item["font_size"] = size
            # 小标题本身也是加粗文本，与正文共用一套「按下标加粗」的绘制逻辑，
            # 因此这里排成 (items, mask, visual) 三元组，与 li/p 的处理一致。
            item["wrapped_masked"] = [
                (visual, mask)
                for _items, mask, visual in _wrap_runs(item["runs"], fonts(size), max_width)
            ]
            item["wrapped"] = [w for w, _ in item["wrapped_masked"]]
            item["h"] = seg_h(size) * len(item["wrapped"]) + _ANALYSIS_SUBHEAD_PAD
            continue

        if kind == "table":
            rowcount = len(item["rows"]) + (1 if item["header"] else 0)
            item["h"] = rowcount * int(body_size * 1.95) + 12
            continue

        if kind == "player":
            _measure_player(item, fonts, max_width, body_size, title_size)
            continue

    # 内容宽度：决定卡片宽度，取最宽区块
    content_w = 0
    for item in lines:
        kind = item["kind"]
        if kind == "h1":
            content_w = max(content_w, fonts(int(body_size * 1.55)).width(item["text"]))
        elif kind == "h2":
            content_w = max(content_w, 12 + fonts(int(body_size * 1.16)).width(item["text"]))
        elif kind == "h3":
            fs = fonts(item.get("font_size") or body_size)
            for visual in item.get("wrapped") or [item["text"]]:
                content_w = max(content_w, fs.width(visual))
        elif kind == "player":
            content_w = max(content_w, item["content_w"])
        elif kind in ("li", "p"):
            fs = fonts(item.get("font_size") or body_size)
            for visual in item["wrapped"]:
                content_w = max(content_w, fs.width(visual) + (6 if kind == "li" else 0))
        elif kind == "table":
            table_w = 0
            for c in range(item["ncols"]):
                table_w += int(item["widths"][c] * body_size * 0.56) + 18
            content_w = max(content_w, table_w - 18)

    # 卡片贴合内容宽度（避免右侧大片留白），并保留最小宽度
    img_w = max(360, min(max_width, content_w)) + padding * 2
    # 玩家区块可能比内容宽度更宽（头像 + 右侧三行），以它为准
    for item in lines:
        if item["kind"] == "player":
            img_w = max(img_w, item["block_w"] + padding * 2)
    img_w = min(max_width + padding * 2, img_w)

    # 底部波浪装饰纹占用高度（与 assets/card.html 的网络路径保持一致）
    total_h = padding + sum(item["h"] for item in lines) + padding
    img = Image.new("RGB", (img_w, max(80, total_h)), bg_color)
    draw = ImageDraw.Draw(img)

    # ---------------- 第二遍：先铺区块背景，再绘制文字 ----------------
    analysis_start: Optional[int] = None
    analysis_end: Optional[int] = None
    cursor = padding
    for item in lines:
        if item.get("analysis"):
            if analysis_start is None:
                analysis_start = cursor - 8
            analysis_end = cursor + item["h"] + 4
        cursor += item["h"]

    if analysis_start is not None and analysis_end is not None:
        draw.rectangle(
            [padding - 12, analysis_start, img_w - padding + 12, analysis_end],
            fill=_analysis_bg,
            outline=_analysis_line,
        )
        # 左侧色条，让末尾区块一眼可辨
        draw.rectangle(
            [padding - 12, analysis_start, padding - 8, analysis_end],
            fill=_accent,
        )

    # 顶部强调条
    draw.rectangle([0, 0, img_w, 3], fill=_accent)

    y = padding
    for item in lines:
        kind = item["kind"]
        is_analysis = bool(item.get("analysis"))

        if kind == "blank":
            pass

        elif kind == "h1":
            fs = fonts(int(body_size * 1.55))
            _draw_mixed(draw, padding, y, item["text"], fs, _accent)
            # 标题下的分隔线
            draw.line(
                [padding, y + fs.height() + 2, img_w - padding, y + fs.height() + 2],
                fill=_LINE,
                width=2,
            )

        elif kind == "h2":
            fs = fonts(int(body_size * 1.16))
            # 网络模板的 .analysis-heading 用 var(--accent)，本地保持一致
            color = _accent
            if not is_analysis:
                # 末尾区块整体已有左侧色条，避免重复
                draw.rectangle([padding, y + 3, padding + 4, y + fs.height() - 6], fill=_accent)
            _draw_mixed(draw, padding + 12, y, item["text"], fs, color)

        elif kind == "hr":
            draw.line([padding, y + 10, img_w - padding, y + 10], fill=_LINE, width=2)

        elif kind == "table":
            y = _draw_table(img_w, padding, draw, item, y, fonts, body_size, text_color)
            continue

        elif kind == "player":
            _draw_player(img, draw, item, y, padding, text_color, _accent)
            y += item["h"]
            continue

        elif kind == "h3":
            # 综合分析里的三个小栏目标题（玩家标题行不会走到这里）。
            # 小标题整行都是重点，因此掩码全真 —— _draw_masked 只按掩码描边，
            # 掩码为假时即使传了 bold_fs 也不会加粗。
            size = item.get("font_size") or _ANALYSIS_SUBHEAD_SIZE
            fs = fonts(size)
            for visual, _vmask in item.get("wrapped_masked") or [(v, []) for v in item["wrapped"]]:
                _draw_masked(draw, padding, y, visual, [True] * len(visual),
                             fs, _accent, fs)
                y += seg_h(size)
            y += _ANALYSIS_SUBHEAD_PAD
            continue

        elif kind == "li":
            size = item.get("font_size") or body_size
            fs = fonts(size)
            # 分析块的粗体只加粗、不放大：字号与正文一致（见 card.html 的
            # `.analysis-body strong{font-size:inherit}`）。传 fs 作为 bold_fs
            # 即「用同一字号描边」，避免同一段里字号忽大忽小。
            bold_fs = fs if is_analysis else fonts(int(size * _ANALYSIS_BOLD_SCALE))
            color = _ANALYSIS_INK if is_analysis else text_color
            for visual, vmask in item.get("wrapped_masked") or [(v, []) for v in item["wrapped"]]:
                _draw_masked(draw, padding + 6, y, visual, vmask, fs, color, bold_fs)
                y += seg_h(size)
            continue

        else:  # p
            size = item.get("font_size") or body_size
            fs = fonts(size)
            # 同 li：分析块内只加粗不放大，两条路径（网络/本地）字号才一致
            bold_fs = fs if is_analysis else fonts(int(size * _ANALYSIS_BOLD_SCALE))
            color = _ANALYSIS_INK if is_analysis else text_color
            for visual, vmask in item.get("wrapped_masked") or [(v, []) for v in item["wrapped"]]:
                _draw_masked(draw, padding, y, visual, vmask, fs, color, bold_fs)
                y += seg_h(size)
            continue

        y += item["h"]

    out_dir = os.path.dirname(str(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    img.save(str(output_path), "PNG")
    return str(output_path)


def cjk_available() -> bool:
    """中文排版字体是否可用（用于诊断日志）。"""
    return find_cjk_font() is not None


# ---------------------------------------------------------------- 战报卡（版式与主题）
#
# 为什么单开一条绘制路径，而不是复用上面的通用区块排版：战报卡的信息层级与
# 其余卡片**不同构** —— 它顶部有一条横跨整宽的横幅（MVP 立绘 + 比分），每位
# 玩家是「头像 + 核心行 + 两行指标条」的固定格子。把它们硬塞进通用区块流，
# 就得为兼容表格/列表/正文把每处尺寸都做成可变的，反而更难守住「重点极大、
# 次要极小、中间不留过渡档」的层级。
#
# 下面是**浅色蓝白灰**主题的令牌：白底 + 冷灰阶 + 单一蓝色强调。所有灰阶都
# 按白底重新标定过对比度（注释里逐个标了值），不是把深色主题的值搬过来 ——
# 深色下够用的颜色在白底上普遍会掉到 2:1 左右，直接搬必然读不清。
# theme 里给了哪个键就覆盖哪个，主题色因此可换。

_M_BG = (255, 255, 255)     # #ffffff  白底
_M_PANEL = (247, 249, 252)  # #f7f9fc  面板（评价区底纹）
_M_PANEL2 = (238, 242, 247) # #eef2f7  分队条底纹
_M_LINE = (226, 232, 240)   # #e2e8f0  分隔线（纯装饰，不承载文字）
_M_INK = (15, 23, 42)       # #0f172a  昵称/数值        17.9:1
_M_INK2 = (30, 41, 59)      # #1e293b  正文             14.0:1
_M_MUTED = (71, 85, 105)    # #475569  指标标签           7.5:1
_M_FAINT = (100, 116, 139)  # #64748b  日期/时长          4.9:1
_M_WIN = (21, 128, 61)      # #15803d  胜                 4.9:1
_M_LOSE = (185, 28, 28)     # #b91c1c  负                 6.3:1
_M_GOLD = (30, 58, 138)     # #1e3a8a  MVP 大字（同色系更深一档）10.4:1
_M_ACCENT = (29, 78, 216)   # #1d4ed8  强调              6.9:1
_M_PLATE = (255, 255, 255)  # 比分区衬板（浅色主题下用半透明白稳住对比度）

_M_W = 844                  # 画布宽（与参考图同宽）
_M_PAD = 32                 # 左右留白（参考图实测 32px）
_M_CW = _M_W - _M_PAD * 2   # 780 内容宽
_M_TOP = 24
_M_BANNER_H = 190
_M_BANNER_ART_W = 676       # 立绘面板宽（按用户要求较原先加宽一倍）
_M_BAND_H = 40
_M_ROW_H = 188              # 玩家块高：核心行 + 指标条 + 出装独立行
_M_ITEMS_H = 44             # 出装独立行高
_M_AVATAR_W = 60
_M_AVATAR_H = 68
_M_ITEMS_W = 246            # 指标条右侧留给装备图标的宽度（出装独立行后仅作占位参考）

_M_MATCH_H1 = "# 比赛详情"
_M_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_M_UNKNOWN_NAME = "匿名玩家"


def _match_tokens(theme) -> dict:
    """主题令牌；theme 里给了哪个键就覆盖哪个（这是「主题色可换」的实现点）。

    ``dark`` 不是颜色而是**模式开关**：它只影响立绘怎么调、比分要不要衬板，
    颜色本身一律由令牌决定。

    默认色与三套预设都从 ``core.card_renderer`` 取（单一事实源），避免
    本地 Pillow 与网络 t2i 两处各存一份默认值而漂移。
    """
    from . import card_renderer as _cr

    # 先用 card_renderer 的归一化把「预设名 / dict」统一成完整令牌，
    # 再逐项转成 RGB 元组；未提供的键落回 card_renderer 的预设默认色。
    t = _cr._normalize_theme(theme)

    def g(key: str, fallback: tuple) -> tuple:
        rgb = _hex_to_rgb(t.get(key))
        return rgb if rgb else fallback

    return {
        "dark": bool(t.get("dark", False)),
        "bg": g("bg", _M_BG),
        "panel": g("panel", _M_PANEL),
        "panel2": g("panel2", _M_PANEL2),
        "line": g("line", _M_LINE),
        "ink": g("ink", _M_INK),
        "ink2": g("ink2", _M_INK2),
        "muted": g("muted", _M_MUTED),
        "faint": g("faint", _M_FAINT),
        "win": g("win", _M_WIN),
        "lose": g("lose", _M_LOSE),
        "gold": g("gold", _M_GOLD),
        "accent": g("accent", _M_ACCENT),
        "plate": g("plate", _M_PLATE),
    }


def is_match_card(text: str) -> bool:
    """是不是战报卡。

    只认首个非空行以 ``# 比赛详情`` 开头 —— 这是 ``templates.render_match_detail``
    的固定开头。认错会把别的卡片塞进战报版式里，所以判据取最紧的那一条。
    """
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        return s.startswith(_M_MATCH_H1)
    return False


def split_match_analysis(text: str) -> tuple[str, str]:
    """按「## 综合分析 / ## 点评」把战报卡拆成主体与评价区。"""
    lines = (text or "").splitlines()
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith("## ") and _is_analysis_title(s[3:].strip()):
            return "\n".join(lines[:i]), "\n".join(lines[i:])
    return text or "", ""


def _strip_image(s: str) -> tuple[str, str, str]:
    """摘掉首个 Markdown 图片，返回 (剩余文本, alt, url)。"""
    m = _MD_IMAGE_RE.search(s)
    if not m:
        return s, "", ""
    return (s[: m.start()] + s[m.end():]), m.group(1).strip(), m.group(2).strip()


def _parse_banner_head(s: str) -> dict:
    """``MVP ![英雄](立绘) · 昵称 · 17/7/16`` → 结构化。"""
    rest, hero, art = _strip_image(s)
    out = {"hero": hero, "art": art, "name": "", "kda": ""}
    for part in rest.split("·"):
        bare = part.strip().strip("*").strip()
        if not bare:
            continue
        if re.fullmatch(r"\d+/\d+/\d+", bare):
            out["kda"] = bare
        else:
            out["name"] = bare
    return out


def _parse_score_line(s: str) -> dict:
    """``天辉 35 : 49 夜魇 · 43:32 · 2025-09-17 06:40`` → 结构化比分。"""
    body, _, meta = s.partition("·")
    out = {"left": "", "right": "", "ls": 0, "rs": 0, "meta": meta.strip()}
    m = re.search(r"(\S+)\s+(\d+)\s*:\s*(\d+)\s+(\S+)", body)
    if m:
        out["left"], out["ls"] = m.group(1), int(m.group(2))
        out["right"], out["rs"] = m.group(4), int(m.group(3))
    return out


def _parse_team_head(s: str) -> dict:
    """``天辉 失败 · 击杀 35 · 经济 114.0k`` → 结构化队伍行。"""
    parts = [p.strip() for p in s.split("·")]
    name_result = parts[0] if parts else s
    bits = name_result.split()
    out = {"name": bits[0] if bits else "", "result": bits[1] if len(bits) > 1 else "",
           "meta": " · ".join(p for p in parts[1:] if p)}
    return out


def _parse_player_title(s: str) -> dict:
    """``![敌法师](立绘) · Lv.26 · **昵称** · **12/3/8** · GPM 706 · XPM 812``"""
    rest, hero, portrait = _strip_image(s)
    out = {"hero": hero, "portrait": portrait, "level": "", "name": "",
           "kda": "", "gpm": "", "xpm": "", "metrics": [], "items": []}
    for part in rest.split("·"):
        bare = part.strip().strip("*").strip()
        if not bare:
            continue
        if bare.startswith("Lv."):
            out["level"] = bare
        elif bare.startswith("GPM"):
            out["gpm"] = bare[3:].strip()
        elif bare.startswith("XPM"):
            out["xpm"] = bare[3:].strip()
        elif re.fullmatch(r"\d+/\d+/\d+", bare):
            out["kda"] = bare
        else:
            out["name"] = bare
    out["name"] = out["name"] or _M_UNKNOWN_NAME
    return out


def _parse_metrics(s: str) -> list[tuple[str, str]]:
    """``金钱 28.6k｜补刀 412/6｜…`` → [(标签, 数值), …]。"""
    out: list[tuple[str, str]] = []
    for part in s.split("｜"):
        bare = part.strip().strip("*").strip()
        if not bare:
            continue
        bits = bare.split(" ", 1)
        out.append((bits[0].strip(), bits[1].strip() if len(bits) > 1 else ""))
    return out


def _parse_items(s: str) -> list[dict]:
    """``![动力鞋](u)![狂战斧](u) ＋ ![暗影护符](u)`` → 图标列表。

    「＋」标记中立物品的起点：记进 ``gap`` 让绘制时留一道空隙，
    一眼能分辨「基础装备」与「中立物品」，而不是挤成一条看不出分界的图标带。
    """
    out: list[dict] = []
    gap = False
    pos = 0
    for m in _MD_IMAGE_RE.finditer(s):
        if "＋" in s[pos:m.start()]:
            gap = True
        out.append({"alt": m.group(1).strip(), "url": m.group(2).strip(), "gap": gap})
        gap = False
        pos = m.end()
    return out


def _parse_match_body(body: str) -> dict:
    """解析战报卡主体；认不出的行直接忽略，绝不因为形状变化而抛异常。"""
    data: dict = {"match_id": "", "banner": None, "score": None, "teams": []}
    team: Optional[dict] = None
    player: Optional[dict] = None

    def flush() -> None:
        nonlocal player
        if player is not None and team is not None:
            team["players"].append(player)
        player = None

    for raw in (body or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("### "):
            flush()
            player = _parse_player_title(s[4:])
            continue
        if s.startswith("## "):
            flush()
            head = s[3:].strip()
            if head.startswith("MVP "):
                data["banner"] = _parse_banner_head(head[4:])
            else:
                team = _parse_team_head(head)
                team["players"] = []
                data["teams"].append(team)
            continue
        if s.startswith("# "):
            if _M_MATCH_H1 in s:
                data["match_id"] = s.rsplit("#", 1)[-1].strip()
            continue
        if player is not None:
            if s.startswith("装备："):
                player["items"] = _parse_items(s[3:])
            elif "｜" in s:
                player["metrics"] = _parse_metrics(s)
            continue
        if data["score"] is None and " : " in s:
            data["score"] = _parse_score_line(s)
    flush()
    return data


# ---------------------------------------------------------------- 图像工具

def _cover(image, w: int, h: int):
    """等比放大并居中裁剪到 (w, h)；取景偏上，保住画面主体的脸。"""
    sw, sh = image.size
    if sw <= 0 or sh <= 0:
        return image
    scale = max(w / sw, h / sh)
    nw, nh = max(w, int(sw * scale + 0.5)), max(h, int(sh * scale + 0.5))
    img = image.resize((nw, nh), Image.LANCZOS)
    return img.crop(((nw - w) // 2, int((nh - h) * 0.25), 
                     (nw - w) // 2 + w, int((nh - h) * 0.25) + h))


def _dim(image, factor: float):
    """整体压暗（保留 alpha）。"""
    r, g, b, a = image.split()
    lut = [int(i * factor) for i in range(256)]
    return Image.merge("RGBA", (r.point(lut), g.point(lut), b.point(lut), a))


def _gray(image, alpha_factor: float = 0.5):
    """去色成灰色并降透明度（保留 alpha 通道缩放）。

    用于「未购买的神杖/魔晶」：`_dim` 只是压暗，彩色图标压暗后仍是
    「暗的彩色」，深蓝底上几乎看不见；真正符合「未点亮 = 空心」语义的
    是**去色成灰**——轮廓还在，但明确读得出「没买」。
    """
    gray = image.convert("L")                      # 亮度 → 灰度
    _, _, _, a = image.split()
    a = a.point(lambda v: int(v * alpha_factor))   # 半透明，避免灰块过重
    return Image.merge("RGBA", (gray, gray, gray, a))


def _tone(image, tok: dict):
    """把立绘调成「能当底图但抢不走文字」的状态。

    深色主题往下压、浅色主题朝白底混合 —— 方向相反，目的相同：让压在
    立绘上的文字拿到足够对比度。原图直铺在任何主题下都会让文字糊掉。

    浅色主题**不压暗**：白底边上出现一块暗色立绘会像贴错了图，
    朝白混合既保住了构图，又和蓝白灰的整体调子一致。
    """
    if tok["dark"]:
        return _dim(image, 0.42)
    white = Image.new("RGBA", image.size, (255, 255, 255, 255))
    return Image.blend(image, white, 0.45)


def _fade_edge(image, side: str, fade_px: int):
    """把图像某一侧做成渐隐的 alpha 蒙版，贴到面板上时不会出现硬切边。"""
    w, h = image.size
    fade_px = max(1, min(fade_px, w))
    mask = Image.new("L", (w, h), 255)
    md = ImageDraw.Draw(mask)
    for i in range(fade_px):
        level = int(255 * (i / fade_px))
        x = i if side == "left" else w - 1 - i
        md.line([(x, 0), (x, h)], fill=level)
    return mask


def _mtext(draw: ImageDraw.ImageDraw, x: int, y: int, s: str, fs: "FontSet",
           fill: tuple, bold: bool = False, bold_fs: Optional["FontSet"] = None) -> int:
    """画一行文本；bold 用描边模拟（容器内没有中文粗体字体）。"""
    if not s:
        return x
    if bold:
        return _draw_masked(draw, x, y, s, [True] * len(s), fs, fill,
                            bold_fs=bold_fs or fs)
    return _draw_mixed(draw, x, y, s, fs, fill)


def _mtext_r(draw: ImageDraw.ImageDraw, right: int, y: int, s: str, fs: "FontSet",
             fill: tuple, bold: bool = False) -> int:
    """右对齐画一行文本，返回起始 x。"""
    x = right - fs.width(s)
    _mtext(draw, x, y, s, fs, fill, bold)
    return x


def _baseline(y: int, box_h: int, fs: "FontSet") -> int:
    """在高度为 box_h 的格子里垂直居中一行文本的 y。"""
    return y + max(0, (box_h - fs.height()) // 2)


# ---------------------------------------------------------------- 绘制

_AGHS_SCEPTER_NAME = "ultimate_scepter.png"
_AGHS_SHARD_NAME = "aghanims_shard.png"


def _is_aghs_pair(items: list[dict], i: int) -> bool:
    """位置 i、i+1 是否正是相邻的「神杖 + 魔晶」两张图。

    靠 URL 文件名判断而非索引，templates 里这两张图始终相邻发出；
    一旦出现顺序/缺失变化就退回单张绘制，不会画错。
    """
    if i + 1 >= len(items):
        return False
    a = (items[i].get("url") or "").rsplit("/", 1)[-1]
    b = (items[i + 1].get("url") or "").rsplit("/", 1)[-1]
    return a == _AGHS_SCEPTER_NAME and b == _AGHS_SHARD_NAME


def _aghs_combo(scepter: dict, shard: dict, height: int):
    """把神杖（上）与魔晶（下）合成一张竖排组合状态图。

    客户端 HUD 里这是一张组合图标：默认空心、出了对应装备才填充颜色。
    CDN 上没有现成组合图，这里用两张物品图标竖排合成；未点亮的那个
    灰化（``_dim``），等效于「空心」，已点亮保留原色（「填色」）。
    宽度取两者较大值，高度给定的行高内上下平分。任一张图缺失时返回 None，
    让调用方退回逐张绘制。
    """
    si = _open_image(scepter.get("url", ""))
    sh = _open_image(shard.get("url", ""))
    if si is None or sh is None:
        return None
    each_h = max(1, (height - 1) // 2)
    gap = 1

    def norm(img):
        w = max(1, int(round(img.width * each_h / max(1, img.height))))
        return img.resize((w, each_h), Image.LANCZOS)

    si = norm(si)
    sh = norm(sh)
    if (scepter.get("alt") or "").startswith("［无］"):
        si = _gray(si)      # 未购买 → 灰色半透明（去色）
    if (shard.get("alt") or "").startswith("［无］"):
        sh = _gray(sh)      # 未购买 → 灰色半透明（去色）
    w = max(si.width, sh.width)
    combo = Image.new("RGBA", (w, each_h * 2 + gap), (0, 0, 0, 0))
    combo.paste(si, ((w - si.width) // 2, 0), si)
    combo.paste(sh, ((w - sh.width) // 2, each_h + gap), sh)
    return combo


def _draw_kda(draw, x: int, y: int, kda: str, fs: "FontSet", tok: dict,
              bold: bool = False) -> int:
    """K/D/A 分色：击杀绿、死亡红、助攻蓝。

    三色是电竞战绩图的通行约定，扫一眼就知道「杀得多还是送得多」；
    整串同色必须逐位读数字才能比较，在十行玩家之间对比时尤其费劲。
    """
    parts = kda.split("/")
    colors = (tok["win"], tok["lose"], tok["accent"])
    for i, part in enumerate(parts):
        if i:
            x = _mtext(draw, x, y, "/", fs, tok["muted"])
        x = _mtext(draw, x, y, part, fs, colors[i] if i < len(colors) else tok["ink2"],
                   bold=bold)
    return x


def _draw_banner(img, draw, data: dict, tok: dict, fonts, top: int) -> None:
    """顶部横幅：MVP 大字在左，立绘在右，比分压图。"""
    left, right = _M_PAD, _M_W - _M_PAD
    box = (left, top, right, top + _M_BANNER_H)
    draw.rounded_rectangle(box, radius=14, fill=tok["panel"])

    banner = data.get("banner") or {}
    score = data.get("score") or {}

    # ---- 立绘面板（右侧）----
    art_x = right - _M_BANNER_ART_W
    art = _open_image(banner.get("art", "")) if banner.get("art") else None
    if art is not None:
        art = _cover(art, _M_BANNER_ART_W, _M_BANNER_H)
        art = _tone(art, tok)          # 按主题调明暗，保证压上去的文字可读
        # 左侧 30% 区域做渐隐：立绘从完全透明平滑过渡到完整显示，
        # 与左侧面板衔接更自然（用户指定 30%）。
        fade = max(1, int(_M_BANNER_ART_W * 0.30))
        mask = _fade_edge(art, "left", fade)
        img.paste(art, (art_x, top), mask)
        # 左侧再铺一层由面板色到透明的渐变，让立绘与左半区自然衔接
        scrim = Image.new("RGBA", (_M_BANNER_ART_W, _M_BANNER_H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(scrim)
        for i in range(_M_BANNER_ART_W):
            alpha = int(170 * max(0.0, 1 - i / fade))
            if alpha:
                sd.line([(i, 0), (i, _M_BANNER_H)],
                        fill=(tok["panel"][0], tok["panel"][1], tok["panel"][2], alpha))
        img.paste(scrim, (art_x, top), scrim)

    f_eye = fonts(13)
    f_name = fonts(34)
    f_hero = fonts(14)
    f_kda = fonts(22)
    f_score = fonts(34)
    f_score_s = fonts(22)
    f_team = fonts(14)
    f_meta = fonts(11)

    x = left + 22

    # ---- 左上角：日期 / 时长（极次要，小灰字）----
    meta_bits = [b.strip() for b in (score.get("meta") or "").split("·") if b.strip()]
    meta_line = " · ".join(meta_bits)
    if data.get("match_id"):
        meta_line = f"{meta_line} · #{data['match_id']}" if meta_line else f"#{data['match_id']}"
    if meta_line:
        _mtext(draw, x, top + 14, meta_line, f_meta, tok["faint"])

    # ---- MVP 眉标 + 昵称（全图最大元素，深蓝加粗）+ 英雄/等级 + KDA ----
    y = top + 40
    _mtext(draw, x, y, "MVP", f_eye, tok["accent"], bold=True)
    y += 22
    _mtext(draw, x, y, banner.get("name", ""), f_name, tok["gold"], bold=True)
    y += 44
    hero_line = " · ".join(p for p in (banner.get("hero", ""),) if p)
    if hero_line:
        _mtext(draw, x, y, hero_line, f_hero, tok["muted"])
    y += 22
    if banner.get("kda"):
        _draw_kda(draw, x, y, banner["kda"], f_kda, tok, bold=True)

    # ---- 比分（压在立绘上）----
    if score.get("left") and score.get("right"):
        win_left = score["ls"] > score["rs"]
        # 比分数字统一字号（不再「胜大负小」），胜负只靠颜色区分，
        # 数字大小一致时扫读更快、也更克制。
        num_fs = f_score
        l_col = tok["win"] if win_left else tok["muted"]
        r_col = tok["muted"] if win_left else tok["win"]
        lw = num_fs.width(str(score["ls"]))
        rw = num_fs.width(str(score["rs"]))
        lname = f_team.width(score["left"])
        rname = f_team.width(score["right"])
        colon_fs = f_score_s
        # 三个间隙各 10px。**先量后画**：衬板要刚好包住整组，
        # 靠猜留白会在队名长短变化时露馅。
        total = (lname + 10 + lw + 10 + colon_fs.width(":")
                 + 10 + rw + 10 + rname)
        sx = right - 22 - total
        sy = top + _M_BANNER_H - 74
        ty = _baseline(sy, num_fs.height(), f_team)
        if not tok["dark"]:
            # 浅色主题下立绘明暗不定，比分直接压上去无法保证对比度。
            # 垫一块近不透明的白衬板把它稳住：形状极简，视觉上就是一枚
            # 数据标签；透明度选 240 是让 14px 的胜方队名也守得住 AA 4.5:1。
            draw.rounded_rectangle(
                (sx - 14, sy - 10, right - 10, sy + num_fs.height() + 8),
                radius=10, fill=tok["plate"] + (255,))
        x = _mtext(draw, sx, ty, score["left"], f_team, l_col)
        x = _mtext(draw, x + 10, sy, str(score["ls"]), num_fs, l_col, bold=win_left)
        x = _mtext(draw, x + 10, ty, ":", colon_fs, tok["faint"])
        x = _mtext(draw, x + 10, sy, str(score["rs"]), num_fs, r_col, bold=not win_left)
        _mtext(draw, x + 10, ty, score["right"], f_team, r_col)


def _draw_team_band(draw, team: dict, tok: dict, fonts, y: int) -> None:
    """分队标题：一行内给出胜负、总击杀、团队经济。"""
    left, right = _M_PAD, _M_W - _M_PAD
    won = team.get("result") == "获胜"
    draw.rounded_rectangle((left, y, right, y + _M_BAND_H), radius=8,
                           fill=tok["panel2"])
    # 左侧竖条标示胜负：色彩之外再给一个形状信号，色觉差异下也能分辨
    draw.rounded_rectangle((left, y + 8, left + 4, y + _M_BAND_H - 8), radius=2,
                           fill=tok["win"] if won else tok["lose"])
    f_team = fonts(16)
    f_meta = fonts(12)
    ty = _baseline(y, _M_BAND_H, f_team)
    x = _mtext(draw, left + 18, ty, team.get("name", ""), f_team, tok["ink"], bold=True)
    x = _mtext(draw, x + 10, ty + 3, team.get("result", ""), f_meta,
               tok["win"] if won else tok["lose"])
    if team.get("meta"):
        _mtext_r(draw, right - 18, ty + 3, team["meta"], f_meta, tok["muted"])


def _draw_player_row(img, draw, player: dict, tok: dict, fonts, y: int) -> None:
    """单名玩家：头像 + 核心行（昵称/KDA/GPM/XPM）+ 两行指标条 + 装备。"""
    left, right = _M_PAD, _M_W - _M_PAD

    # ---- 头像 ----
    av = _open_image(player.get("portrait", "")) if player.get("portrait") else None
    if av is not None:
        av = _cover(av, _M_AVATAR_W, _M_AVATAR_H)
        mask = Image.new("L", (_M_AVATAR_W, _M_AVATAR_H), 255)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle((0, 0, _M_AVATAR_W - 1, _M_AVATAR_H - 1), radius=8, fill=255)
        img.paste(av, (left, y + 4), mask)
        draw.rounded_rectangle((left, y + 4, left + _M_AVATAR_W - 1, y + 4 + _M_AVATAR_H - 1),
                               radius=8, outline=tok["line"])

    f_name = fonts(22)
    f_hero = fonts(12)
    f_kda = fonts(19)
    f_lab = fonts(11)
    f_val = fonts(21)

    core_x = left + (_M_AVATAR_W + 14 if av is not None else 0)
    core_y = y + 8
    _mtext(draw, core_x, core_y, player.get("name", ""), f_name, tok["ink"], bold=True)
    hero_line = " · ".join(p for p in (player.get("hero", ""), player.get("level", "")) if p)
    if hero_line:
        _mtext(draw, core_x, core_y + 30, hero_line, f_hero, tok["muted"])

    # ---- 核心行右侧：KDA（大号，与昵称同档）----
    # GPM/XPM 已下移到下方指标条（与金钱等六项同排），核心行只留 KDA。
    # KDA 这组同样是「上方数值 + 下方标签」，组内水平居中。
    if player.get("kda"):
        kda = player["kda"]
        vw = f_kda.width(kda) + f_kda.width("//")
        lw = f_lab.width("KDA")
        col_w = max(vw, lw)
        cx = right - 4 - col_w // 2
        _draw_kda(draw, cx - vw // 2, core_y + 2, kda, f_kda, tok, bold=True)
        _mtext(draw, cx - lw // 2, core_y + 26, "KDA", f_lab, tok["muted"])

    # ---- 指标条：上行大号数值、下行小号标签（两行，层级由字号承担）----
    # 装备移到玩家块底部独立成行后，指标条占满整行宽度。
    band_y = y + _M_AVATAR_H + 14
    metrics = player.get("metrics") or []
    if metrics:
        n = len(metrics)
        col_w = max(1, (right - left) // n)
        for i, (label, value) in enumerate(metrics):
            cell_x = left + i * col_w
            cx = cell_x + col_w // 2
            vw = f_val.width(value)
            _mtext(draw, cx - vw // 2, band_y, value, f_val, tok["ink"])
            lw = f_lab.width(label)
            _mtext(draw, cx - lw // 2, band_y + 26, label, f_lab, tok["muted"])

    # ---- 出装：玩家块底部独立成行，从左往右排，完整展示 ----
    # 内容包括 6 件主战装备、中立物品（＋ 后）、背包（◦ 后）、
    # 阿哈得姆神杖 / 魔晶点亮状态（｜ 后）。
    items = player.get("items") or []
    if items:
        iy = y + 128                       # 指标条之下的独立行
        ih = _M_ITEMS_H - 10
        ix = left
        i = 0
        while i < len(items):
            it = items[i]
            # 相邻的神杖 + 魔晶两张图 → 合成一张「上神杖下图标」的组合状态图，
            # 未点亮的那个灰化（复刻客户端 HUD 的空心/填色语义）。
            if _is_aghs_pair(items, i):
                comp = _aghs_combo(items[i], items[i + 1], ih)
                if comp is not None:
                    w = comp.width
                    if ix + w > right:
                        break
                    img.paste(comp, (ix, iy), comp)
                    ix += w + 8
                i += 2
                continue
            icon = _open_image(it["url"])
            if icon is None:
                i += 1
                continue
            w = max(1, int(round(icon.width * ih / max(1, icon.height))))
            if ix + w > right:
                break                        # 超宽截断，保持单行不折行
            if it.get("gap"):
                ix += 10                     # 中立物品前的空隙
            alt = it.get("alt", "")
            lit = not alt.startswith("［无］")   # ［无］ 前缀 → 未点亮，灰化
            icon = icon.resize((w, ih), Image.LANCZOS)
            if not lit:
                icon = _dim(icon, 0.45)
            img.paste(icon, (ix, iy), icon)
            ix += w + 8
            i += 1

    # ---- 行分隔线 ----
    draw.line([(left, y + _M_ROW_H - 6), (right, y + _M_ROW_H - 6)], fill=tok["line"])


def _analysis_layout(text: str, f_body: "FontSet", max_width: int):
    """把评价正文排版成折行列表；供「量高」与「绘制」共用。

    只依赖 ``f_body`` 的宽度，与画布无关，因此可以在建画布前先量出
    评价区真实高度 —— 这修复了「画布先按小高度建、评价被裁到画布外」
    的问题。
    """
    lines = [l.strip() for l in (text or "").splitlines()]
    title = ""
    body: list[str] = []
    for l in lines:
        if not l:
            continue
        if l.startswith("## "):
            title = l[3:].strip()
            continue
        body.append(l.replace("**", "").replace("###", "").strip())

    wrapped: list[str] = []
    for para in body:
        cur = ""
        for ch in para:
            if f_body.width(cur + ch) > max_width:
                wrapped.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            wrapped.append(cur)
    return title, wrapped


def _analysis_block_height(text: str, f_body: "FontSet", max_width: int) -> int:
    """评价区的真实绘制高度（与 ``_draw_analysis`` 的排版一致）。"""
    _, wrapped = _analysis_layout(text, f_body, max_width)
    line_h = int(f_body.height()) + 8
    pad = 18
    return pad + 30 + line_h * len(wrapped) + pad


def _draw_analysis(draw, img, text: str, tok: dict, fonts, y: int,
                   max_width: int) -> int:
    """卡尾评价区：深色面板 + 左侧强调条 + 标题。返回结束 y。"""
    f_title = fonts(17)
    f_body = fonts(15)
    line_h = int(f_body.height()) + 8

    title, wrapped = _analysis_layout(text, f_body, max_width)

    pad = 18
    h = pad + 30 + line_h * len(wrapped) + pad
    left, right = _M_PAD, _M_W - _M_PAD
    draw.rounded_rectangle((left, y, right, y + h), radius=12, fill=tok["panel"])
    draw.rounded_rectangle((left, y + 12, left + 4, y + h - 12), radius=2,
                           fill=tok["accent"])
    _mtext(draw, left + pad, y + pad - 2, title, f_title, tok["accent"], bold=True)
    ty = y + pad + 30
    for line in wrapped:
        _mtext(draw, left + pad, ty, line, f_body, tok["ink2"])
        ty += line_h
    return y + h


def render_match_card(text: str, output_path: str, theme: Optional[dict] = None,
                      body_size: int = 17) -> str:
    """把战报卡的 Markdown 渲染成深色卡片，返回输出路径。

    与通用渲染器一致采用「先测量、后绘制」：横幅与每行玩家高度都是固定值，
    先按块累加算出画布高度，再一次性绘制，末尾的评价区才能紧贴图片底部。
    """
    tok = _match_tokens(theme)
    body, analysis = split_match_analysis(text)
    data = _parse_match_body(body)

    fonts_by_size: dict[int, FontSet] = {}

    def fonts(size: int) -> FontSet:
        if size not in fonts_by_size:
            fonts_by_size[size] = FontSet(size)
        return fonts_by_size[size]

    # ---- 先算总高（评价区也要按真实高度计入，否则会被裁到画布外）----
    height = _M_TOP
    height += _M_BANNER_H + 18
    for team in data["teams"]:
        height += _M_BAND_H + 10
        height += _M_ROW_H * max(1, len(team["players"]))
        height += 16
    if analysis:
        height += 22 + _analysis_block_height(analysis, fonts(15), _M_CW - 36) + 18

    img = Image.new("RGBA", (_M_W, height), tok["bg"] + (255,))
    draw = ImageDraw.Draw(img)

    y = _M_TOP
    if data.get("banner") or data.get("score"):
        _draw_banner(img, draw, data, tok, fonts, y)
        y += _M_BANNER_H + 18

    for team in data["teams"]:
        _draw_team_band(draw, team, tok, fonts, y)
        y += _M_BAND_H + 10
        if team["players"]:
            for player in team["players"]:
                _draw_player_row(img, draw, player, tok, fonts, y)
                y += _M_ROW_H
        else:
            _mtext(draw, _M_PAD, y, "暂无数据", fonts(15), tok["muted"])
            y += _M_ROW_H
        y += 16

    if analysis:
        y = _draw_analysis(draw, img, analysis, tok, fonts, y + 22, _M_CW - 36)

    out = img.convert("RGB")
    out.save(output_path, "PNG")
    return output_path
