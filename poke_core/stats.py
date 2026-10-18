"""统计、连击与排行榜。

数据通过 AstrBot 的插件 KV 存储持久化（``Star.get_kv_data`` / ``put_kv_data``），
不额外落文件，卸载插件后可在 WebUI 的插件数据里清理。

写入策略：内存累积 + 节流落盘（默认 15 秒一次，或跨天时强制落盘），
避免每条消息都打一次数据库。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

STORAGE_KEY = "poke_stats_v2"
SAVE_INTERVAL = 15.0
HISTORY_KEEP = 14


@dataclass
class UserStat:
    """单个用户的计数。"""

    poked: int = 0
    """被戳次数。"""
    pokes: int = 0
    """主动戳别人次数。"""
    name: str = ""
    """最近一次出现时的昵称（仅用于展示）。"""

    def to_dict(self) -> dict:
        return {"poked": self.poked, "pokes": self.pokes, "name": self.name}

    @classmethod
    def from_dict(cls, raw: Any) -> "UserStat":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            poked=_to_int(raw.get("poked")),
            pokes=_to_int(raw.get("pokes")),
            name=str(raw.get("name") or ""),
        )


def _to_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class Bucket:
    """一组计数（今日 / 累计）。

    多机器人时，``bots`` 记「每个号分别被戳了多少次」，``scoped`` 记
    「每个号下每个用户被戳了多少次」——排行榜仍按全体合并，但 ``{count}``
    这类「这是你第几次戳我」的变量必须按号算，否则换个号戳一次就变成第 2 次。
    """

    total: int = 0
    users: dict[str, UserStat] = field(default_factory=dict)
    bots: dict[str, int] = field(default_factory=dict)
    """``{bot_scope: 被戳次数}``。"""
    scoped: dict[str, dict[str, int]] = field(default_factory=dict)
    """``{bot_scope: {user_id: 被戳次数}}``。"""

    def get(self, user_id: str) -> UserStat:
        key = str(user_id or "")
        stat = self.users.get(key)
        if stat is None:
            stat = UserStat()
            self.users[key] = stat
        return stat

    def get_scoped(self, scope: str, user_id: str) -> int:
        return self.scoped.get(str(scope or ""), {}).get(str(user_id or ""), 0)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "users": {k: v.to_dict() for k, v in self.users.items()},
            "bots": dict(self.bots),
            "scoped": {k: dict(v) for k, v in self.scoped.items()},
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Bucket":
        if not isinstance(raw, dict):
            return cls()
        users_raw = raw.get("users")
        users = {}
        if isinstance(users_raw, dict):
            for key, value in users_raw.items():
                users[str(key)] = UserStat.from_dict(value)
        bots: dict[str, int] = {}
        bots_raw = raw.get("bots")
        if isinstance(bots_raw, dict):
            for key, value in bots_raw.items():
                bots[str(key)] = _to_int(value)
        scoped: dict[str, dict[str, int]] = {}
        scoped_raw = raw.get("scoped")
        if isinstance(scoped_raw, dict):
            for scope, per_user in scoped_raw.items():
                if not isinstance(per_user, dict):
                    continue
                scoped[str(scope)] = {str(k): _to_int(v) for k, v in per_user.items()}
        return cls(
            total=_to_int(raw.get("total")),
            users=users,
            bots=bots,
            scoped=scoped,
        )


class StatsStore:
    """统计中心。"""

    def __init__(self, plugin: Any = None, logger: Any = None, enabled: bool = True) -> None:
        self._plugin = plugin
        self._logger = logger
        self.enabled = enabled
        self.today = Bucket()
        self.all_time = Bucket()
        self.history: list[dict] = []
        self._day = date.today().isoformat()
        self._loaded = False
        self._dirty = False
        self._last_save = 0.0
        self._lock = asyncio.Lock()
        # 连击：仅内存，用于「连戳彩蛋」
        self._last_poke_at: dict[str, float] = {}
        self._streak: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def load(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            self._loaded = True
            raw = None
            if not self._storage_ready():
                self._log("debug", "插件未注入 plugin_id，统计仅保存在内存中")
            else:
                try:
                    raw = await self._plugin.get_kv_data(STORAGE_KEY, None)
                except Exception as exc:  # pragma: no cover - 存储不可用时降级为内存模式
                    self._log("warning", f"读取统计失败，已降级为内存模式：{exc}")
                    raw = None
            if isinstance(raw, dict):
                self._day = str(raw.get("date") or self._day)
                self.today = Bucket.from_dict(raw.get("today"))
                self.all_time = Bucket.from_dict(raw.get("all"))
                history = raw.get("history")
                self.history = [h for h in history if isinstance(h, dict)][-HISTORY_KEEP:] if isinstance(history, list) else []
            self._rollover_if_needed()

    async def flush(self) -> None:
        """强制落盘（插件卸载 / 配置重载时调用）。"""
        if not self._dirty:
            return
        await self._save(force=True)

    async def save_if_due(self) -> None:
        if not self._dirty:
            return
        if time.monotonic() - self._last_save < SAVE_INTERVAL:
            return
        await self._save(force=False)

    async def _save(self, force: bool = False) -> None:
        if not self.enabled or not self._storage_ready():
            self._dirty = False
            return
        payload = self.snapshot()
        try:
            await self._plugin.put_kv_data(STORAGE_KEY, payload)
        except Exception as exc:  # pragma: no cover
            self._log("warning", f"写入统计失败：{exc}")
            return
        self._dirty = False
        self._last_save = time.monotonic()

    def _storage_ready(self) -> bool:
        """真实运行时 Star 会被注入 plugin_id；没有它就无法访问 KV 存储。"""
        return bool(getattr(self._plugin, "plugin_id", "")) and callable(
            getattr(self._plugin, "put_kv_data", None),
        )

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    def record_incoming(self, user_id: str, name: str = "", scope: str = "") -> int:
        """记录一次「被戳」，返回**该机器人下**该用户今日被戳次数。

        ``scope`` 是 ``platform:self_id``；为空时退化为全体合并计数。
        """
        self._rollover_if_needed()
        scope = str(scope or "")
        user_key = str(user_id or "")
        stat = self.today.get(user_id)
        stat.poked += 1
        stat.name = name or stat.name
        self.today.total += 1
        all_stat = self.all_time.get(user_id)
        all_stat.poked += 1
        all_stat.name = name or all_stat.name
        self.all_time.total += 1
        count = stat.poked
        if scope:
            self.today.bots[scope] = self.today.bots.get(scope, 0) + 1
            self.all_time.bots[scope] = self.all_time.bots.get(scope, 0) + 1
            today_scoped = self.today.scoped.setdefault(scope, {})
            today_scoped[user_key] = today_scoped.get(user_key, 0) + 1
            all_scoped = self.all_time.scoped.setdefault(scope, {})
            all_scoped[user_key] = all_scoped.get(user_key, 0) + 1
            count = today_scoped[user_key]
        self._dirty = True
        self._bump_streak(user_id, scope)
        return count

    def record_outgoing(self, user_id: str, name: str = "") -> None:
        """记录一次「主动戳别人」。"""
        self._rollover_if_needed()
        stat = self.today.get(user_id)
        stat.pokes += 1
        stat.name = name or stat.name
        all_stat = self.all_time.get(user_id)
        all_stat.pokes += 1
        all_stat.name = name or all_stat.name
        self._dirty = True

    # ------------------------------------------------------------------
    # 连击
    # ------------------------------------------------------------------
    def _bump_streak(self, user_id: str, scope: str = "", window: float = 45.0) -> int:
        key = self._streak_key(user_id, scope)
        now = time.monotonic()
        last = self._last_poke_at.get(key)
        if last is not None and now - last <= window:
            self._streak[key] = self._streak.get(key, 1) + 1
        else:
            self._streak[key] = 1
        self._last_poke_at[key] = now
        return self._streak[key]

    def streak(self, user_id: str, scope: str = "") -> int:
        return self._streak.get(self._streak_key(user_id, scope), 1)

    @staticmethod
    def _streak_key(user_id: str, scope: str = "") -> str:
        """连击按「机器人 + 用户」分别累计，避免 A 号戳一下、B 号戳一下被算成连击。"""
        return f"{scope}|{user_id}" if scope else str(user_id or "")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def today_poked(self, user_id: str) -> int:
        stat = self.today.users.get(str(user_id or ""))
        return stat.poked if stat else 0

    def ranking(
        self,
        limit: int = 10,
        bucket: str = "today",
        scope: str = "",
    ) -> list[tuple[str, int, str]]:
        """排行榜。``scope`` 非空时只统计「这个号」被戳的次数。"""
        source = self.today if bucket == "today" else self.all_time
        scope = str(scope or "")
        if scope:
            counts = source.scoped.get(scope, {})
            rows = [
                (uid, int(count), (source.users.get(uid).name if source.users.get(uid) else ""))
                for uid, count in counts.items()
                if int(count) > 0
            ]
        else:
            rows = [
                (uid, stat.poked, stat.name)
                for uid, stat in source.users.items()
                if stat.poked > 0
            ]
        rows.sort(key=lambda item: (-item[1], item[0]))
        return rows[: max(limit, 1)]

    def summary(self, scope: str = "") -> dict:
        return {
            "today_total": self.today.total,
            "all_total": self.all_time.total,
            "today_users": len([s for s in self.today.users.values() if s.poked]),
            "today_bot": self.today.bots.get(str(scope or ""), 0),
            "all_bot": self.all_time.bots.get(str(scope or ""), 0),
            "history": self.history[-HISTORY_KEEP:],
        }

    def snapshot(self) -> dict:
        return {
            "date": self._day,
            "today": self.today.to_dict(),
            "all": self.all_time.to_dict(),
            "history": self.history[-HISTORY_KEEP:],
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _rollover_if_needed(self) -> None:
        today_str = date.today().isoformat()
        if today_str == self._day:
            return
        if self.today.total > 0:
            self.history.append({"date": self._day, "total": self.today.total})
            self.history = self.history[-HISTORY_KEEP:]
        self.today = Bucket()
        self._day = today_str
        self._dirty = True

    def _log(self, level: str, message: str) -> None:
        func = getattr(self._logger, level, None)
        if callable(func):
            func(f"[stats] {message}")
