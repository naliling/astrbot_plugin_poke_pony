"""冷却与限流。

四道闸门，防止「一群人围着机器人狂戳」把号戳没了：

1. **去重窗口**：同一条事件可能同时带 ``Poke`` 消息段和通知事件，只在主流程判一次；
2. **用户冷却**：同一用户 N 秒内只响应一次；
3. **防骚扰阈值**：同一用户 60 秒内被戳超过 N 次 → 静默（或按配置怼一句），不拉黑；
4. **全局每分钟上限**：所有用户合计每分钟最多响应 M 次，超出直接静默丢弃。

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


class BurstTracker:
    """滑动窗口计数器：统计「某个用户在最近 N 秒内被戳了几次」。

    防骚扰只用它做一件事——判断是否超过阈值。**不做拉黑**：
    超过阈值只是不再回应（或按配置怼一句），窗口滑过去就自动恢复。
    """

    def __init__(self, window: float = 60.0, clock=time.monotonic) -> None:
        self._window = max(float(window), 1.0)
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._last_purge = 0.0

    def note(self, key: str) -> int:
        """记录一次命中，返回窗口内的总次数（含本次）。"""
        now = self._clock()
        bucket = self._hits.get(key)
        if bucket is None:
            bucket = deque()
            self._hits[key] = bucket
        bucket.append(now)
        self._trim(bucket, now)
        return len(bucket)

    def count(self, key: str) -> int:
        bucket = self._hits.get(key)
        if not bucket:
            return 0
        self._trim(bucket, self._clock())
        return len(bucket)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def _trim(self, bucket: deque[float], now: float) -> None:
        while bucket and now - bucket[0] > self._window:
            bucket.popleft()

    def purge(self, ttl: float = 600.0) -> None:
        """清掉长时间没动静的 key。"""
        now = self._clock()
        if now - self._last_purge < 300.0:
            return
        self._last_purge = now
        for key in [k for k, b in self._hits.items() if not b or now - b[-1] > ttl]:
            self._hits.pop(key, None)

    def stats(self) -> dict[str, int]:
        return {"keys": len(self._hits)}

