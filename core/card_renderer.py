"""Dota2 结果卡片渲染：网络 t2i 优先，本地 Pillow 兜底。

对外只暴露 :func:`render_card`：给定 Markdown 文本，返回一张可用图片的本地路径。
设计要点：

* **图标先落本地**：装备图标与英雄头像由 ``core.icon_cache`` 下载到本地缓存，
  发给网络 t2i 前把 URL 内联成 ``data:`` URI。端点里的浏览器不需要访问外网，
  因此不会再出现「图标位置空白 / 拉图超时导致整次渲染失败」；
  本地 Pillow 渲染则直接读缓存文件画真图。
* 网络 t2i（AstrBot 官方 ``/generate``）排版质量最好，但结果需要校验：
  历史上有过端点返回 0 字节、非图片内容（HTML 502 页面）甚至纯文本 JSON
  错误体的例子，因此渲染后必须读一遍像素。
* 网络失败（超时、全端点 500、图片损坏）时静默降级到本地 Pillow 渲染。
* 输出文件落在 AstrBot temp 目录，交给 ``event.track_temporary_local_file`` 清理。
"""
from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from ..compat import logger
from . import icon_cache

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_TEMPLATE_CACHE: dict[str, str] = {}

# 网络 t2i 渲染选项。device_scale_factor_level 取 high：
# 实测 low 会产出损坏图片，normal 偏小、ultra 体积过大。
_T2I_OPTIONS = {
    "full_page": True,
    "type": "jpeg",
    "quality": 88,
    "device_scale_factor_level": "high",
}


def _template(theme: Optional[dict] = None) -> Optional[str]:
    if "card" not in _TEMPLATE_CACHE:
        try:
            _TEMPLATE_CACHE["card"] = (_ASSETS_DIR / "card.html").read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning(f"Dota2 卡片模板读取失败，将只使用本地渲染: {exc}")
            _TEMPLATE_CACHE["card"] = ""
    html = _TEMPLATE_CACHE["card"] or ""
    if not html:
        return None
    # 注入可配置配色：浅色/深色令牌全部透传给 CSS，未提供的键保持模板默认值。
    overrides = {
        "--bg": theme.get("bg") if theme else None,
        "--panel": theme.get("panel") if theme else None,
        "--panel-2": theme.get("panel2") if theme else None,
        "--line": theme.get("line") if theme else None,
        "--ink": theme.get("ink") if theme else None,
        "--ink-2": theme.get("ink2") if theme else None,
        "--muted": theme.get("muted") if theme else None,
        "--faint": theme.get("faint") if theme else None,
        "--accent": theme.get("accent") if theme else None,
        "--accent-2": theme.get("gold") if theme else None,
        "--accent-soft": theme.get("accent") if theme else None,
        "--win": theme.get("win") if theme else None,
        "--lose": theme.get("lose") if theme else None,
        "--gold": theme.get("gold") if theme else None,
        "--analysis-bg": theme.get("analysis_bg") if theme else None,
        "--analysis-line": theme.get("analysis_line") if theme else None,
    }
    for var, val in overrides.items():
        if val:
            # 仅替换 :root 里该变量的初值，避免误伤其它位置。
            html = re.sub(
                rf"(?m)^(\s*{re.escape(var)}:)#[0-9a-fA-F]{{3,6}};",
                rf"\1{val};",
                html,
                count=1,
            )
    # 手绘可爱风（pink）：给 <body> 加 handdrawn 类，启用模板里的装饰 CSS。
    if theme and str(theme.get("style") or "") == "handdrawn":
        html = re.sub(r"(?m)^<body\b", '<body class="handdrawn"', html, count=1)
    return html


# ---------------------------------------------------------------- 主题预设
#
# 三套主题是**唯一事实源**：本地 Pillow 渲染器与网络 t2i 模板都从这里取色。
# 用户在配置里只用一个下拉框选预设名，不开放逐色自定义（那样太复杂）。
#
# 每套都含 14 个令牌 + dark 开关；颜色按「同一份 Markdown、两条渲染路径
# 视觉一致」的目标调校，并保证关键文字对底色达到 WCAG AA 对比度。

