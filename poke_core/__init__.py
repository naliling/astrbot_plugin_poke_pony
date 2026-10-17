"""新.戳一戳 的核心引擎。

模块划分：

- :mod:`poke_core.settings`  配置解析与平台能力画像（含自定义平台）
- :mod:`poke_core.detect`    跨平台「戳一戳」信号识别
- :mod:`poke_core.backends`  跨平台「回戳」执行器（三级降级）
- :mod:`poke_core.templates` 发言模板引擎（权重 / 轮换 / 洗牌袋）
- :mod:`poke_core.stats`     统计与排行榜（KV 持久化）
- :mod:`poke_core.ratelimit` 冷却、防骚扰滑动窗口与限流
- :mod:`poke_core.humanoid`  只读 humanoid_core 的版本化契约
"""

from __future__ import annotations

__version__ = "1.91.78"
