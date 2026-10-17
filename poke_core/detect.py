"""跨平台「戳一戳」信号识别。

不同平台对「戳一戳」的支持差异极大：

============  ==========================================  ==============================
平台           原生信号                                     本插件识别方式
============  ==========================================  ==============================
QQ / OneBot    ``notice.notify.poke`` 通知 / ``Poke`` 消息段   原生识别，零配置
QQ 官方机器人   无                                           触发词 / ``/戳`` 指令
Telegram      无                                           触发词 / ``/poke`` 指令
飞书           无                                           触发词 / ``/戳`` 指令
企业微信       无                                           触发词 / ``/戳`` 指令
微信个人号      无                                           触发词 / ``/戳`` 指令
============  ==========================================  ==============================

因此这里做了「双通道」识别：**原生通道**优先，**触发词通道**兜底。
组件类型全部用鸭子类型判断，避免因 AstrBot 版本差异导致 import 失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .settings import Settings, as_str


@dataclass(slots=True)
class PokeSignal:
    """一次「戳一戳」信号。"""

    platform: str
    source: str
    """``native`` 原生戳 / ``keyword`` 触发词。"""
    sender_id: str
    sender_name: str
    target_id: str
    target_name: str
    group_id: str
    is_group: bool
    times: int = 1

    @property
    def is_native(self) -> bool:
        return self.source == "native"


# ---------------------------------------------------------------------------
# 组件读取（全部鸭子类型，兼容不同 AstrBot 版本）
# ---------------------------------------------------------------------------


def _kind(component: Any) -> str:
    return type(component).__name__


def _iter_components(event: Any) -> list[Any]:
    try:
        return list(event.get_messages() or [])
    except Exception:  # pragma: no cover - 适配器异常时静默降级
        return []


def component_target_id(component: Any) -> str:
    """从 ``Poke`` / ``At`` 组件里取出目标 ID。"""
    getter = getattr(component, "target_id", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = None
        if value not in (None, "", 0, "0"):
            return str(value)
    for attr in ("qq", "id", "user_id"):
        value = getattr(component, attr, None)
        if value not in (None, "", 0, "0"):
            return str(value)
    return ""


def first_at_target(event: Any) -> str:
    """取消息链里第一个 At 的目标 ID（用于 ``/戳 @某人``）。"""
    for component in _iter_components(event):
        if _kind(component) == "At":
            target = component_target_id(component)
            if target and target != "all":
                return target
    return ""


def reply_target(event: Any) -> str:
    """取被引用（回复）消息的发送者 ID，用于「回复某人的消息 + /戳」。"""
    for component in _iter_components(event):
        if _kind(component) == "Reply":
            sender = getattr(component, "sender_id", None)
            if sender not in (None, "", 0, "0"):
                return str(sender)
    return ""


def _raw_event(event: Any) -> dict:
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# 识别入口
# ---------------------------------------------------------------------------


def detect_poke(event: Any, settings: Settings) -> PokeSignal | None:
    """识别一次戳一戳信号；不是戳一戳事件时返回 ``None``。"""
    platform = as_str(getattr(event, "get_platform_name", lambda: "")()).lower()
    if not platform:
        platform = "unknown"

    signal = _detect_native(event, platform, settings)
    if signal is not None:
        return signal

    return _detect_keyword(event, platform, settings)


def _detect_native(event: Any, platform: str, settings: Settings) -> PokeSignal | None:
    """原生通道：``Poke`` 消息段 或 OneBot 通知事件。"""
    target_id = ""
    sender_id = ""

    for component in _iter_components(event):
        if _kind(component) == "Poke":
            target_id = component_target_id(component)
            if target_id:
                break

    if not target_id:
        raw = _raw_event(event)
        # OneBot v11: post_type=notice, notice_type=notify, sub_type=poke
        if raw.get("sub_type") == "poke" or raw.get("notice_type") == "poke":
            target_id = as_str(raw.get("target_id"))
            sender_id = as_str(raw.get("user_id"))

    if not target_id:
        return None

    self_id = as_str(_safe_call(event, "get_self_id"))
    if settings.only_self_target and self_id and target_id != self_id:
        # 群里 A 戳 B，机器人不掺和
        return None

    sender_id = sender_id or as_str(_safe_call(event, "get_sender_id"))
    if self_id and sender_id == self_id:
        # 机器人自己戳自己（部分协议端会回显），直接忽略
        return None

    return PokeSignal(
        platform=platform,
        source="native",
        sender_id=sender_id,
        sender_name=as_str(_safe_call(event, "get_sender_name")) or sender_id,
        target_id=target_id,
        target_name=self_id if target_id == self_id else target_id,
        group_id=as_str(_safe_call(event, "get_group_id")),
        is_group=bool(as_str(_safe_call(event, "get_group_id"))) and not _is_private(event),
    )


def _detect_keyword(event: Any, platform: str, settings: Settings) -> PokeSignal | None:
    """触发词通道：消息内容恰好等于某个触发词。"""
    if not settings.text_trigger_allowed(platform):
        return None

    text = as_str(_safe_call(event, "get_message_str")).strip()
    if not text:
        return None
    normalized = text.lstrip("/／!！.。").strip().casefold()
    if not normalized:
        return None

    triggers = {trigger.casefold() for trigger in settings.triggers}
    if normalized not in triggers:
        return None

    self_id = as_str(_safe_call(event, "get_self_id"))
    at_target = first_at_target(event)
    target_id = at_target or self_id
    if not target_id:
        return None

    sender_id = as_str(_safe_call(event, "get_sender_id"))
    if self_id and sender_id == self_id:
        return None

    return PokeSignal(
        platform=platform,
        source="keyword",
        sender_id=sender_id,
        sender_name=as_str(_safe_call(event, "get_sender_name")) or sender_id,
        target_id=target_id,
        target_name=self_id if target_id == self_id else at_target,
        group_id=as_str(_safe_call(event, "get_group_id")),
        is_group=bool(as_str(_safe_call(event, "get_group_id"))) and not _is_private(event),
    )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _safe_call(event: Any, name: str) -> Any:
    func = getattr(event, name, None)
    if not callable(func):
        return ""
    try:
        return func() or ""
    except Exception:
        return ""


def _is_private(event: Any) -> bool:
    try:
        return bool(event.is_private_chat())
    except Exception:
        return False
