"""新.戳一戳 —— AstrBot 跨平台戳一戳增强插件。

功能总览
--------
- **原生戳一戳**：QQ / OneBot 协议端的 ``Poke`` 消息段与 ``notice.notify.poke``
  通知双通道监听，自动去重，不再漏事件；
- **全平台兼容**：Telegram、飞书、企业微信、微信个人号、QQ 官方机器人等没有原生
  戳能力的平台，用「触发词 + 指令」模拟，回戳走各平台自己的拟真文案；
- **回戳三级降级**：原生 API → Poke 消息段 → 拟真文案，任何一级失败都不会把
  异常抛给用户；
- **发言引擎**：权重随机 / 轮换 / 洗牌袋三种模式，支持变量插值、连戳彩蛋；
- **LLM 台词**：可选让模型现场生成一句，带超时、字数上限与每分钟配额，失败自动
  回退模板；
- **防骚扰**：同一用户 60 秒内被戳超过阈值就静默（可选怼一句），**不拉黑**；
- **人形化联动**：只读 humanoid_core 导出的版本化契约，睡着时不吵、情绪带进提示词；
- **数据看板**：今日/累计被戳次数、连击数、排行榜，KV 持久化；
- **指令与工具**：``/戳``、``/戳状态``、``/戳榜``、``/戳帮助``，以及可选的
  LLM 函数工具 ``poke_user``。

作者：娜莉灵 ｜ 版本：v1.91.78 ｜ 许可：MIT
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from collections import deque
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
    from .poke_core.humanoid import HumanoidBridge, HumanoidSnapshot
    from .poke_core.ratelimit import BurstTracker, RateLimiter
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
    from poke_core.humanoid import HumanoidBridge, HumanoidSnapshot
    from poke_core.ratelimit import BurstTracker, RateLimiter
    from poke_core.settings import Settings, as_str
    from poke_core.stats import StatsStore
    from poke_core.templates import TemplatePool, render_template

try:
    from astrbot.api.message_components import At, Plain
except Exception:  # pragma: no cover
    At = Plain = None  # type: ignore[assignment]


PLUGIN_NAME = "新.戳一戳"
PLUGIN_VERSION = "v1.91.78"
SEEN_FLAG = "_superpoke_seen"
HANDLED_FLAG = "_superpoke_handled"

#: LLM 台词的内置系统提示（用户可在「LLM 系统提示」里整体覆盖）
DEFAULT_LLM_SYSTEM = (
    "你正在被聊天对象「戳一戳」。用你此刻的语气回一句就好："
    "不要解释、不要加引号、不要写旁白，只输出这句话本身。"
)

HELP_TEXT = f"""🫵 {PLUGIN_NAME} {PLUGIN_VERSION} · 使用说明

