"""装备图标 / 英雄立绘的本地缓存层。

为什么需要它
------------

卡片里的图标原先直接写成 Valve 官方 CDN 的 URL（``![黑皇杖](https://cdn...png)``），
由**远端 t2i 端点里的浏览器**去加载。实测该浏览器经常拉不到图：

* 图标位置只剩 alt 文本或一片空白，卡片像坏了；
* 端点为了等这些永远不来的图片，自己先超时
  （服务端返回 ``internal server error: Timeout 5000ms exceeded``），
  整次网络渲染失败并降级。

现在改为：插件自己把图标下载到**本地缓存**，渲染时内联成 ``data:`` URI。
网络 t2i 不再需要访问任何外部图片，本地 Pillow 兜底也能直接画出真图标，
两条渲染路径都有图。

缓存位置与刷新
--------------

落盘目录按「插件数据目录 → 插件自带 assets → 系统临时目录」依次探测，
文件名由 URL 推导（``items/black_king_bar.png``、``heroes/antimage.png``）。
缓存只在**文件缺失**时重新下载，因此进程重启后依然有效、也不会反复打 CDN。

图片会被归一化以缩小体积（装备图标量化到 64 色、英雄立绘裁成固定比例），
因为一份 10 人战报要内联 60 多个图标，体积直接决定远端端点能否渲染成功。
"""

from __future__ import annotations

import base64
import io
import os
import re
import time
from pathlib import Path
from typing import Iterable, Optional

from ..compat import logger

# ---------------------------------------------------------------- URL 识别

# 装备图标：https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/items/blink.png
_ITEM_MARKER = "/dota_react/items/"

# 英雄立绘。legacy 路径是竖版（235x272，最贴合「左侧竖跨三行」的版式），
# dota_react 路径是横版（256x144，新英雄没有竖版时兜底）。
_HERO_VERT_MARKER = "/apps/dota2/images/heroes/"
_HERO_REACT_MARKER = "/dota_react/heroes/"
_HERO_REACT_BASE = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/"
)
_HERO_VERT_BASE = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/heroes/"

# 横幅用途标记（见 core.hero_icons.BANNER_QUERY）。带此标记的 URL 保留横版
# 原始比例、落到 banners/ 目录，绝不与玩家行头像的竖版归一化混用。
_BANNER_QUERY = "?kind=banner"

# 横幅是照片级画面，量化到 64 色会出现明显色带；128 色在「压暗 42% 后当底图」
# 的用法下已看不出差别，同时把 256x144 的 PNG 控制在几十 KB 以内
# （网络 t2i 要把图内联成 base64，体积会再放大 1.37 倍）。
_BANNER_COLORS = 128

_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

# 立绘统一裁成这个比例（与 legacy 竖版一致），再缩到 _HERO_SIZE。
# 版式上占 104x120 CSS 像素，这里存 2 倍图供高分屏使用。
_HERO_ASPECT = 235 / 272
_HERO_SIZE = (208, 240)

# 装备图标原图就是 88x64；量化到 64 色后单张约 2.4KB（原图约 11.7KB），
# 60 张从 690KB 降到 150KB —— 内联进 t2i 请求体后差距会被 base64 再放大 1.37 倍。
_ICON_COLORS = 64

# 同一张图失败后多久之内不再重试（避免每次渲染都去撞一个坏 URL）
_FAIL_TTL = 300.0

_MEM: dict[str, Optional[Path]] = {}
_DATA_URI: dict[str, str] = {}
_FAILED: dict[str, float] = {}
_CACHE_DIR: Optional[Path] = None


# ---------------------------------------------------------------- 路径

def is_icon_url(url: str) -> bool:
    """判断 URL 是否属于本模块管理的图标（装备图标 / 英雄立绘）。"""
    text = str(url or "")
    return _ITEM_MARKER in text or _HERO_VERT_MARKER in text or _HERO_REACT_MARKER in text