_THEME_PRESETS: dict[str, dict] = {
    # 蓝白：白底 + 蓝强调（默认，浅色蓝白灰）
    "light": {
        "dark": False,
        "bg": "#ffffff", "panel": "#f7f9fc", "panel2": "#eef2f7",
        "line": "#e2e8f0", "ink": "#0f172a", "ink2": "#1e293b",
        "muted": "#475569", "faint": "#64748b",
        "accent": "#1d4ed8", "gold": "#1e3a8a",
        "win": "#15803d", "lose": "#b91c1c",
        "analysis_bg": "#f7f9fc", "analysis_line": "#1d4ed8",
    },
    # 深色暗金：近黑暖调底 + 暗金强调，沉稳电竞感
    "dark-gold": {
        "dark": True,
        "bg": "#0d0a08", "panel": "#161210", "panel2": "#1e1815",
        "line": "#2b2318", "ink": "#f2ead9", "ink2": "#d8ccb4",
        "muted": "#a89a7d", "faint": "#7a6f5a",
        "accent": "#c9a227", "gold": "#e3c15a",
        "win": "#7fb069", "lose": "#c25a4e",
        "analysis_bg": "#161210", "analysis_line": "#c9a227",
    },
    # 粉色：粉底 + 玫红强调，柔和可爱（手绘可爱风：圆润字体 + 手绘装饰）
    "pink": {
        "dark": False,
        "style": "handdrawn",
        "bg": "#fff5f8", "panel": "#ffeef4", "panel2": "#ffe3ee",
        "line": "#f7c9da", "ink": "#5b2333", "ink2": "#7a3a4e",
        "muted": "#a86b7e", "faint": "#c08a9a",
        "accent": "#e5538a", "gold": "#c2336b",
        "win": "#4e9d6e", "lose": "#d1586b",
        "analysis_bg": "#ffeef4", "analysis_line": "#e5538a",
    },
    # 深蓝暗金：深蓝底 + 暗金标题/重点，其余文字白色（用户指定底 #0a1942）
    "navy-gold": {
        "dark": True,
        "bg": "#0a1942", "panel": "#10245a", "panel2": "#16306e",
        "line": "#26408a", "ink": "#ffffff", "ink2": "#e8ecf5",
        "muted": "#93a3c8", "faint": "#64749a",
        "accent": "#c9a227", "gold": "#e3c15a",
        "win": "#5fb85f", "lose": "#d1605a",
        "analysis_bg": "#10245a", "analysis_line": "#c9a227",
    },
}

# 兼容旧值：旧的 mode=dark / dark=True 归到深色暗金预设
_THEME_ALIASES = {
    "dark": "dark-gold",
    "gold": "dark-gold",
    "blue": "light",
    "default": "light",
    "cute": "pink",
    "girl": "pink",
    "navy": "navy-gold",
    "deepblue": "navy-gold",
}

_DEFAULT_PRESET = "light"


def theme_presets() -> dict:
    """返回全部主题预设（复制，防外部改动）。"""
    return {k: dict(v) for k, v in _THEME_PRESETS.items()}


def resolve_preset(name) -> dict:
    """把用户选的主题名解析成完整令牌 dict；未知名退回默认蓝白。"""
    key = str(name or "").strip().lower()
    key = _THEME_ALIASES.get(key, key)
    preset = _THEME_PRESETS.get(key) or _THEME_PRESETS[_DEFAULT_PRESET]
    return dict(preset)


# 兼容旧代码引用的名字（_normalize_theme 的旧实现曾用这两个常量）
_LIGHT_DEFAULTS = {k: v for k, v in _THEME_PRESETS["light"].items() if k != "dark"}
_DARK_DEFAULTS = {k: v for k, v in _THEME_PRESETS["dark-gold"].items() if k != "dark"}


def _normalize_theme(theme) -> dict:
    """把主题配置归一化成完整令牌。

    新约定：配置是**预设名**（``"light" / "dark-gold" / "pink"``）或
    含 ``mode``/``dark`` 键的 dict。旧的逐色 dict（``{"accent": "#xxx"}``）
    仍被接受并覆盖到预设之上，保证向后兼容。
    """
    # 字符串 = 预设名（也可能是旧值 dark/light）
    if isinstance(theme, str):
        return resolve_preset(theme)

    t = dict(theme or {})
    # dict 里带了预设名字段（mode / preset / theme）时以它为基底
    name = t.pop("preset", None) or t.pop("theme", None) or t.pop("mode", None)
    if name is None and t.get("dark"):
        name = "dark"
    if name is not None:
        base = resolve_preset(name)
    else:
        base = dict(_THEME_PRESETS[_DEFAULT_PRESET])
        base["dark"] = bool(t.get("dark", False))
    # 用户显式提供的颜色键覆盖预设；空值与 dark 开关不当作颜色覆盖
    for k, v in t.items():
        if k == "dark" or v in (None, ""):
            continue
        base[k] = v
    if "dark" in t:
        base["dark"] = bool(t["dark"])
    return base


