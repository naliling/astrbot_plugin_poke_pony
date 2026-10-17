"""新·戳一戳 —— AstrBot 跨平台戳一戳增强插件。

功能总览
--------
- **原生戳一戳**：QQ / OneBot 协议端的 ``Poke`` 消息段与 ``notice.notify.poke``
  通知双通道监听，自动去重，不再漏事件；
- **全平台兼容**：Telegram、飞书、企业微信、微信个人号、QQ 官方机器人等没有原生
  戳能力的平台，用「触发词 + 指令」模拟，回戳走拟真文案；
- **回戳三级降级**：原生 API → Poke 消息段 → 拟真文案，任何一级失败都不会把
  异常抛给用户（老版本正是死在这一步）；
- **发言引擎**：权重随机 / 轮换 / 洗牌袋三种模式，支持变量插值、连戳彩蛋；
- **防刷屏**：用户冷却、每分钟上限、安静时段、黑白名单；
- **数据看板**：今日/累计被戳次数、连击数、排行榜，KV 持久化；
- **指令与工具**：``/戳``、``/戳状态``、``/戳榜``、``/戳帮助``，以及可选的
  LLM 函数工具 ``poke_user``。

作者：naliling ｜ 版本：v2.0.0 ｜ 许可：MIT
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime
from typing import Any, AsyncGenerator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # AstrBot 标准加载方式：插件目录作为包导入
    from .poke_core.backends import PokeBackend
    from .poke_core.detect import (
        PokeSignal,
        detect_poke,
        first_at_target,
        reply_target,
    )
    from .poke_core.ratelimit import RateLimiter
    from .poke_core.settings import Settings, as_str
    from .poke_core.stats import StatsStore
    from .poke_core.templates import TemplatePool, render_template
except ImportError:  # pragma: no cover - 兼容以顶层模块方式加载
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from poke_core.backends import PokeBackend
    from poke_core.detect import (
        PokeSignal,
        detect_poke,
        first_at_target,
        reply_target,
    )
    from poke_core.ratelimit import RateLimiter
    from poke_core.settings import Settings, as_str
    from poke_core.stats import StatsStore
    from poke_core.templates import TemplatePool, render_template

try:
    from astrbot.api.message_components import At, Plain
except Exception:  # pragma: no cover
    At = Plain = None  # type: ignore[assignment]


PLUGIN_NAME = "新·戳一戳"
SEEN_FLAG = "_superpoke_seen"
HANDLED_FLAG = "_superpoke_handled"

HELP_TEXT = """🫵 新·戳一戳 · 使用说明

【被动】别人戳你时自动回应
· QQ：双击头像戳一戳，机器人会发言 / 回戳
· 其他平台：发送「戳一戳」「戳戳」「拍一拍」「戳我」触发

【主动】用指令戳别人
· /戳 @某人         —— 戳一下
· /戳 @某人 3       —— 连戳三下
· /戳 123456789     —— 按用户 ID 戳（非 QQ 平台可用）
· 回复某人的消息 + /戳 —— 戳被回复的人

