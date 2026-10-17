"""发言模板引擎。

支持三种抽取模式：

- ``权重随机``：按「发言权重」加权抽取，权重越大越容易被选中；
- ``轮换``：严格按配置顺序循环，保证每条都会轮到；
- ``洗牌袋``：一轮内不重复（把整池洗牌后依次取完再洗），比纯随机更均匀。

模板支持变量插值，例如 ``{sender} 又戳我，第 {count} 次了！``。
未知变量会原样保留，避免用户写错变量名时整条发言变成空白。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

_VAR_PATTERN = re.compile(r"\{([^{}\s]{1,32})\}")

MODE_WEIGHTED = "权重随机"
MODE_ROTATE = "轮换"
MODE_BAG = "洗牌袋"


def render_template(text: str, context: Mapping[str, Any]) -> str:
    """把 ``{var}`` 替换成上下文里的值；未知变量原样保留。"""
    if not text or "{" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context:
            value = context[key]
            return "" if value is None else str(value)
        return match.group(0)

    return _VAR_PATTERN.sub(_replace, text)


@dataclass
class TemplatePool:
    """一组可抽取的发言模板。"""

    items: tuple[dict, ...]
    mode: str = MODE_WEIGHTED
    _index: int = 0
    _bag: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mode = self.mode if self.mode in {MODE_WEIGHTED, MODE_ROTATE, MODE_BAG} else MODE_WEIGHTED

    @property
    def enabled_items(self) -> list[dict]:
        return [item for item in self.items if item.get("enabled", True) and item.get("text")]

    def __bool__(self) -> bool:
        return bool(self.enabled_items)

    def __len__(self) -> int:
        return len(self.enabled_items)

    def pick(self, rng: random.Random | None = None) -> str | None:
        """抽取一条发言；池子为空时返回 ``None``。"""
        items = self.enabled_items
        if not items:
            return None
        rng = rng or random
        if self.mode == MODE_ROTATE:
            item = items[self._index % len(items)]
            self._index = (self._index + 1) % max(len(items), 1)
            return str(item.get("text", ""))
        if self.mode == MODE_BAG:
            return self._pick_from_bag(items, rng)
        return self._pick_weighted(items, rng)

    def _pick_weighted(self, items: list[dict], rng: random.Random) -> str:
        weights = [max(int(item.get("weight", 1) or 1), 1) for item in items]
        total = sum(weights)
        if total <= 0:
            return str(items[-1].get("text", ""))
        roll = rng.uniform(0, total)
        cumulative = 0.0
        for item, weight in zip(items, weights):
            cumulative += weight
            if roll <= cumulative:
                return str(item.get("text", ""))
        return str(items[-1].get("text", ""))

    def _pick_from_bag(self, items: list[dict], rng: random.Random) -> str:
        # 配置热更新后条目数量可能变化，越界就重新洗牌
        if not self._bag or max(self._bag) >= len(items):
            self._bag = list(range(len(items)))
            rng.shuffle(self._bag)
        index = self._bag.pop()
        self._index += 1
        return str(items[index].get("text", ""))