def _asset_name(url: str) -> str:
    """取 URL 的文件名（去掉 query），作为缓存文件名。"""
    name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return name or "icon"


def hero_asset_key(url: str) -> str:
    """英雄立绘 URL → 英雄资源名（``antimage``）。

    竖版 ``antimage_vert.jpg``、横版 ``antimage.png``、旧版 ``antimage_full.png``
    都归到同一个 key，因此同一个英雄只会占一份缓存文件 ——
    无论最终是从哪个源站路径下载成功的。
    """
    name = _asset_name(url)
    if "." in name:
        name = name.rsplit(".", 1)[0]
    for suffix in ("_vert", "_full", "_lg"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name or "hero"


def cache_relpath(url: str) -> Path:
    """URL → 缓存内的相对路径。

    英雄立绘一律存成 ``heroes/<key>.png``：无论源站给的是竖版 JPEG 还是
    横版 PNG，归一化后都是同一套 PNG，渲染层不用再关心来源格式。

    横幅（带 ``?kind=banner``）单独存 ``banners/<key>.png``：它**不**做竖版
    归一化，必须与头像分开缓存，否则两者互相覆盖。
    """
    if _ITEM_MARKER in url:
        return Path("items") / _asset_name(url)
    if _BANNER_QUERY in url:
        return Path("banners") / f"{hero_asset_key(url)}.png"
    return Path("heroes") / f"{hero_asset_key(url)}.png"


def candidate_urls(url: str) -> list[str]:
    """同一张图的可选下载地址，按优先级排列。

    竖版立绘（``heroes/<key>_vert.jpg``）最贴合卡片左侧的竖长版式，但
    新英雄（破晓辰星 / 玛西 / 琼英碧灵 / 兽）没有这张图，会 404；
    此时退回官方横版立绘（``dota_react/heroes/<key>.png``），
    归一化时再裁成竖版比例，两条路径在版式上完全一致。
    """
    urls = [url]
    if _HERO_VERT_MARKER in url and _HERO_REACT_MARKER not in url:
        key = hero_asset_key(url)
        urls.append(f"{_HERO_REACT_BASE}{key}.png")
    elif _BANNER_QUERY in url:
        # 横版立绘缺失（罕见）时退回竖版：渲染层按 cover 裁切，源图比例
        # 不影响版式正确性，只是构图不如横版舒展。
        key = hero_asset_key(url)
        urls.append(f"{_HERO_VERT_BASE}{key}_vert.jpg{_BANNER_QUERY}")
    return list(dict.fromkeys(urls))


def _probe_cache_dirs() -> list[Path]:
    """候选缓存目录，按优先级排列。

    1. AstrBot 插件数据目录（插件升级不会覆盖，最干净）；
    2. 插件自带的 ``assets/icon_cache``（没有 AstrBot 环境时用）；
    3. 系统临时目录（前两者都不可写时的最后手段）。
    """
    dirs: list[Path] = []
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        dirs.append(Path(get_astrbot_data_path()) / "plugin_data" / "dota2assistant" / "icons")
    except Exception:
        pass
    dirs.append(Path(__file__).resolve().parent.parent / "assets" / "icon_cache")
    try:
        import tempfile

        dirs.append(Path(tempfile.gettempdir()) / "dota2_icon_cache")
    except Exception:
        pass
    return dirs


def cache_dir() -> Path:
    """返回可写的缓存根目录（进程内只探测一次）。"""
    global _CACHE_DIR
    if _CACHE_DIR is not None:
        return _CACHE_DIR

    for candidate in _probe_cache_dirs():
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_test"
            probe.write_bytes(b"")
            probe.unlink()
            _CACHE_DIR = candidate
            return candidate
        except Exception:
            continue

    # 全部不可写：退化为临时目录但不建子目录（读写都会失败，只是不抛异常）
    _CACHE_DIR = Path("/tmp")
    return _CACHE_DIR


def local_path(url: str) -> Optional[Path]:
    """已缓存的本地文件路径；未缓存返回 None（**不触发下载**，本地渲染是同步的）。"""
    if not is_icon_url(url):
        return None
    if url in _MEM:
        return _MEM[url]

    path = cache_dir() / cache_relpath(url)
    resolved = path if path.exists() and path.stat().st_size > 0 else None
    _MEM[url] = resolved
    return resolved


def data_uri(url: str) -> str:
    """已缓存的图片 → ``data:`` URI；未缓存返回空串。

    内联后网络 t2i 的浏览器无需访问外网，这是「每次都有效渲染」的关键。
    """
    if url in _DATA_URI:
        return _DATA_URI[url]

    path = local_path(url)
    if path is None:
        return ""
    try:
        raw = path.read_bytes()
    except Exception:
        return ""

    suffix = path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    _DATA_URI[url] = uri
    return uri


# ---------------------------------------------------------------- Markdown 解析

def image_urls(markdown: str) -> list[str]:
    """取出 Markdown 里所有图片 URL（去重，保持顺序）。"""
    seen: list[str] = []
    for match in _MD_IMAGE_RE.finditer(markdown or ""):
        url = match.group(2)
        if url not in seen:
            seen.append(url)
    return seen


def inline_data_uris(markdown: str) -> str:
    """把 Markdown 里已缓存的图标 URL 换成内联 data URI。

    未缓存的 URL 原样保留：网络路径下浏览器仍可尝试直连 CDN，
    本地路径下渲染器会把图片降级成 alt 文本。
    """
    def repl(match: re.Match) -> str:
        alt, url = match.group(1), match.group(2)
        uri = data_uri(url)
        return f"![{alt}]({uri})" if uri else match.group(0)

    return _MD_IMAGE_RE.sub(repl, markdown or "")


# ---------------------------------------------------------------- 下载

def _image_from_bytes(raw: bytes):
    """校验并解码图片字节；不是图片（例如 HTML 错误页）返回 None。"""
    if not raw or len(raw) < 120:
        return None
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img.load()
        return img
    except Exception:
        return None


def _center_crop(img, aspect: float):
    """按目标宽高比居中裁剪（新英雄只有横版立绘，裁中间那一块）。"""
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    target_w = min(w, int(round(h * aspect)))
    target_h = min(h, int(round(w / aspect)))
    left = (w - target_w) // 2
    top = int((h - target_h) * 0.25)  # 稍微偏上，保住英雄的脸
    return img.crop((left, top, left + target_w, top + target_h))


def _normalize(url: str, raw: bytes) -> Optional[bytes]:
    """把下载到的原图归一化成小体积 PNG；无法处理时返回原字节。"""
    img = _image_from_bytes(raw)
    if img is None:
        return None

    try:
        from PIL import Image

        if _ITEM_MARKER in url:
            out = img.convert("RGBA")
            colors = _ICON_COLORS
        elif _BANNER_QUERY in url:
            # 横幅保留横版原始比例，只做色彩量化压缩体积，**不裁切、不缩放**
            out = img.convert("RGBA")
            colors = _BANNER_COLORS
        else:
            out = _center_crop(img.convert("RGBA"), _HERO_ASPECT)
            out = out.resize(_HERO_SIZE, Image.LANCZOS)
            colors = _ICON_COLORS

        buf = io.BytesIO()
        try:
            # 量化：装备图标/立绘都是色块+透明的图形，肉眼几乎无差
            method = Image.FASTOCTREE if colors == _ICON_COLORS else Image.MEDIANCUT
            quantized = out.quantize(colors=colors, method=method)
            quantized.save(buf, "PNG", optimize=True)
        except Exception:
            buf = io.BytesIO()
            out.save(buf, "PNG", optimize=True)
        normalized = buf.getvalue()
    except Exception:
        return raw

    # 归一化反而更大（少见）时保留原图，不做无意义的放大
    return normalized if 0 < len(normalized) < len(raw) else raw


async def _download(url: str, timeout: float) -> Optional[bytes]:
    """下载单个 URL，返回可用的图片字节；失败返回 None。"""
    try:
        import aiohttp
    except ModuleNotFoundError:
        return None

    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.debug(f"Dota2 图标下载失败 HTTP {response.status}: {url}")
                    return None
                return await response.read()
    except Exception as exc:
        logger.debug(f"Dota2 图标下载异常 {url}: {exc}")
        return None


async def _fetch_and_store(url: str, timeout: float) -> Optional[Path]:
    """下载 → 归一化 → 原子落盘（含备用地址与别名登记）。"""
    for candidate in candidate_urls(url):
        raw = await _download(candidate, timeout)
        if raw is None:
            continue

        payload = _normalize(candidate, raw)
        if payload is None:
            continue

        path = cache_dir() / cache_relpath(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, path)
        except Exception as exc:
            logger.debug(f"Dota2 图标缓存写入失败 {path}: {exc}")
            return None

        _MEM[url] = path
        # 备用地址同样登记：同一英雄的横版/竖版 URL 不会重复下载
        if candidate != url:
            _MEM[candidate] = path
        _DATA_URI.pop(url, None)
        _DATA_URI.pop(candidate, None)
        return path

    return None


async def ensure(
    urls: Iterable[str],
    *,
    budget: float = 6.0,
    per_request_timeout: float = 6.0,
    concurrency: int = 8,
) -> dict[str, Path]:
    """确保这批 URL 都已缓存（缺失的才下载），返回 ``{url: 本地路径}``。

    ``budget`` 是整体等待上限：冷缓存下 60 多个图标不可能无限等，
    超时后未拿到的图标走各自的降级路径（网络路径直连 CDN、本地路径显示 alt 文本），
    绝不让渲染整体卡死。
    """
    import asyncio

    wanted = [u for u in dict.fromkeys(urls or []) if is_icon_url(u)]
    ready: dict[str, Path] = {}
    if not wanted:
        return ready

    pending: list[str] = []
    now = time.monotonic()
    for url in wanted:
        cached = local_path(url)
        if cached is not None:
            ready[url] = cached
            continue
        failed_at = _FAILED.get(url)
        if failed_at is not None and now - failed_at < _FAIL_TTL:
            continue
        pending.append(url)

    if not pending:
        return ready

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def worker(url: str) -> tuple[str, Optional[Path]]:
        async with semaphore:
            return url, await _fetch_and_store(url, per_request_timeout)

    tasks = [asyncio.ensure_future(worker(u)) for u in pending]
    try:
        done, still_running = await asyncio.wait(tasks, timeout=budget)
    except Exception as exc:
        logger.warning(f"Dota2 图标缓存下载异常: {exc}")
        done, still_running = set(), set()

    for task in still_running:
        task.cancel()
    if still_running:
        logger.info(
            f"Dota2 图标缓存超时（{budget}s）：{len(still_running)}/{len(pending)} "
            "张未下载完，本次先按未缓存处理。"
        )

    for task in done:
        try:
            url, path = task.result()
        except Exception:
            continue
        if path is not None:
            ready[url] = path
        else:
            _FAILED[url] = time.monotonic()

    missing = [u for u in pending if u not in ready]
    if ready:
        logger.debug(f"Dota2 图标缓存就绪 {len(ready)} 张（新下载 {len(ready) - (len(wanted) - len(pending))} 张）。")
    if missing:
        logger.debug(f"Dota2 图标缓存缺失 {len(missing)} 张（将使用兜底渲染）。")
    return ready


def reset_caches() -> None:
    """清空进程内缓存（测试用）。"""
    _MEM.clear()
    _DATA_URI.clear()
    _FAILED.clear()
