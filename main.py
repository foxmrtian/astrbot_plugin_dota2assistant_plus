from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

_plugin_dir = Path(__file__).parent

# 注意：这里刻意 *不* 做 sys.path.insert + 顶层名导入（v0.3.x 之前的老写法）。
# AstrBot 以 data.plugins.<插件目录名>.main 加载本插件，热重载时
# star_manager._purge_modules() 只清理带该前缀的模块。一旦把插件目录塞进
# sys.path，`from core.opendota import ...` 会额外注册一套顶层模块
# （core.opendota / tools.match_tool ...），它们不匹配该前缀，永远不会被清理，
# 于是热重载后跑的仍是首次加载的旧代码（表现为：工具照旧返回文本、英雄名不变）。
# 统一用包相对导入，模块名落在 data.plugins.<目录名>.* 下，才能被正确重载。
from .compat import AstrMessageEvent, Context, Star, filter, logger  # noqa: E402
from .core.opendota import OpenDotaClient  # noqa: E402
from .core.store import DotaStore  # noqa: E402
from .core.templates import (  # noqa: E402
    analysis_title,
    detect_kind,
    markdown_to_plain,
)
from .tools.delivery import pop_cards, pop_focus_hero  # noqa: E402

# 从比赛卡片首行解析 match_id：``# 比赛详情 #8831125663`` → 8831125663
_MATCH_ID_RE = re.compile(r"^#\s*比赛详情\s+#(\d+)", re.M)

# on_decorating_result 钩子里等待卡片生成的上限（秒）。
#
# 钩子同步等待过久会超时、事件被提前关闭，`set_result` 随之失效 —— 图渲染好了
# 却发不出去。实测 GLM-4-Flash 写一段点评只要 2~3 秒，因此 8 秒足够覆盖正常
# 情况；真正的慢（模型卡住/录像总结）则交给后台 `send_message` 主动推送。
_CARD_HOOK_BUDGET = 8.0

# 文本转图片渲染器（参考 outputpro 插件 T2IStep）
try:
    from astrbot.core.message.components import Image, Plain
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    Plain = None  # type: ignore

_ASSETS_DIR = Path(__file__).parent / "assets"

# LLM 点评过长时截断，避免图片被一段长文拉得过高
_MAX_ANALYSIS_CHARS = 1200


