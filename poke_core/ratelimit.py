"""冷却与限流。

三道闸门，防止「一群人围着机器人狂戳」把号戳没了：

1. **去重窗口**：同一条事件可能同时带 ``Poke`` 消息段和通知事件，只在主流程判一次；
2. **用户冷却**：同一用户 N 秒内只响应一次；
3. **每分钟上限**：全局每分钟最多响应 M 次，超出直接静默丢弃。

全部是纯内存实现，重启即清空——限流数据本来就不该持久化。
"""

from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    """线程/协程安全的轻量限流器（AstrBot 事件循环内单线程使用）。"""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._last_seen: dict[str, float] = {}
        self._window: deque[float] = deque()
        self._last_purge = 0.0

    # ------------------------------------------------------------------
    # 用户冷却
    # ------------------------------------------------------------------
    def check_user(self, user_id: str, cooldown: float) -> tuple[bool, float]:
        """返回 ``(是否放行, 还需等待秒数)``。"""
        key = str(user_id or "")
        if not key or cooldown <= 0:
            return True, 0.0
        now = self._clock()
        last = self._last_seen.get(key)
        if last is not None:
            elapsed = now - last
            if elapsed < cooldown:
                return False, cooldown - elapsed
        return True, 0.0

    def touch_user(self, user_id: str) -> None:
        key = str(user_id or "")
        if key:
            self._last_seen[key] = self._clock()

    def clear_user(self, user_id: str) -> None:
        self._last_seen.pop(str(user_id or ""), None)

    # ------------------------------------------------------------------
    # 全局限流
    # ------------------------------------------------------------------
    def check_global(self, per_minute: int) -> bool:
        if per_minute <= 0:
            return True
        now = self._clock()
        self._trim(now)
        return len(self._window) < per_minute

    def touch_global(self) -> None:
        now = self._clock()
        self._window.append(now)
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self._window and now - self._window[0] > 60.0:
            self._window.popleft()

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def purge(self, ttl: float = 3600.0) -> None:
        """清理长时间不活跃的用户记录，避免内存无限增长。"""
        now = self._clock()
        if now - self._last_purge < 300.0:
            return
        self._last_purge = now
        expired = [key for key, ts in self._last_seen.items() if now - ts > ttl]
        for key in expired:
            self._last_seen.pop(key, None)
        self._trim(now)

    def stats(self) -> dict[str, int]:
        return {"users": len(self._last_seen), "window": len(self._window)}