【被动】别人戳你时自动回应
· QQ：双击头像戳一戳，机器人会发言 / 回戳
· 其他平台：发送触发词（默认「戳一戳」「戳戳」「拍一拍」「戳我」）触发

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
        self._burst = BurstTracker(window=60.0)
        self._rng = random.Random()
        self._speech_pool = TemplatePool(())
        self._combo_pool = TemplatePool(())
        self._harass_pool = TemplatePool(())
        self._pool_key: tuple | None = None
        self._started_at = datetime.now()
        self._llm_window: deque[float] = deque()
        self._humanoid: HumanoidBridge | None = None
        self._humanoid_key: tuple[bool, str] | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        settings = self._settings()
        self._stats.enabled = settings.stats_enabled
        await self._stats.load()
        self._logger.info(
            f"[{PLUGIN_NAME}] {PLUGIN_VERSION} 已启动："
            f"发言模板 {len(settings.speech_pool)} 条，"
            f"回戳={'开' if settings.poke_back_enabled else '关'}，"
            f"LLM={'开' if settings.llm_enabled else '关'}，"
            f"防骚扰={'开' if settings.harass_enabled else '关'}，"
            f"人形化联动={'开' if settings.humanoid_enabled else '关'}",
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
        self._burst.purge()
        if settings.stats_enabled:
            await self._stats.load()

        # ---- 人形化联动：她睡着了就别吵 ----
        humanoid = self._humanoid_snapshot(settings, event, signal.sender_id)
        if (
            humanoid is not None
            and humanoid.asleep
            and settings.humanoid_silent_when_asleep
        ):
            self._logger.debug(f"[{PLUGIN_NAME}] 人形化核心处于睡眠状态，本次不回应")
            return

        # ---- 冷却 / 全局限流 ----
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

        # ---- 防骚扰：60 秒窗口内的连戳计数 ----
        burst = self._burst.note(signal.sender_id) if settings.harass_enabled else 0
        if settings.harass_enabled and burst > settings.harass_threshold:
            self._limiter.touch_global()
            event.set_extra(HANDLED_FLAG, True)
            self._logger.info(
                f"[{PLUGIN_NAME}] 用户 {signal.sender_name or signal.sender_id} "
                f"60 秒内第 {burst} 次戳，超过阈值 {settings.harass_threshold}，"
                f"按「{settings.harass_action}」处理",
            )
            if settings.harass_should_reply():
                template = self._pick_harass(settings)
                if template:
                    context = self._build_context(settings, signal, burst, burst)
                    text = render_template(template, context)
                    yield event.chain_result(
                        self._build_speech_chain(settings, signal, text),
                    )
            return

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
                text = await self._compose_speech(event, settings, signal, count, streak, humanoid)
                if text:
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
            self._logger.debug(
                f"[{PLUGIN_NAME}] 回戳 {target_name or target_id} 未执行（{outcome.message}）",
            )

    # ------------------------------------------------------------------
    # 发言合成：LLM 优先，失败/未配置时回退模板
    # ------------------------------------------------------------------
    async def _compose_speech(
        self,
        event: AstrMessageEvent,
        settings: Settings,
        signal: PokeSignal,
        count: int,
        streak: int,
        humanoid: HumanoidSnapshot | None,
    ) -> str | None:
        if (
            settings.llm_enabled
            and self._llm_quota_ok(settings)
            and self._decide(True, settings.llm_probability)
        ):
            text = await self._llm_speech(event, settings, signal, count, streak, humanoid)
            if text:
                self._llm_window.append(asyncio.get_running_loop().time())
                return text
        template = self._pick_speech(settings, streak)
        if not template:
            return None
        context = self._build_context(settings, signal, count, streak)
        return render_template(template, context)

    def _llm_quota_ok(self, settings: Settings) -> bool:
        if settings.llm_per_minute <= 0:
            return False
        now = asyncio.get_running_loop().time()
        while self._llm_window and now - self._llm_window[0] > 60.0:
            self._llm_window.popleft()
        return len(self._llm_window) < settings.llm_per_minute

    async def _llm_speech(
        self,
        event: AstrMessageEvent,
        settings: Settings,
        signal: PokeSignal,
        count: int,
        streak: int,
        humanoid: HumanoidSnapshot | None,
    ) -> str | None:
        """让模型现场生成一句；任何异常都静默回退到模板。"""
        try:
            getter = getattr(self.context, "get_using_provider", None)
            provider = getter(umo=event.unified_msg_origin) if callable(getter) else None
            if provider is None:
                return None

            system = settings.llm_system_prompt.strip() or DEFAULT_LLM_SYSTEM
            lines = [
                f"戳你的人：{signal.sender_name or signal.sender_id}",
                f"这是对方今天第 {count} 次戳你，当前连击 {streak} 次。",
            ]
            if humanoid is not None:
                lines.extend(humanoid.as_prompt_lines())
            if settings.llm_extra_prompt.strip():
                lines.append(settings.llm_extra_prompt.strip())
            if settings.llm_max_chars:
                lines.append(f"控制在 {settings.llm_max_chars} 个字以内。")
            prompt = "\n".join(lines)

            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system),
                timeout=settings.llm_timeout,
            )
            text = (getattr(response, "completion_text", "") or "").strip()
            if not text:
                return None
            limit = settings.llm_max_chars
            if humanoid is not None and humanoid.max_chars > 0:
                limit = min(limit, humanoid.max_chars) if limit else humanoid.max_chars
            if limit and len(text) > limit:
                text = text[:limit].rstrip()
            return text or None
        except asyncio.TimeoutError:
            self._logger.warning(f"[{PLUGIN_NAME}] LLM 生成超时，回退模板")
            return None
        except Exception as exc:  # pragma: no cover - 绝不把异常抛给用户
            self._logger.warning(f"[{PLUGIN_NAME}] LLM 生成失败，回退模板：{exc}")
            return None

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
            "可以试试开启「原生失败降级」，或在「平台适配」里填一条拟真文案。"
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
            f"🫵 {PLUGIN_NAME} {PLUGIN_VERSION} · 运行状态",
            f"平台：{platform}（原生戳：{'✅' if profile.native_poke else '❌ 拟真模式'}）",
            f"总开关：{'✅' if settings.enabled else '❌'}　"
            f"回戳：{'✅' if settings.poke_back_enabled else '❌'}　"
            f"发言：{'✅' if settings.speech_enabled else '❌'}",
            f"发言模板：{len(settings.speech_pool)} 条（{settings.speech_mode}）",
            f"LLM 台词：{'✅' if settings.llm_enabled else '❌'}　"
            f"防骚扰：{'✅ ' + str(settings.harass_threshold) + '/分' if settings.harass_enabled else '❌'}",
            f"回戳次数：{settings.poke_back_times}　冷却：{settings.user_cooldown:.0f}s　"
            f"限流：{settings.per_minute_limit}/分钟",
            f"今日被戳：{summary['today_total']} 次（{summary['today_users']} 人）",
            f"累计被戳：{summary['all_total']} 次",
            f"已运行：{hours} 小时 {minutes} 分钟",
        ]

        humanoid = self._humanoid_snapshot(settings, event, as_str(event.get_sender_id()))
        if settings.humanoid_enabled:
            if humanoid is not None:
                lines.append(f"人形化：{humanoid.summary() or '已连接'}"
                             f"{'（睡眠中）' if humanoid.asleep else ''}")
            else:
                lines.append("人形化：已开启，但没读到 humanoid_core 的契约")

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
        key = (
            settings.speech_mode,
            settings.speech_pool,
            settings.combo_pool,
            settings.harass_pool,
        )
        if key == self._pool_key:
            return
        self._speech_pool = TemplatePool(settings.speech_pool, settings.speech_mode)
        self._combo_pool = TemplatePool(settings.combo_pool, settings.speech_mode)
        self._harass_pool = TemplatePool(settings.harass_pool, settings.speech_mode)
        self._pool_key = key

    def _humanoid_bridge(self, settings: Settings) -> HumanoidBridge:
        key = (settings.humanoid_enabled, settings.humanoid_state_path)
        if self._humanoid is None or key != self._humanoid_key:
            self._humanoid = HumanoidBridge(override_path=settings.humanoid_state_path)
            self._humanoid_key = key
        return self._humanoid

    def _humanoid_snapshot(
        self,
        settings: Settings,
        event: AstrMessageEvent,
        user_id: str,
    ) -> HumanoidSnapshot | None:
        if not settings.humanoid_enabled:
            return None
        try:
            bridge = self._humanoid_bridge(settings)
            return bridge.snapshot(as_str(event.get_self_id()), user_id)
        except Exception as exc:  # pragma: no cover - 联动失败绝不外抛
            self._logger.debug(f"[{PLUGIN_NAME}] 读取人形化契约失败：{exc}")
            return None

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

    def _pick_harass(self, settings: Settings) -> str | None:
        """防骚扰触发时取一条「怼人」文案；配了冷却就按冷却来。"""
        if not settings.harass_pool:
            return None
        allowed, _ = self._limiter.check_user(
            f"harass:{id(self)}",
            settings.harass_cooldown,
        )
        if not allowed:
            return None
        text = self._harass_pool.pick(self._rng)
        if text:
            self._limiter.touch_user(f"harass:{id(self)}")
        return text

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