【查询】
· /戳状态  —— 查看运行状态与今日数据
· /戳榜    —— 今日被戳排行榜
· /戳帮助  —— 显示本说明"""


class SuperPokePlugin(Star):
    """跨平台戳一戳增强插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self._logger = getattr(self, "logger", None) or logger
        self._stats = StatsStore(plugin=self, logger=self._logger, enabled=True)
        self._limiter = RateLimiter()
        self._rng = random.Random()
        self._speech_pool = TemplatePool(())
        self._combo_pool = TemplatePool(())
        self._pool_key: tuple | None = None
        self._started_at = datetime.now()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        settings = self._settings()
        self._stats.enabled = settings.stats_enabled
        await self._stats.load()
        self._logger.info(
            f"[{PLUGIN_NAME}] 已启动：{len(settings.speech_pool)} 条发言模板，"
            f"回戳={'开' if settings.poke_back_enabled else '关'}，"
            f"统计={'开' if settings.stats_enabled else '关'}",
        )

    async def terminate(self) -> None:
        try:
            await self._stats.flush()
        except Exception as exc:  # pragma: no cover
            self._logger.warning(f"[{PLUGIN_NAME}] 卸载时保存统计失败：{exc}")
        self._logger.info(f"[{PLUGIN_NAME}] 已卸载")

    # ------------------------------------------------------------------
    # 主监听：原生戳一戳 + 跨平台触发词
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_poke_event(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """监听戳一戳事件与触发词。"""
        try:
            settings = self._settings()
            if not settings.enabled:
                return
            if event.get_extra(SEEN_FLAG):
                return
            event.set_extra(SEEN_FLAG, True)

            signal = detect_poke(event, settings)
            if signal is None:
                return
            async for result in self._handle_signal(event, signal, settings):
                yield result
            # 真正处理过这次戳一戳才终止事件，避免影响被限流/被过滤的消息
            if event.get_extra(HANDLED_FLAG):
                # 洋葱模型下消息已在此前发出，这里只阻止后续 LLM 再回一遍
                event.stop_event()
        except Exception as exc:  # pragma: no cover - 兜底，绝不影响主流程
            self._logger.error(f"[{PLUGIN_NAME}] 处理戳一戳事件失败：{exc}", exc_info=True)

    async def _handle_signal(
        self,
        event: AstrMessageEvent,
        signal: PokeSignal,
        settings: Settings,
    ) -> AsyncGenerator[Any, None]:
        """处理一次已经识别出来的戳一戳信号。"""
        if not self._pass_gate(settings, signal):
            return

        self._limiter.purge()
        if settings.stats_enabled:
            await self._stats.load()

        allowed, wait = self._limiter.check_user(signal.sender_id, settings.user_cooldown)
        if not allowed:
            self._logger.debug(
                f"[{PLUGIN_NAME}] 用户 {signal.sender_id} 冷却中，剩余 {wait:.1f}s",
            )
            return
        if not self._limiter.check_global(settings.per_minute_limit):
            self._logger.debug(f"[{PLUGIN_NAME}] 触发全局每分钟上限，丢弃本次戳一戳")
            return

        self_id = as_str(event.get_self_id())
        proxy_target = bool(signal.target_id and self_id and signal.target_id != self_id)

        # ---- 统计 ----
        count = 1
        streak = 1
        if settings.stats_enabled:
            if proxy_target:
                self._stats.record_outgoing(signal.target_id, signal.target_name)
            else:
                count = self._stats.record_incoming(signal.sender_id, signal.sender_name)
                streak = self._stats.streak(signal.sender_id)
            await self._stats.save_if_due()

        self._limiter.touch_user(signal.sender_id)
        self._limiter.touch_global()
        event.set_extra(HANDLED_FLAG, True)

        context = self._build_context(settings, signal, count, streak)

        # ---- 发言 ----
        if not proxy_target:
            will_poke = self._decide(settings.poke_back_enabled, settings.poke_back_probability)
            speech = self._decide(settings.speech_enabled, settings.speech_probability)
            if settings.link_mode == "回戳时不发言" and will_poke:
                speech = False
            elif settings.link_mode == "仅回戳时发言" and not will_poke:
                speech = False

            if speech:
                template = self._pick_speech(settings, streak)
                if template:
                    text = render_template(template, context)
                    chain = self._build_speech_chain(settings, signal, text)
                    self._logger.info(
                        f"[{PLUGIN_NAME}] 回应 {signal.sender_name or signal.sender_id}"
                        f"（第 {count} 次，连击 {streak}）：{text}",
                    )
                    yield event.chain_result(chain)
        else:
            will_poke = self._decide(settings.poke_back_enabled, settings.poke_back_probability)

        # ---- 回戳 ----
        if not will_poke:
            return
        backend = PokeBackend(settings, self._logger)
        target_id = signal.target_id if proxy_target else signal.sender_id
        target_name = signal.target_name if proxy_target else signal.sender_name
        outcome = await backend.poke(
            event,
            target_id=target_id,
            target_name=target_name,
            group_id=signal.group_id,
            times=settings.poke_back_times,
        )
        if outcome.ok:
            self._logger.info(
                f"[{PLUGIN_NAME}] 回戳 {target_name or target_id} 成功（{outcome.message}）",
            )
        else:
            self._logger.warning(
                f"[{PLUGIN_NAME}] 回戳 {target_name or target_id} 失败（{outcome.message}），已静默跳过",
            )

    # ------------------------------------------------------------------
    # 指令：/戳
    # ------------------------------------------------------------------
    @filter.command("戳", alias={"poke"})
    async def poke_command(self, event: AstrMessageEvent, arg: str = "") -> AsyncGenerator[Any, None]:
        """戳一戳指定用户。"""
        settings = self._settings()
        try:
            reply = await self._do_poke_command(event, settings, arg)
        except Exception as exc:  # pragma: no cover - 指令也绝不把异常甩给用户
            self._logger.error(f"[{PLUGIN_NAME}] /戳 执行失败：{exc}", exc_info=True)
            reply = "戳的时候出了点小问题，已经记到日志里了。"
        yield event.plain_result(reply)
        event.stop_event()

    async def _do_poke_command(
        self,
        event: AstrMessageEvent,
        settings: Settings,
        arg: str,
    ) -> str:
        reason = self._command_guard(event, settings, require_active=True)
        if reason:
            return reason

        target_id, times = self._parse_poke_args(event, arg, settings)
        if not target_id:
            return (
                "用法：/戳 @某人 [次数]　或　回复某人的消息后发送 /戳\n"
                "非 QQ 平台可以写成 /戳 用户ID 3"
            )

        allowed, wait = self._limiter.check_user(
            f"cmd:{event.get_sender_id()}",
            settings.active_poke_cooldown,
        )
        if not allowed:
            return f"别急，{wait:.1f} 秒后再戳。"
        self._limiter.touch_user(f"cmd:{event.get_sender_id()}")

        if settings.stats_enabled:
            await self._stats.load()
            self._stats.record_outgoing(target_id)
            await self._stats.save_if_due()

        backend = PokeBackend(settings, self._logger)
        outcome = await backend.poke(
            event,
            target_id=target_id,
            target_name=target_id,
            group_id=as_str(event.get_group_id()),
            times=times,
        )
        if outcome.ok:
            verb = "原生回戳" if outcome.method == "native" else "拟真回戳"
            return f"已{verb} {target_id} ×{times} ✅"
        return (
            f"戳失败了：{outcome.detail or '当前平台不支持'}\n"
            "可以试试开启「原生失败降级」，让机器人改用拟真文案。"
        )

    # ------------------------------------------------------------------
    # 指令：/戳状态
    # ------------------------------------------------------------------
    @filter.command("戳状态", alias={"戳信息"})
    async def poke_status(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看戳一戳插件运行状态。"""
        settings = self._settings()
        if settings.stats_enabled:
            await self._stats.load()
        platform = as_str(event.get_platform_name()) or "unknown"
        profile = settings.profile_for(platform)
        summary = self._stats.summary()
        uptime = datetime.now() - self._started_at
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes = remainder // 60

        lines = [
            f"🫵 {PLUGIN_NAME} · 运行状态",
            f"平台：{platform}（原生戳：{'✅' if profile.native_poke else '❌ 拟真模式'}）",
            f"总开关：{'✅' if settings.enabled else '❌'}　"
            f"回戳：{'✅' if settings.poke_back_enabled else '❌'}　"
            f"发言：{'✅' if settings.speech_enabled else '❌'}",
            f"发言模板：{len(settings.speech_pool)} 条（{settings.speech_mode}）",
            f"回戳次数：{settings.poke_back_times}　冷却：{settings.user_cooldown:.0f}s　"
            f"限流：{settings.per_minute_limit}/分钟",
            f"今日被戳：{summary['today_total']} 次（{summary['today_users']} 人）",
            f"累计被戳：{summary['all_total']} 次",
            f"已运行：{hours} 小时 {minutes} 分钟",
        ]
        if settings.in_quiet_hours(_now_minutes()):
            lines.append("😴 当前处于安静时段，不会回应")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ------------------------------------------------------------------
    # 指令：/戳榜
    # ------------------------------------------------------------------
    @filter.command("戳榜", alias={"戳排行", "戳排名"})
    async def poke_ranking(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """查看今日被戳排行榜。"""
        settings = self._settings()
        if settings.stats_enabled:
            await self._stats.load()
        rows = self._stats.ranking(settings.ranking_size)
        if not rows:
            yield event.plain_result("今天还没人戳我，榜上空空如也 🕳️")
            event.stop_event()
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🫵 今日被戳排行榜"]
        for index, (user_id, count, name) in enumerate(rows):
            badge = medals[index] if index < len(medals) else f"{index + 1}."
            lines.append(f"{badge} {name or user_id} —— {count} 次")
        lines.append(f"合计 {self._stats.today.total} 次")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ------------------------------------------------------------------
    # 指令：/戳帮助
    # ------------------------------------------------------------------
    @filter.command("戳帮助", alias={"戳说明", "戳用法"})
    async def poke_help(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """显示使用说明。"""
        yield event.plain_result(HELP_TEXT)
        event.stop_event()

    # ------------------------------------------------------------------
    # LLM 工具：让模型主动戳人
    # ------------------------------------------------------------------
    @filter.llm_tool(name="poke_user")
    async def poke_user_tool(self, event: AstrMessageEvent, target_id: str, times: int = 1) -> str:
        """戳一戳指定用户，用于表达「提醒 / 撒娇 / 催促」等动作。

        Args:
            target_id(string): 目标用户的 ID，QQ 号或平台用户 ID
            times(number): 戳的次数，范围 1-10，默认 1
        """
        settings = self._settings()
        if not settings.llm_tool_enabled:
            return "戳一戳工具未启用，请先在插件配置里打开「LLM工具开关」。"
        if not settings.enabled or not settings.active_poke_enabled:
            return "戳一戳功能当前已关闭。"
        target = as_str(target_id).strip()
        if not target:
            return "缺少目标用户 ID。"
        times = max(1, min(int(times or 1), 10))
        backend = PokeBackend(settings, self._logger)
        outcome = await backend.poke(
            event,
            target_id=target,
            target_name=target,
            group_id=as_str(event.get_group_id()),
            times=times,
        )
        if not outcome.ok:
            return f"戳 {target} 失败：{outcome.detail or '平台不支持'}"
        return f"已戳 {target} ×{times}（{outcome.method}）"

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _settings(self) -> Settings:
        settings = Settings.from_config(self.config)
        self._refresh_pools(settings)
        return settings

    def _refresh_pools(self, settings: Settings) -> None:
        key = (settings.speech_mode, settings.speech_pool, settings.combo_pool)
        if key == self._pool_key:
            return
        self._speech_pool = TemplatePool(settings.speech_pool, settings.speech_mode)
        self._combo_pool = TemplatePool(settings.combo_pool, settings.speech_mode)
        self._pool_key = key

    def _pass_gate(self, settings: Settings, signal: PokeSignal) -> bool:
        """平台 / 群聊私聊 / 名单 / 安静时段 的统一闸门。"""
        if not settings.platform_allowed(signal.platform):
            return False
        if signal.is_group:
            if not settings.group_enabled or not settings.group_allowed(signal.group_id):
                return False
        elif not settings.private_enabled:
            return False
        if not settings.user_allowed(signal.sender_id):
            return False
        if settings.in_quiet_hours(_now_minutes()):
            return False
        return True

    def _command_guard(
        self,
        event: AstrMessageEvent,
        settings: Settings,
        *,
        require_active: bool = False,
    ) -> str | None:
        """指令通用前置校验，返回错误文案或 None。"""
        if not settings.enabled:
            return f"{PLUGIN_NAME} 当前已被关闭。"
        platform = as_str(event.get_platform_name()).lower()
        if not settings.platform_allowed(platform):
            return f"本平台（{platform or 'unknown'}）未启用戳一戳。"
        if require_active and not settings.active_poke_enabled:
            return "主动戳功能已关闭。"
        if require_active and settings.active_poke_admin_only and not event.is_admin():
            return "只有管理员可以主动戳人。"
        return None

    def _parse_poke_args(
        self,
        event: AstrMessageEvent,
        arg: str,
        settings: Settings,
    ) -> tuple[str, int]:
        """解析 ``/戳`` 的目标与次数。"""
        default_times = settings.active_poke_default_times
        tokens = [token for token in as_str(arg).replace("，", " ").split() if token]
        numbers = [token for token in tokens if token.isdigit()]

        target = first_at_target(event) or reply_target(event)
        if target:
            times = int(numbers[0]) if numbers else default_times
            return target, max(1, min(times, 10))

        if not numbers:
            return "", default_times
        if len(numbers) >= 2:
            return numbers[0], max(1, min(int(numbers[1]), 10))
        first = numbers[0]
        if len(first) >= 5:  # 长数字更可能是用户 ID，而不是次数
            return first, default_times
        return "", max(1, min(int(first), 10))

    def _build_context(
        self,
        settings: Settings,
        signal: PokeSignal,
        count: int,
        streak: int,
    ) -> dict[str, str]:
        now = datetime.now()
        return {
            "sender": signal.sender_name or signal.sender_id,
            "sender_id": signal.sender_id,
            "target": signal.target_name or signal.target_id,
            "target_id": signal.target_id,
            "bot": settings.bot_nickname,
            "group": signal.group_id,
            "count": str(count),
            "streak": str(streak),
            "time": now.strftime("%H:%M"),
            "date": now.strftime("%Y-%m-%d"),
            "platform": signal.platform,
        }

    def _pick_speech(self, settings: Settings, streak: int) -> str | None:
        use_combo = (
            settings.combo_threshold > 0
            and streak >= settings.combo_threshold
            and bool(self._combo_pool)
        )
        pool = self._combo_pool if use_combo else self._speech_pool
        return pool.pick(self._rng)

    def _build_speech_chain(
        self,
        settings: Settings,
        signal: PokeSignal,
        rendered: str,
    ) -> list:
        profile = settings.profile_for(signal.platform)
        chain: list = []
        if At is not None and Plain is not None and settings.speech_at and profile.supports_at:
            if str(signal.sender_id).isdigit():
                chain.append(At(qq=str(signal.sender_id)))
                rendered = f" {rendered}"
        if Plain is not None:
            chain.append(Plain(rendered))
        return chain

    @staticmethod
    def _decide(enabled: bool, probability: int) -> bool:
        if not enabled:
            return False
        if probability >= 100:
            return True
        if probability <= 0:
            return False
        return random.randint(1, 100) <= probability


def _now_minutes() -> int:
    now = datetime.now()
    return now.hour * 60 + now.minute
