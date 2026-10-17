"""人形化核心（humanoid_core）联动 —— 只读。

联动依据是 humanoid_core 主动导出的**版本化契约**：

    state.json → roles[<bot_id>].self.contract   （v1，字段只加不改）

`humanoid/link.py` 里写得很清楚：这是给别的插件读的，字段只增不删。
所以我们只读这个 contract，不碰它的任何内部结构，也不做任何写入。

读不到（没装 / 关掉了 contract_enabled / 路径不对）时一律返回 ``None``，
调用方静默降级，绝不影响戳一戳主流程。

用到的字段：

- ``feelings``    体感短句列表，例如 ["很困"]
- ``body.energy_text`` / ``body.asleep``
- ``activity.name``   此刻在做的事
- ``persona``         角色名
- ``form.max_chars``  她此刻愿意说多长
- ``roles.<bid>.users.<uid>.mood.affection``  对某个人的好感度（0-100）
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

#: 契约版本，读到更高版本也照常读（字段只加不改）
CONTRACT_VERSION = 1

#: 常见位置（相对 data 根目录）
_CANDIDATE_PATHS: tuple[str, ...] = (
    "plugin_data/humanoid_core/state.json",
    "plugin_data/astrbot_plugin_humanoid_core/state.json",
)

#: 找不到时做一次有界搜索的限制（不扫全盘）
_SEARCH_MAX_DEPTH = 3
_SEARCH_MAX_ENTRIES = 20000
_SEARCH_SKIP_DIRS = {
    "proc", "sys", "dev", "usr", "lib", "lib64", "bin", "sbin", "etc",
    "var", "snap", "node_modules", "__pycache__", ".git", "venv", "site-packages",
}

#: 未命中后的负缓存时长（秒）
_RETRY_SECONDS = 300.0


@dataclass(frozen=True)
class HumanoidSnapshot:
    """从人形化契约里抽出来的、戳一戳用得到的字段。"""

    mood: str = ""
    """体感短句拼接，例如「很困」。"""
    energy: str = ""
    """体力描述，例如「状态一般」。"""
    activity: str = ""
    """此刻在做的事。"""
    persona: str = ""
    """角色名。"""
    max_chars: int = 0
    """她此刻愿意说的字数上限；0 表示没给。"""
    asleep: bool = False
    """是否睡着。"""
    affection: str = ""
    """对当前这个人的好感度（0-100），没有记录时为空。"""

    def as_prompt_lines(self) -> list[str]:
        """给 LLM 的上下文行（只包含有值的项）。"""
        lines: list[str] = []
        if self.persona:
            lines.append(f"你的名字：{self.persona}")
        if self.mood:
            lines.append(f"你现在的体感：{self.mood}")
        if self.energy:
            lines.append(f"你的体力：{self.energy}")
        if self.activity:
            lines.append(f"你此刻在做：{self.activity}")
        if self.affection:
            lines.append(f"你对这个人的好感度：{self.affection}/100")
        if self.max_chars:
            lines.append(f"你此刻只想说 {self.max_chars} 个字以内")
        return lines

    def summary(self) -> str:
        parts = [p for p in (self.persona, self.mood, self.energy, self.activity) if p]
        return " · ".join(parts)


class HumanoidBridge:
    """读取 humanoid_core 契约的只读桥。"""

    def __init__(self, data_root: str | None = None, override_path: str = "") -> None:
        self._data_root = data_root or _default_data_root()
        self._override = (override_path or "").strip()
        self._resolved: str | None = None
        self._resolved_at = 0.0
        self._cache_key: tuple[str, float, int] | None = None
        self._cache_root: dict | None = None

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def available(self) -> bool:
        return bool(self._state_path())

    def snapshot(self, bot_id: str, user_id: str = "") -> HumanoidSnapshot | None:
        """取当前 bot / 用户对应的快照；读不到返回 ``None``。"""
        path = self._state_path()
        if not path:
            return None
        root = self._read_root(path)
        if not root:
            return None

        roles = root.get("roles")
        if not isinstance(roles, dict):
            return None
        role = roles.get(str(bot_id)) or roles.get("default")
        if not isinstance(role, dict):
            return None

        contract = _dig(role, "self", "contract")
        if not isinstance(contract, dict) or not contract:
            return None

        feelings = contract.get("feelings")
        mood = "、".join(str(x) for x in feelings[:3]) if isinstance(feelings, list) else ""
        body = contract.get("body") if isinstance(contract.get("body"), dict) else {}
        form = contract.get("form") if isinstance(contract.get("form"), dict) else {}
        activity = contract.get("activity") if isinstance(contract.get("activity"), dict) else {}

        return HumanoidSnapshot(
            mood=mood,
            energy=_text(body.get("energy_text")),
            activity=_text(activity.get("name")),
            persona=_text(contract.get("persona")),
            max_chars=_int(form.get("max_chars")),
            asleep=bool(body.get("asleep")),
            affection=self._affection(role, user_id),
        )

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------
    def _state_path(self) -> str | None:
        if self._override:
            return self._override if os.path.isfile(self._override) else None
        if self._resolved and os.path.isfile(self._resolved):
            return self._resolved
        now = time.monotonic()
        if self._resolved is None and now - self._resolved_at < _RETRY_SECONDS:
            return None

        self._resolved_at = now
        for rel in _CANDIDATE_PATHS:
            candidate = os.path.join(self._data_root, rel)
            if os.path.isfile(candidate):
                self._resolved = candidate
                return candidate

        found = _bounded_search(self._data_root)
        self._resolved = found
        return found

    def _read_root(self, path: str) -> dict | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        key = (path, stat.st_mtime, stat.st_size)
        if key == self._cache_key:
            return self._cache_root
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        self._cache_key = key
        self._cache_root = data if isinstance(data, dict) else None
        return self._cache_root

    @staticmethod
    def _affection(role: dict, user_id: str) -> str:
        users = role.get("users")
        if not isinstance(users, dict) or not user_id:
            return ""
        entry = users.get(str(user_id))
        if not isinstance(entry, dict):
            return ""
        value = _dig(entry, "mood", "affection")
        if isinstance(value, int | float):
            return f"{float(value):.0f}"
        return ""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _default_data_root() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return get_astrbot_data_path()
    except Exception:  # pragma: no cover - 非 AstrBot 环境
        return os.path.join(os.path.expanduser("~"), ".astrbot", "data")


def _dig(node: Any, *keys: str) -> Any:
    cur = node
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bounded_search(root: str) -> str | None:
    """在 data 根目录下有限深度地找 state.json，避免把面板卡死。"""
    if not root or not os.path.isdir(root):
        return None
    seen = 0
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _SEARCH_MAX_DEPTH:
            continue
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > _SEARCH_MAX_ENTRIES:
                return None
            try:
                if entry.is_file():
                    if entry.name == "state.json" and "humanoid" in current.lower():
                        return entry.path
                elif entry.is_dir() and entry.name not in _SEARCH_SKIP_DIRS:
                    stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return None