class Dota2AssistantPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)

        plugin_config = config or {}
        self.enable_fallback = plugin_config.get("enable_fallback_commands", True)
        timeout = plugin_config.get("request_timeout", 15)
        steam_api_key = plugin_config.get("steam_api_key", "")

        # 赛后总结：插件自己调本机对话大模型生成，附在图片最下方
        self.enable_llm_summary = plugin_config.get("enable_llm_summary", True)
        self.summary_provider_id = plugin_config.get("summary_provider_id", "")
        # 默认 180 秒：推理类模型（如 DeepSeek-R1）写三点总结实测约 88 秒，
        # 早期默认 30 秒会几乎必定超时，导致总结退回主模型文字。
        self.summary_timeout = plugin_config.get("summary_timeout", 180)
        # 默认 500 字：三点总结要求「只点评每方最突出/最落后的 1-2 人」，
        # 500 字足够写清；再长只会让人人写一句、失去重点。
        # 注意这是**兜底截断**，正常输出应当不超过它（提示词已要求 ≤500 字）。
        self.summary_max_chars = plugin_config.get("summary_max_chars", 500)

        # 录像解析驱动的赛后总结。具体某一局的卡片不再用「卡片文字」喂模型，
        # 而是向 OpenDota 申请录像解析、用解析出的团战/关键目标/对线/经济曲线
        # 写赛后总结。解析约需数分钟到十分钟，因此走后台任务：
        # 先发「录像正在解析中」提示，解析完成后把对战数据+点评合成一张卡补发。
        self.enable_replay_summary = plugin_config.get("enable_replay_summary", True)
        # 解析轮询总时长上限（秒）。OpenDota 官方说数分钟、实测常见 5~10 分钟，
        # 默认 720 秒留足余量；超时则放弃点评、只发对战卡片。
        self.replay_parse_timeout = int(plugin_config.get("replay_parse_timeout", 720))

        # on_decorating_result 钩子里等待卡片生成的上限（秒）。
        #
        # 钩子同步等待过久会超时、本轮事件被提前关闭，set_result 随之失效
        # —— 「图渲染好了却发不出去」就是这个原因。窗口内完成走 set_result
        # （单条消息，体验最好）；超时则转到后台用 send_message 主动推送，
        # 不依赖本轮事件生命周期，因此必定送达。实测 GLM-4-Flash 写点评只要
        # 2~3 秒，默认 8 秒足够；调大只会让慢的情况下多等一会儿。
        self.card_hook_budget = float(
            plugin_config.get("card_hook_budget", _CARD_HOOK_BUDGET)
        )

        # 图片输出开关。默认开启（保持原有行为）；关闭后不再渲染卡片图片，
        # 直接把 Markdown 文本发出去 —— 有些平台/用户更需要可复制、可搜索的
        # 文字，图片里的数据没法选中复制。
        self.enable_image_output = plugin_config.get("enable_image_output", True)

        # 优先本地渲染。网络 t2i 端点极不稳定（504 / 超时 / 单端点 58 秒慢），
        # 在同步 hook 里会把响应拖到超时、图片发不出去（真实事故）。
        # 本地 Pillow 渲染 match 卡 < 1 秒、样式可控，默认开启；网络 t2i 仅作兜底。
        self.prefer_local_render = plugin_config.get("prefer_local_render", True)

        # 卡片主题：新版用一个下拉框选预设名（light / dark-gold / pink），
        # 由 core.card_renderer 的三套预设统一成完整令牌，两条渲染路径一致。
        # 兼容旧配置：若用户仍存了逐色 dict 的 ``card_theme``，也照常接受。
        preset = plugin_config.get("card_theme_preset")
        legacy = plugin_config.get("card_theme")
        if preset:
            # 预设名优先；旧逐色 dict（若有）作为覆盖叠加在预设之上
            self.card_theme = {"preset": preset, **(legacy if isinstance(legacy, dict) else {})}
        else:
            self.card_theme = legacy or {}

        # 数据目录
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "dota2assistant"
        except Exception:
            data_dir = Path(__file__).parent / "data"

        # 创建 Valve 客户端（如果配置了 API Key）
        valve_client = None
        if steam_api_key:
            from .core.valve_client import ValveClient
            valve_client = ValveClient(api_key=steam_api_key, timeout=timeout)
            logger.info("Valve API 客户端已启用。")

        self.client = OpenDotaClient(timeout=timeout, valve_client=valve_client)
        self.store = DotaStore(data_dir / "dota2.db")
        self.hero_map = _load_json_map(_ASSETS_DIR / "heroes.json")
        self.item_name_map = _load_json_map(_ASSETS_DIR / "items.json")

        self._register_llm_tools()

        try:
            from .core.image_renderer import find_cjk_font

            if find_cjk_font() is None:
                logger.warning(
                    "未找到可用的中文字体，本地兜底渲染的中文可能显示为方块；"
                    "网络 t2i 正常时不受影响。"
                )
        except Exception:
            pass

        logger.info(f"Dota2 助手插件已加载（英雄映射 {len(self.hero_map)} 条，物品映射 {len(self.item_name_map)} 条）。")

    def _register_llm_tools(self) -> None:
        from .tools.hero_build_tool import DotaHeroBuildTool
        from .tools.hero_tool import DotaHeroListTool, DotaHeroTool
        from .tools.item_tool import DotaItemTool
        from .tools.live_tool import DotaLiveTool
        from .tools.match_tool import DotaMatchTool
        from .tools.my_profile_tool import DotaMyProfileTool
        from .tools.player_tool import DotaPlayerTool
        from .tools.pro_tool import DotaProTool

        tools = (
            DotaPlayerTool(client=self.client),
            DotaMyProfileTool(client=self.client, store=self.store),
            DotaHeroTool(client=self.client, hero_map=self.hero_map),
            DotaHeroListTool(client=self.client),
            DotaHeroBuildTool(client=self.client, hero_map=self.hero_map),
            DotaItemTool(client=self.client, item_name_map=self.item_name_map),
            DotaMatchTool(client=self.client),
            DotaLiveTool(client=self.client),
            DotaProTool(client=self.client),
        )

        add_llm_tools = getattr(self.context, "add_llm_tools", None)
        if callable(add_llm_tools):
            add_llm_tools(*tools)
            return

        tool_manager = getattr(getattr(self.context, "provider_manager", None), "llm_tools", None)
        func_list = getattr(tool_manager, "func_list", None)
        if isinstance(func_list, list):
            func_list.extend(tools)
            return

        logger.warning("当前 AstrBot Context 不支持 LLM Tool 注册，已跳过 Dota2 工具注册。")

    # --- 图片输出：发送前钩子 ---

    @filter.on_decorating_result(priority=30)
    async def _decorate_dota_card(self, event: AstrMessageEvent):
        """把本轮工具查到的数据 + 模型点评合成一张图片发出。"""
        cards = pop_cards(event)
        if not cards:
            return  # 本轮与 Dota2 卡片无关，放过其它消息

        # 先给用户一个「已收到」反馈；后续成功/失败再追加对应表情。
        try:
            from .core import emoji_feedback

            await emoji_feedback.on_query_start(event)
        except Exception:
            pass

        try:
            from .core.image_renderer import is_match_card

            match_id = self._card_match_id(cards) if any(is_match_card(c) for c in cards) else 0

            # ── 具体某一局：用录像解析数据写赛后总结（异步，约数分钟）──
            # 已解析：生成总结→出图（一条消息，含点评）。
            # 未解析：本轮先发「录像正在解析中」提示，起后台任务在解析完成后
            #         把对战数据+点评合成一张卡补发（对战与点评始终同一张卡）。
            if match_id and getattr(self, "enable_replay_summary", True):
                focus = pop_focus_hero(event) if event is not None else ""
                handled = await self._deliver_match_card(event, match_id, cards, focus)
                if handled:
                    return
                # handled=False 表示录像总结被关闭/不可用，退回原有一段式流程

            focus_hero = pop_focus_hero(event) if event is not None else ""
            await self._deliver_with_budget(
                event, lambda: self._produce_cards(event, cards, focus_hero)
            )
        except Exception:
            await self._emit_feedback(event, success=False)
            raise

    @staticmethod
    def _card_match_id(cards: list[str]) -> int:
        """从比赛卡片首行解析 match_id；不是比赛卡返回 0。"""
        for card in cards or []:
            m = _MATCH_ID_RE.search(card or "")
            if m:
                try:
                    return int(m.group(1))
                except (TypeError, ValueError):
                    return 0
        return 0

    async def _deliver_match_card(self, event, match_id: int, cards: list[str],
                                  focus_hero: str) -> bool:
        """处理「具体某一局」的录像解析赛后总结。

        返回 True 表示已接管本轮输出（同步出图，或已发「解析中」提示并起了
        后台任务），调用方直接 return；返回 False 表示录像总结被关闭或
        不可用，调用方走原有的一段式点评流程。
        """
        from .core.opendota import get_registered_match

        match = get_registered_match(match_id)
        if match is None:
            try:
                match = await self.client.get_match_detail(match_id)
            except Exception as exc:
                logger.warning(f"读取比赛 #{match_id} 失败，退回原有点评: {exc}")
                return False
        if match is None:
            return False

        if match.parsed:
            # 已解析：用录像数据生成总结并出图（对战+点评同一张卡）。
            # 与其它卡片走同一条「有界等待 + 超时转后台推送」的投递路径，
            # 避免录像总结偏慢时把钩子拖到超时、卡片发不出去。
            async def _produce():
                analysis = await self._replay_analysis(match, cards, event, focus_hero)
                image_path, markdown = await self._render_card_image(cards, analysis)
                return image_path, markdown, analysis, "录像解析"

            await self._deliver_with_budget(event, _produce)
            return True

        # 未解析：本轮先发提示，后台解析完成后补发完整卡片。
        await self._send_text(event, f"比赛 #{match_id} 录像正在解析中，请稍候…")
        self._spawn(self._run_replay_summary(event, match_id, cards, focus_hero))
        return True

    async def _replay_analysis(self, match, cards, event, focus_hero: str) -> str:
        """调用 summarizer 用录像数据生成赛后总结；失败返回空串。"""
        try:
            from .core.summarizer import summarize_match_replay

            return await summarize_match_replay(
                self, match, cards, event, focus_hero=focus_hero
            )
        except Exception as exc:
            logger.error(f"Dota2 录像总结生成异常: {exc}")
            return ""

    async def _run_replay_summary(self, event, match_id: int, cards: list[str],
                                  focus_hero: str) -> None:
        """后台任务：等待录像解析完成 → 生成总结 → 合成完整卡片补发。

        全程独立 try/except，任何失败都不能影响主请求；超时则补发一张
        只有对战数据、没有点评的卡片（用户至少能拿到数据）。
        """
        try:
            timeout = getattr(self, "replay_parse_timeout", 720)
            match = await self.client.wait_for_parse(match_id, timeout=timeout)
            analysis = ""
            if match is not None:
                analysis = await self._replay_analysis(match, cards, event, focus_hero)
            else:
                logger.warning(f"比赛 #{match_id} 解析超时，补发无点评卡片。")
            # 解析完成（或超时）后，把对战数据+点评合成一张卡，主动推给原会话。
            await self._send_card(event, cards, analysis, "录像解析", followup=True)
        except Exception as exc:
            logger.error(f"Dota2 录像解析后台任务异常: {exc}")

    async def _send_text(self, event, text: str) -> None:
        """把一段纯文本作为本轮回复发出。"""
        try:
            event.set_result(event.plain_result(text))
            await self._emit_feedback(event, success=True)
        except Exception:
            pass

    async def _send_card(self, event, cards: list[str], analysis: str, source: str,
                         followup: bool = False) -> None:
        """把「数据卡片 + 点评」渲染成图片并发出。

        ``followup=False``：作为本轮回复（``set_result``）；
        ``followup=True``：作为后台任务的后续消息，主动推到原会话
        （``Context.send_message`` + ``unified_msg_origin``）。
        """
        image_path, markdown = await self._render_card_image(cards, analysis)
        await self._emit_image(event, image_path, markdown, followup=followup)

    async def _render_card_image(self, cards: list[str], analysis: str):
        """合成 Markdown 并渲染成图片；返回 ``(图片路径或 None, Markdown)``。"""
        markdown = self._compose_card_markdown(cards, analysis)

        if not getattr(self, "enable_image_output", True) or Image is None:
            return None, markdown

        try:
            from .core.card_renderer import render_card

            image_path = await render_card(
                markdown, host=self, theme=getattr(self, "card_theme", {}),
                prefer_local=getattr(self, "prefer_local_render", True),
            )
        except Exception as exc:
            logger.error(f"Dota2 卡片渲染异常: {exc}")
            image_path = None

        if not image_path:
            logger.warning("Dota2 卡片渲染失败，降级为文本输出。")
        return image_path, markdown

    async def _emit_image(self, event, image_path, markdown: str,
                          followup: bool = False) -> None:
        """把渲染结果送出：本轮回复（``set_result``）或后台推送。"""
        if not image_path:
            if followup:
                await self._push_text(event, markdown_to_plain(markdown))
            else:
                event.set_result(event.plain_result(markdown_to_plain(markdown)))
                await self._emit_feedback(event, success=True)
            return

        track = getattr(event, "track_temporary_local_file", None)
        if callable(track):
            try:
                track(str(image_path))
            except Exception:
                pass

        if followup:
            await self._push_image(event, str(image_path))
        else:
            event.set_result(event.chain_result([Image.fromFileSystem(str(image_path))]))
            await self._emit_feedback(event, success=True)

    async def _write_analysis(self, event, cards: list[str], focus_hero: str):
        """生成赛后点评；返回 ``(点评文本, 来源)``。"""
        analysis = ""
        source = ""
        # 1) 优先：插件自己调配置的大模型写赛后总结
        try:
            from .core.summarizer import summarize

            analysis = await summarize(self, cards, event, focus_hero=focus_hero)
            if analysis:
                source = "模型生成"
        except Exception as exc:
            logger.error(f"Dota2 赛后总结生成异常: {exc}")

        # 2) 兜底：模型不可用/超时时，用 Agent 本轮的回复文字
        if not analysis:
            analysis = self._extract_analysis(event)
            if analysis:
                source = "Agent 回复"
                try:
                    from .core.summarizer import format_analysis

                    analysis = format_analysis(
                        analysis, cards,
                        max_chars=getattr(self, "summary_max_chars", 500),
                        kind=detect_kind(cards),
                    )
                except Exception as exc:
                    logger.warning(f"Dota2 Agent 回复整理失败，按原文输出: {exc}")
        return analysis, source

    async def _produce_cards(self, event, cards: list[str], focus_hero: str):
        """「写点评 + 出图」的完整产物；返回 ``(图片路径, Markdown, 点评, 来源)``。"""
        analysis, source = await self._write_analysis(event, cards, focus_hero)
        image_path, markdown = await self._render_card_image(cards, analysis)
        return image_path, markdown, analysis, source

    async def _deliver_with_budget(self, event, producer) -> None:
        """在钩子里**有界等待**卡片生成，超时则转后台主动推送。

        这是「卡片经常发不出去」的根治点。``on_decorating_result`` 钩子若长时间
        同步等待（调模型写点评 + 渲染动辄数秒到数分钟），钩子会超时、本轮事件被
        提前关闭，此时 ``set_result`` 静默失效 —— 图片明明渲染好了却发不出去。

        因此钩子只等 ``card_hook_budget`` 秒：

        * 窗口内完成 → 照旧 ``set_result``（单条消息，体验最好）；
        * 超时 → 立刻让出钩子，改由后台任务 ``context.send_message`` 主动推送。
          后者不依赖本轮事件的生命周期，事件被关闭也照样送达。
        """
        budget = float(getattr(self, "card_hook_budget", _CARD_HOOK_BUDGET))
        task = self._spawn(producer())
        try:
            image_path, markdown, _analysis, _source = await asyncio.wait_for(
                asyncio.shield(task), timeout=budget,
            )
        except asyncio.TimeoutError:
            logger.info(
                f"Dota2 卡片生成超过 {budget:.0f}s 钩子预算，转为后台主动推送。"
            )
            # shield 保证 task 未被取消，后台继续跑完并推送
            self._spawn(self._push_generated(event, task))
            self._clear_result(event)
            return
        except Exception as exc:
            logger.error(f"Dota2 卡片生成失败: {exc}")
            event.set_result(event.plain_result("卡片生成失败，请稍后再试。"))
            await self._emit_feedback(event, success=False)
            return

        await self._emit_image(event, image_path, markdown, followup=False)

    async def _push_generated(self, event, task) -> None:
        """后台等待卡片生成完成，再主动推送到原会话（绕开事件生命周期）。"""
        try:
            image_path, markdown, _analysis, _source = await task
        except Exception as exc:
            logger.error(f"Dota2 后台卡片生成异常: {exc}")
            await self._push_text(event, "卡片生成失败，请稍后再试。")
            return
        if image_path:
            if not await self._push_image(event, str(image_path)):
                logger.error("Dota2 后台推送卡片失败，本轮卡片可能未送达。")
        else:
            await self._push_text(event, markdown_to_plain(markdown))

    @staticmethod
    def _clear_result(event) -> None:
        """清空本轮结果，避免占位文本与卡片重复（空链＝不发消息）。"""
        try:
            event.set_result(event.chain_result([]))
        except Exception:
            pass

    def _spawn(self, coro) -> "asyncio.Future":
        """创建后台任务并**保留强引用**。

        asyncio 的事件循环只对任务持弱引用：``create_task`` / ``ensure_future``
        的返回值若没人接手，任务可能在跑完前被 GC 回收 —— 表现就是「后台补发
        偶发不执行」。这里统一登记到一个集合，完成后自动移除。
        """
        task = asyncio.ensure_future(coro)
        tasks = getattr(self, "_bg_tasks", None)
        if tasks is None:
            tasks = set()
            self._bg_tasks = tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def _push_image(self, event, image_path: str) -> bool:
        """后台任务：把图片主动推到原会话；返回是否推送成功。

        ``context.send_message`` 不依赖本轮事件的 ``set_result``，因此即使
        ``on_decorating_result`` 钩子超时、事件被提前关闭，卡片仍能送达。
        """
        ok = False
        try:
            from .compat import MessageChain

            if MessageChain is None:
                logger.error("Dota2 补发卡片失败：当前 AstrBot 未提供 MessageChain。")
                return False
            if Image is None:
                logger.error("Dota2 补发卡片失败：消息组件 Image 不可用。")
                return False
            chain = MessageChain([Image.fromFileSystem(image_path)])
            ok = await self.context.send_message(event.unified_msg_origin, chain)
            if not ok:
                logger.warning("Dota2 补发卡片失败：未找到匹配的消息平台。")
        except Exception as exc:
            logger.error(f"Dota2 补发卡片图片异常: {exc}")
            return False

        # 表情反馈放在主 try 之外、且自带兜底：它失败不能把「推送成功」判成失败，
        # 也不能把异常抛给后台任务（那会变成「Task exception was never retrieved」）
        if ok:
            try:
                await self._emit_feedback(event, success=True)
            except Exception as exc:
                logger.debug(f"Dota2 推送后表情反馈失败: {exc}")
        return bool(ok)

    async def _push_text(self, event, text: str) -> bool:
        """后台任务：把文本主动推到原会话；返回是否推送成功。"""
        ok = False
        try:
            from .compat import MessageChain

            if MessageChain is None:
                logger.error("Dota2 补发文本失败：当前 AstrBot 未提供 MessageChain。")
                return False
            chain = MessageChain().message(text)
            ok = await self.context.send_message(event.unified_msg_origin, chain)
            if not ok:
                logger.warning("Dota2 补发文本失败：未找到匹配的消息平台。")
        except Exception as exc:
            logger.error(f"Dota2 补发文本异常: {exc}")
            return False

        if ok:
            try:
                await self._emit_feedback(event, success=True)
            except Exception as exc:
                logger.debug(f"Dota2 推送后表情反馈失败: {exc}")
        return bool(ok)

    async def _emit_feedback(self, event: AstrMessageEvent, success: bool) -> None:
        """发送表情反馈；失败只记日志，不能影响主流程。"""
        try:
            from .core import emoji_feedback

            if success:
                await emoji_feedback.on_query_success(event)
            else:
                await emoji_feedback.on_query_failure(event)
        except Exception:
            pass

    def _extract_analysis(self, event: AstrMessageEvent) -> str:
        """取出 LLM 本轮回复中的纯文本，作为卡片底部的「综合分析」。"""
        try:
            result = event.get_result()
            if result is None:
                return ""
            # 按 Plain 组件分段，用空行连接，保留段落结构（get_plain_text 会用空格压平换行）
            parts = []
            for comp in result.chain:
                if isinstance(comp, Plain):
                    text = (comp.text or "").strip()
                    if text:
                        parts.append(text)
            text = "\n\n".join(parts).strip()
        except Exception:
            return ""

        # 图片直发等场景可能残留标记，清掉换行噪音
        if len(text) > _MAX_ANALYSIS_CHARS:
            text = text[:_MAX_ANALYSIS_CHARS].rstrip() + "…"
        return text

    def _compose_card_markdown(self, cards: list[str], analysis: str) -> str:
        """拼接最终 Markdown：数据卡片在上，LLM 评价固定放在最下面。

        评价区块的标题随查询种类切换：**具体某一局**是赛后复盘，叫
        「综合分析」；其余查询（近期战绩 / 英雄 / 物品 / 实时 / 职业等）
        叫「点评」—— 它们对的是上方那份列表，说「赛后」并不成立。
        两条渲染路径与产物校验都同时认这两个标题（见 ANALYSIS_TITLES）。
        """
        blocks = [c.strip() for c in cards if c and c.strip()]
        if analysis:
            title = analysis_title(detect_kind(cards))
            blocks.append(f"## {title}\n\n{analysis}")
        return "\n\n".join(blocks)

    def _extract_output_mode(self, message_str: str) -> tuple[str, bool]:
        """从消息中提取输出模式标志，默认返回图片。

        返回 (清理后的消息, 是否启用图片输出)。
        --text: 强制文本输出
        --image: 强制图片输出（默认行为，可省略）
        """
        msg = message_str.strip()
        # 默认值跟随配置开关：关闭图片输出后，连 /dota 命令也默认走文本，
        # 否则用户关掉开关却发现斜杠命令仍在出图，会以为配置没生效。
        use_image = bool(getattr(self, "enable_image_output", True))
        if msg.endswith("--text"):
            msg = msg[:-len("--text")].strip()
            use_image = False
        elif msg.endswith("--image"):
            msg = msg[:-len("--image")].strip()
            use_image = True
        return msg, use_image
    async def _render_result_as_image(
        self,
        event: AstrMessageEvent,
        text: str,
        prefix: str = "",
    ):
        """将文本渲染为卡片图片并发送；失败时回退到纯文本。"""
        try:
            from .core import emoji_feedback

            await emoji_feedback.on_query_start(event)
        except Exception:
            pass

        if Image is None:
            yield event.plain_result(prefix + markdown_to_plain(text))
            await self._emit_feedback(event, success=False)
            return

        image_path = None
        try:
            from .core.card_renderer import render_card

            # 传入 self 以便优先使用 AstrBot 的网络 t2i 渲染；
            # card_theme 透传可配置配色（底纹等），空 dict 则保持默认暖色。
            # getattr 兜底：防御热重载残留旧实例（未跑 __init__）缺属性。
            image_path = await render_card(
                text, host=self, theme=getattr(self, "card_theme", {}),
                prefer_local=getattr(self, "prefer_local_render", True),
            )
        except Exception as exc:
            logger.error(f"Dota2 文本转图片失败: {exc}")

        if not image_path:
            # 渲染失败：退化为纯文本，保证用户至少能拿到数据
            yield event.plain_result(prefix + markdown_to_plain(text))
            await self._emit_feedback(event, success=False)
            return

        # 交给 AstrBot 在会话结束时清理；不能在此处删除，
        # 因为消息链是在生成器结束后才真正发送的。
        track = getattr(event, "track_temporary_local_file", None)
        if callable(track):
            try:
                track(str(image_path))
            except Exception:
                pass

        chain = []
        if prefix and Plain is not None:
            chain.append(Plain(prefix))
        chain.append(Image.fromFileSystem(str(image_path)))
        yield event.chain_result(chain)
        await self._emit_feedback(event, success=True)

    # --- Fallback commands ---

    @filter.command_group("dota")
    def dota(self):
        """Dota2 查询命令组"""

    @dota.command("player")
    async def dota_player(self, event: AstrMessageEvent):
        '''查询玩家资料和近期战绩: /dota player <玩家名> [--text]'''
        if not self.enable_fallback:
            return

        player_name, use_image = self._extract_output_mode(event.message_str)
        if not player_name:
            yield event.plain_result("请输入玩家名称，例如：/dota player Miracle")
            return

        yield event.plain_result(f"正在查询玩家 [{player_name}] ...")
        try:
            from .core.templates import fmt_avg_kda, fmt_rank, render_player_profile

            profiles = await self.client.search_players(player_name)
            if not profiles:
                yield event.plain_result(f"找不到名为 '{player_name}' 的玩家。")
                return

            profile = profiles[0]
            full_profile = await self.client.get_player_profile(profile.account_id)
            if full_profile:
                profile = full_profile

            recent = await self.client.get_player_recent_matches(profile.account_id, limit=10)
            rank_str = fmt_rank(profile.rank_tier, profile.leaderboard_rank)
            avg_kda = fmt_avg_kda(recent)
            result_text = render_player_profile(profile, recent, rank_str, avg_kda)
            if use_image:
                async for res in self._render_result_as_image(event, result_text):
                    yield res
            else:
                yield event.plain_result(markdown_to_plain(result_text))
        except Exception as exc:
            logger.error(f"Dota2 玩家查询失败: {exc}")
            yield event.plain_result("查询失败：Dota2 服务暂时不可用，请稍后再试。")

    @dota.command("hero")
    async def dota_hero(self, event: AstrMessageEvent):
        '''查询英雄信息: /dota hero <英雄名> [--text]'''
        if not self.enable_fallback:
            return

        hero_name_raw, use_image = self._extract_output_mode(event.message_str)
        hero_name = hero_name_raw.strip().lower()
        if not hero_name:
            yield event.plain_result("请输入英雄名称，例如：/dota hero 反恐怖利刃")
            return

        yield event.plain_result(f"正在查询英雄 [{hero_name}] ...")
        try:
            from .core.templates import render_hero_info

            heroes = await self.client.get_heroes()
            hero = None
            for h in heroes:
                if (hero_name == h.localized_name.lower()
                        or hero_name == h.name.lower()
                        or hero_name == h.name.replace("npc_dota_hero_", "").lower()):
                    hero = h
                    break

            if not hero and self.hero_map:
                internal = self.hero_map.get(hero_name)
                if internal:
                    for h in heroes:
                        if h.name == internal:
                            hero = h
                            break

            if not hero:
                matches = [h for h in heroes if hero_name in h.localized_name.lower()]
                if len(matches) == 1:
                    hero = matches[0]
                elif len(matches) > 1:
                    names = ", ".join(h.localized_name for h in matches[:5])
                    yield event.plain_result(f"找到多个英雄：{names}，请提供更精确的名称。")
                    return

            if not hero:
                yield event.plain_result(f"找不到名为 '{hero_name}' 的英雄。")
                return

            result_text = render_hero_info(hero)
            if use_image:
                async for res in self._render_result_as_image(event, result_text):
                    yield res
            else:
                yield event.plain_result(markdown_to_plain(result_text))
        except Exception as exc:
            logger.error(f"Dota2 英雄查询失败: {exc}")
            yield event.plain_result("查询失败：Dota2 服务暂时不可用，请稍后再试。")

    @dota.command("match")
    async def dota_match(self, event: AstrMessageEvent):
        '''查询比赛详情: /dota match <比赛ID> [--text]'''
        if not self.enable_fallback:
            return

        match_id_str, use_image = self._extract_output_mode(event.message_str)
        if not match_id_str or not match_id_str.isdigit():
            yield event.plain_result("请输入比赛 ID（数字），例如：/dota match 8831125663")
            return

        match_id = int(match_id_str)
        yield event.plain_result(f"正在查询比赛 #{match_id} ...")
        try:
            from .core.templates import render_match_detail

            match = await self.client.get_match_detail(match_id)
            if not match:
                yield event.plain_result(f"获取比赛 #{match_id} 失败，比赛可能不存在。")
                return

            result_text = render_match_detail(match)
            cards = [result_text]

            # 具体某一局：用录像解析数据写赛后总结。
            # 已解析 → 同步生成总结并入卡片；未解析 → 先发提示、后台解析后补发。
            if use_image and getattr(self, "enable_replay_summary", True):
                if match.parsed:
                    analysis = await self._replay_analysis(match, cards, event, "")
                    md = self._compose_card_markdown(cards, analysis)
                    async for res in self._render_result_as_image(event, md):
                        yield res
                else:
                    yield event.plain_result(f"比赛 #{match_id} 录像正在解析中，请稍候…")
                    self._spawn(self._run_replay_summary(event, match_id, cards, ""))
                return

            if use_image:
                async for res in self._render_result_as_image(event, result_text):
                    yield res
            else:
                yield event.plain_result(markdown_to_plain(result_text))
        except Exception as exc:
            logger.error(f"Dota2 比赛查询失败: {exc}")
            yield event.plain_result("查询失败：Dota2 服务暂时不可用，请稍后再试。")

    @dota.command("live")
    async def dota_live(self, event: AstrMessageEvent):
        '''查看实时比赛: /dota live [--text]'''
        if not self.enable_fallback:
            return

        _, use_image = self._extract_output_mode(event.message_str)
        yield event.plain_result("正在查询实时比赛 ...")
        try:
            from .core.templates import render_live_games

            games = await self.client.get_live_games()
            result_text = render_live_games(games)
            if use_image:
                async for res in self._render_result_as_image(event, result_text):
                    yield res
            else:
                yield event.plain_result(markdown_to_plain(result_text))
        except Exception as exc:
            logger.error(f"Dota2 实时比赛查询失败: {exc}")
            yield event.plain_result("查询失败：Dota2 服务暂时不可用，请稍后再试。")

    @dota.command("pro")
    async def dota_pro(self, event: AstrMessageEvent):
        '''查看职业比赛: /dota pro [--text]'''
        if not self.enable_fallback:
            return

        _, use_image = self._extract_output_mode(event.message_str)
        yield event.plain_result("正在查询职业比赛 ...")
        try:
            from .core.templates import render_pro_matches

            matches = await self.client.get_pro_matches(limit=10)
            result_text = render_pro_matches(matches)
            if use_image:
                async for res in self._render_result_as_image(event, result_text):
                    yield res
            else:
                yield event.plain_result(markdown_to_plain(result_text))
        except Exception as exc:
            logger.error(f"Dota2 职业比赛查询失败: {exc}")
            yield event.plain_result("查询失败：Dota2 服务暂时不可用，请稍后再试。")

    @dota.command("bind")
    async def dota_bind(self, event: AstrMessageEvent):
        '''绑定 Steam ID: /dota bind <Steam ID>'''
        sender_id = event.get_sender_id()
        if not sender_id:
            yield event.plain_result("无法获取用户身份信息。")
            return

        # 从消息中提取数字（兼容各种格式）
        import re
        numbers = re.findall(r"\d+", event.message_str.strip())
        if not numbers:
            yield event.plain_result(
                "请提供 Steam ID，支持两种格式：\n"
                "- Steam ID 32-bit（数字），例如：/dota bind 899428504\n"
                "- Steam ID 64-bit（17位数字），例如：/dota bind 76561198859694232"
            )
            return

        steam_id_str = numbers[0]

        account_id = int(steam_id_str)
        # Steam ID 64-bit 转 32-bit
        if len(steam_id_str) == 17 and steam_id_str.startswith("76561198"):
            account_id = account_id - 76561197960265728

        # 验证 ID 是否有效
        yield event.plain_result(f"正在验证 Steam ID #{account_id} ...")
        try:
            profile = await self.client.get_player_profile(account_id)
            if not profile:
                yield event.plain_result(f"Steam ID #{account_id} 无效，请检查后重试。")
                return

            self.store.bind_account(sender_id, account_id, profile.persona_name or "")
            name = profile.persona_name or str(account_id)
            yield event.plain_result(f"绑定成功！已将你的 Dota2 账号绑定为 {name} (#{account_id})。\n之后可以说「查一下我的战绩」来查询。")
        except Exception as exc:
            logger.error(f"Dota2 绑定失败: {exc}")
            yield event.plain_result(f"绑定失败：{exc}")

    @dota.command("unbind")
    async def dota_unbind(self, event: AstrMessageEvent):
        '''解除 Steam ID 绑定: /dota unbind'''
        sender_id = event.get_sender_id()
        if not sender_id:
            yield event.plain_result("无法获取用户身份信息。")
            return

        try:
            existing = self.store.get_bound_account(sender_id)
            if not existing:
                yield event.plain_result("你还没有绑定过 Steam ID。")
                return

            self.store.unbind_account(sender_id)
            yield event.plain_result("已解除 Steam ID 绑定。")
        except Exception as exc:
            logger.error(f"Dota2 解绑失败: {exc}")
            yield event.plain_result(f"解绑失败：{exc}")

    async def terminate(self):
        """插件卸载时清理。"""


def _load_json_map(path: Path) -> dict[str, str]:
    """加载 JSON 映射文件，失败返回空 dict。"""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"加载映射文件 {path.name} 失败: {exc}")
    return {}