def _temp_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        path = Path(get_astrbot_temp_path())
    except Exception:
        path = Path("/tmp")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        path = Path("/tmp")
    return path


def _new_path(suffix: str) -> str:
    name = f"dota2_card_{int(time.time())}_{uuid.uuid4().hex[:8]}{suffix}"
    return str(_temp_dir() / name)


def _is_valid_image(path: str, min_side: int = 40) -> bool:
    """校验渲染产物确实是一张可用图片。

    t2i 端点偶发返回 0 字节或半截文件；直接发给用户会得到一张裂图，
    因此这里必须实际解码并检查尺寸。纯灰/纯黑图同样视为失败。
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 1024:
            return False
        # 端点失败时会把错误体直接写进 .jpg（实测见过 Cloudflare 502 的 HTML
        # 与 {"code":1,"message":"Timeout 5000ms exceeded"} 的 JSON），
        # 按文件头先挡掉，省得后面 Image.open 抛异常刷日志。
        with open(path, "rb") as handle:
            head = handle.read(16)
        if not (head.startswith(b"\xff\xd8") or head.startswith(b"\x89PNG")
                or head[:6] in (b"GIF87a", b"GIF89a") or head[:4] == b"RIFF"
                or head[:2] == b"BM"):
            return False

        from PIL import Image

        with Image.open(path) as img:
            img.load()
            width, height = img.size
            if width < min_side or height < min_side:
                return False
            sample = img.convert("L").resize((64, 64))
            pixels = list(sample.getdata())
        spread = max(pixels) - min(pixels)
        return spread > 12
    except Exception:
        return False


# 卡片末尾评价区块的标题文案（与 main.py / templates.ANALYSIS_TITLES 一致）。
# 对局叫「综合分析」，其余查询叫「点评」，二者都要触发产物校验。
_ANALYSIS_TITLE = "综合分析"
_REVIEW_TITLE = "点评"
_ANALYSIS_TITLES = (_ANALYSIS_TITLE, _REVIEW_TITLE)
# 退化产物的高度阈值（像素）。实测正常卡片 900~1600px；模板脚本未加载时
# 退化成纯文本 div，高度塌缩到 ~120px 以下。阈值留出足够裕度。
_DEGENERATE_MAX_HEIGHT = 200


def _looks_degenerate(path: str) -> bool:
    """以「高度过矮」识别模板退化，替代原先的暖色底纹占比校验。

    原先靠 ``--analysis-bg`` (#fff8ef) 的暖白占比判定，但底纹已可配置，
    颜色不再能作为退化信号。退化本质是全页未渲染（marked 未执行），
    表现为高度塌缩，因此改用高度判据：既与配色解耦，又保留对
    「疑似模板脚本未加载」产物的回退保护。
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            _, height = img.size
        return height < _DEGENERATE_MAX_HEIGHT
    except Exception:
        return False


# 冷缓存下要下载 60 多张图标，给一个整体预算；超时未拿到的图标走各自的
# 降级路径（网络路径保留原 URL 让浏览器直连、本地路径显示中文名），
# 不能让渲染整体卡住。
_ICON_BUDGET_SECONDS = 8.0


async def prepare_icons(markdown: str) -> None:
    """确保 Markdown 里用到的图标都在本地缓存（缺失的才下载）。"""
    urls = icon_cache.image_urls(markdown)
    if not urls:
        return
    try:
        await icon_cache.ensure(urls, budget=_ICON_BUDGET_SECONDS)
    except Exception as exc:
        logger.warning(f"Dota2 图标缓存准备失败，本次按未缓存处理: {exc}")


async def _render_network(markdown: str, host, theme: Optional[dict] = None) -> Optional[str]:
    """调用网络 t2i 渲染；失败返回 None。

    ``theme`` 用于把可配置配色注入模板（见 :func:`_template`）。
    """
    tmpl = _template(theme)
    if not tmpl:
        return None

    # 图标内联成 data URI：端点里的浏览器只认识内网/自身，访问 Valve CDN
    # 经常超时，而端点等不到图片会自己先抛 5000ms 超时、整次渲染失败。
    inlined = icon_cache.inline_data_uris(markdown)

    try:
        render = getattr(host, "html_render", None)
        if not callable(render):
            return None
        # 注意：render_custom_template 在所有端点失败时会抛 RuntimeError，
        # 且没有本地降级；这里必须捕获并交给调用方走本地渲染。
        produced = await render(tmpl, {"text": inlined}, False, dict(_T2I_OPTIONS))
        if not produced:
            return None
        produced = str(produced)
        if not os.path.exists(produced) or not _is_valid_image(produced):
            logger.warning(f"Dota2 网络 t2i 产物无效，改用本地渲染: {produced}")
            return None
        # 内容级校验（仅判版式是否塌缩）：端点内的浏览器若拿不到 marked，
        # 模板会退化成把 Markdown 当纯文本塞进一个 div，页面高度塌缩、
        # 卡片尾部（含「综合分析」）被裁掉。这种产物是"合法"图片，需看高度。
        # 注意：不再依赖底纹颜色（#fff8ef）判定 —— 底纹已可配置，颜色不等于退化。
        if any(t in markdown for t in _ANALYSIS_TITLES) and _looks_degenerate(produced):
            logger.warning(
                f"Dota2 网络 t2i 产物疑似模板脚本未加载（高度过矮），改用本地渲染: {produced}"
            )
            return None
        return produced
    except Exception as exc:
        logger.warning(f"Dota2 网络 t2i 渲染失败，改用本地渲染: {exc}")
        return None


def _render_local(markdown: str, theme: Optional[dict] = None) -> Optional[str]:
    try:
        from .image_renderer import find_cjk_font, render_text_to_image

        if find_cjk_font() is None:
            logger.warning("Dota2 本地渲染未找到中文字体，图片中的中文可能显示异常。")
        out = _new_path(".png")
        # 图标/头像直接读本地缓存文件绘制（render_card 已预热过一轮）
        render_text_to_image(markdown, out, theme=theme)
        if _is_valid_image(out):
            return out
        logger.warning("Dota2 本地渲染产物校验失败。")
        return None
    except Exception as exc:
        logger.error(f"Dota2 本地渲染失败: {exc}")
        return None


def _resolve_host(host=None):
    """解析可用的 t2i 宿主。

    ``html_render`` 是 ``Star`` 实例方法，而 LLM 工具拿不到插件实例；
    因此默认回退到 AstrBot 的全局 ``html_renderer`` 单例。
    """
    if host is not None and callable(getattr(host, "html_render", None)):
        return host
    try:
        from astrbot.core import html_renderer

        if callable(getattr(html_renderer, "render_custom_template", None)):
            return _GlobalRendererAdapter(html_renderer)
    except Exception:
        pass
    return None


class _GlobalRendererAdapter:
    """把全局 HtmlRenderer 适配成与 ``Star.html_render`` 相同的调用形态。"""

    def __init__(self, renderer):
        self._renderer = renderer

    async def html_render(self, tmpl, data, return_url=False, options=None):
        return await self._renderer.render_custom_template(tmpl, data, return_url, options)


async def render_card(
    markdown: str, host=None, theme: Optional[dict] = None,
    prefer_local: bool = False,
) -> Optional[str]:
    """把 Markdown 渲染成卡片图片，返回本地文件路径；全部失败返回 None。

    ``theme`` 是插件配置里的可配置配色（底纹/主色），两条渲染路径都认：
    网络路径注入模板 CSS 变量，本地路径覆盖对应常量；空 dict 时保持默认外观。

    ``prefer_local=True`` 时**跳过网络 t2i、直接走本地 Pillow 渲染**。
    网络 t2i 端点实测极不稳定（504 / 超时 / 单端点 58 秒慢），在
    ``on_decorating_result`` 这种同步流水线里会把整个响应拖到超时、
    图片发不出去；本地渲染 match 卡 < 1 秒、文本卡也很快，且样式可控。
    网络路径仍作为本地失败后的兜底。
    """
    text = (markdown or "").strip()
    if not text:
        return None

    # 先把用到的图标拉到本地：网络路径需要 data URI、本地路径需要文件
    await prepare_icons(text)

    # 归一化主题令牌（补全浅色/深色默认值），避免渲染器收到空值
    theme = _normalize_theme(theme)

    # 优先本地：跳过网络 t2i（不稳定），仅在本地失败时才退回网络兜底
    if prefer_local:
        path = _render_local(text, theme)
        if path:
            return path

    resolved = _resolve_host(host)
    if resolved is not None:
        path = await _render_network(text, resolved, theme)
        if path:
            return path

    return _render_local(text, theme)
