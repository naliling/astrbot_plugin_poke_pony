"""跨平台「回戳」执行器。

这是老版本插件最大的坑：它直接 ``event.bot.api.call_action("group_poke", ...)``，
一旦适配器换了、或者协议端没实现该 action，就会抛异常并把错误甩到聊天窗口里。

新版本把「回戳」拆成三级降级链，任何一级失败都不会中断事件、更不会把
Python traceback 丢给用户：

1. **原生回戳**：QQ/OneBot 的 ``group_poke`` / ``friend_poke`` / ``send_poke``；
2. **链式回戳**（实验开关）：把 ``Poke`` 消息段塞进消息链发送；
3. **拟真回戳**：按平台文案发一条「戳了戳 / 拍了拍」，Telegram、飞书、
   企业微信、微信个人号等没有原生戳能力的平台走这一级。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .settings import Settings, as_str
from .templates import render_template

try:  # 不同 AstrBot 版本组件导出位置略有差异
    from astrbot.api.event import MessageChain
except Exception:  # pragma: no cover
    MessageChain = None  # type: ignore[assignment]

try:
    from astrbot.api.message_components import At, Plain
except Exception:  # pragma: no cover
    At = Plain = None  # type: ignore[assignment]


class BackendError(Exception):
    """原生回戳不可用。"""


@dataclass(slots=True)
class PokeOutcome:
    """一次回戳的执行结果。"""

    ok: bool
    method: str
    """``native`` / ``chain`` / ``text`` / ``skipped``。"""
    detail: str = ""

    @property
    def message(self) -> str:
        return f"{self.method}:{self.detail}" if self.detail else self.method


def _poke_component_class() -> Any:
    """延迟导入 Poke 组件，老版本 AstrBot 上返回 None。"""
    try:
        from astrbot.api.message_components import Poke

        return Poke
    except Exception:  # pragma: no cover
        return None


def _to_api_id(value: str) -> int | str:
    """QQ 类平台的 ID 必须是 int；其他平台的字符串 ID 原样返回。"""
    text = str(value or "").strip()
    if text.lstrip("-").isdigit():
        try:
            return int(text)
        except ValueError:  # pragma: no cover
            return text
    return text


class PokeBackend:
    """把「回戳」落到具体平台。"""

    def __init__(self, settings: Settings, logger: Any = None) -> None:
        self.settings = settings
        self.logger = logger

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def poke(
        self,
        event: Any,
        *,
        target_id: str,
        target_name: str = "",
        group_id: str = "",
        times: int = 1,
        native_only: bool = False,
    ) -> PokeOutcome:
        """执行回戳，自动按平台能力降级。"""
        platform = as_str(getattr(event, "get_platform_name", lambda: "")()).lower()
        profile = self.settings.profile_for(platform)
        times = max(int(times or 1), 1)
        context = self._build_context(event, target_id, target_name, group_id, times)

        want_native = profile.native_poke and (self.settings.prefer_native or native_only)
        errors: list[str] = []

        if want_native:
            try:
                action = await self._native_poke(event, target_id, group_id, times)
                return PokeOutcome(True, "native", action)
            except BackendError as exc:
                errors.append(str(exc))
            except Exception as exc:  # pragma: no cover - 兜底，绝不外抛
                errors.append(f"{type(exc).__name__}: {exc}")

        if native_only:
            return PokeOutcome(False, "native", "; ".join(errors) or "该平台不支持原生回戳")

        if profile.native_poke and self.settings.chain_poke:
            try:
                await self._chain_poke(event, target_id)
                return PokeOutcome(True, "chain", "poke-segment")
            except Exception as exc:
                errors.append(f"chain: {exc}")

        if self.settings.native_fallback or not profile.native_poke:
            try:
                await self._text_poke(event, profile, context, target_id)
                return PokeOutcome(True, "text", "; ".join(errors))
            except Exception as exc:  # pragma: no cover
                errors.append(f"text: {exc}")

        return PokeOutcome(False, "skipped", "; ".join(errors))

    async def supports_native(self, event: Any) -> bool:
        """探测当前事件是否具备原生回戳能力（不做真实调用）。"""
        platform = as_str(getattr(event, "get_platform_name", lambda: "")()).lower()
        if not self.settings.profile_for(platform).native_poke:
            return False
        return self._resolve_call_action(event) is not None

    # ------------------------------------------------------------------
    # 一级：原生 API
    # ------------------------------------------------------------------
    def _resolve_call_action(self, event: Any) -> Any:
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        caller = getattr(bot, "call_action", None)
        if callable(caller):
            return caller
        # 少数适配器把 API 挂在 bot.api 上
        api = getattr(bot, "api", None)
        caller = getattr(api, "call_action", None)
        return caller if callable(caller) else None

    async def _native_poke(self, event: Any, target_id: str, group_id: str, times: int) -> str:
        caller = self._resolve_call_action(event)
        if caller is None:
            raise BackendError("当前适配器未暴露 call_action，无法原生回戳")

        user_id = _to_api_id(target_id)
        gid = _to_api_id(group_id) if group_id else ""

        if gid:
            attempts = [
                ("group_poke", {"group_id": gid, "user_id": user_id}),
                ("send_poke", {"group_id": gid, "user_id": user_id}),
            ]
        else:
            attempts = [
                ("friend_poke", {"user_id": user_id}),
                ("send_poke", {"user_id": user_id}),
            ]

        errors: list[str] = []
        interval = max(self.settings.poke_back_interval_ms, 0) / 1000.0
        done = 0
        for index in range(times):
            if index and interval:
                await asyncio.sleep(interval)
            last_error = ""
            for action, params in attempts:
                try:
                    await caller(action, **params)
                    done += 1
                    break
                except Exception as exc:
                    last_error = f"{action}: {exc}"
            else:
                errors.append(last_error or "unknown error")
                break

        if done == 0:
            raise BackendError("; ".join(errors) or "协议端未实现戳一戳 action")
        return f"poke x{done}"

    # ------------------------------------------------------------------
    # 二级：Poke 消息段（实验特性）
    # ------------------------------------------------------------------
    async def _chain_poke(self, event: Any, target_id: str) -> None:
        poke_cls = _poke_component_class()
        if poke_cls is None or MessageChain is None:
            raise BackendError("当前 AstrBot 版本不支持 Poke 消息段")
        await event.send(MessageChain([poke_cls(id=str(target_id))]))

    # ------------------------------------------------------------------
    # 三级：拟真文案
    # ------------------------------------------------------------------
    async def _text_poke(
        self,
        event: Any,
        profile: Any,
        context: dict[str, str],
        target_id: str,
    ) -> None:
        if MessageChain is None or Plain is None:  # pragma: no cover
            raise BackendError("消息组件不可用")

        template = profile.render_template() or self.settings.poke_back_text
        text = render_template(template, context)
        chain = []
        if At is not None and profile.supports_at and str(target_id).isdigit():
            chain.append(At(qq=str(target_id)))
            text = f" {text}"
        chain.append(Plain(text))
        await event.send(MessageChain(chain))

    # ------------------------------------------------------------------
    # 上下文
    # ------------------------------------------------------------------
    def _build_context(
        self,
        event: Any,
        target_id: str,
        target_name: str,
        group_id: str,
        times: int,
    ) -> dict[str, str]:
        self_id = as_str(_safe(event, "get_self_id"))
        sender_id = as_str(_safe(event, "get_sender_id"))
        return {
            "bot": self.settings.bot_nickname,
            "bot_id": self_id,
            "target": target_name or target_id,
            "target_id": target_id,
            "sender": as_str(_safe(event, "get_sender_name")) or sender_id,
            "sender_id": sender_id,
            "group": group_id,
            "times": str(times),
        }


def _safe(event: Any, name: str) -> Any:
    func = getattr(event, name, None)
    if not callable(func):
        return ""
    try:
        return func() or ""
    except Exception:
        return ""
