from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig
from astrbot.api import logger
import random


class PokePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._speech_index = 0  # 轮换模式下的当前索引

    @filter.event_message_type(EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        '''
        处理戳一戳事件。
        通过 raw_message 检测 OneBot 通知类事件中的 poke 子类型。
        '''
        # 获取原始 OneBot 事件(raw_message 非 OneBot 平台下可能是 tuple/其他类型)
        raw_event = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw_event, dict):
            return

        # 仅处理 poke 通知事件
        if not (
            raw_event.get("notice_type") == "notify"
            and raw_event.get("sub_type") == "poke"
        ):
            return

        self_id = raw_event.get("self_id")
        target_id = raw_event.get("target_id")
        user_id = raw_event.get("user_id")

        # 只处理别人戳自己的情况
        if not all([self_id, target_id, user_id]) or target_id != self_id:
            return

        logger.info(f"收到戳一戳事件: user_id={user_id}")

        # ===== 读取配置 =====
        speech_cfg = self.config.get("发言", {})
        poke_cfg = self.config.get("回戳", {})
        misc_cfg = self.config.get("杂项", {})
        group_id = raw_event.get("group_id")

        speech_enabled = speech_cfg.get("戳一戳发言开关", True)
        speech_prob = speech_cfg.get("戳一戳发言概率", 100)
        poke_enabled = poke_cfg.get("回戳开关", True)
        poke_prob = poke_cfg.get("回戳概率", 100)
        link_mode = misc_cfg.get("联动模式", "发言与回戳独立")

        # ===== 判定回戳是否触发（概率检测，统一用于联动判断） =====
        will_poke = poke_enabled and random.randint(1, 100) <= poke_prob

        # ===== 1. 发言功能（受联动模式影响） =====
        should_speech = speech_enabled and random.randint(1, 100) <= speech_prob
        if should_speech:
            if link_mode == "回戳时不发言" and will_poke:
                should_speech = False
            elif link_mode == "仅回戳时发言" and not will_poke:
                should_speech = False

        if should_speech:
            speech = self._pick_speech()
            if speech:
                logger.info(f"发言回复 {user_id}: {speech}")
                yield event.plain_result(speech)

        # ===== 2. 回戳功能 =====
        if will_poke:
            await self._poke_back(event, user_id, group_id)

    def _pick_speech(self) -> str | None:
        '''从发言列表中按配置模式选取一条发言内容'''
        speech_cfg = self.config.get("发言", {})
        speech_list = speech_cfg.get("戳一戳发言", [])
        if not speech_list:
            return None

        # 过滤已启用的发言（template_list 保存时包含 __template_key 字段）
        enabled = [s for s in speech_list if s.get("启用发言", True)]
        if not enabled:
            return None

        mode = speech_cfg.get("发言选取模式", "随机选取")

        if mode == "轮换":
            # 轮换模式：按列表顺序依次选取
            idx = self._speech_index % len(enabled)
            self._speech_index += 1
            return enabled[idx].get("发言内容", "")

        # 随机选取模式（默认）：按权重随机选择
        weights = [max(int(s.get("发言权重", 1)), 1) for s in enabled]
        total = sum(weights)
        if total <= 0:
            return enabled[-1].get("发言内容", "")

        r = random.uniform(0, total)
        cumulative = 0
        for s, w in zip(enabled, weights):
            cumulative += w
            if r <= cumulative:
                return s.get("发言内容", "")

        # 保底：返回最后一条
        return enabled[-1].get("发言内容", "")

    async def _poke_back(self, event: AstrMessageEvent, user_id: int, group_id: int | None = None):
        '''执行回戳（概率判定已在 on_poke 中完成）'''
        count = self.config.get("回戳", {}).get("回戳次数", 1)

        # 次数限制
        count = max(int(count), 0)
        if count <= 0:
            return

        # 判断场景：群聊用 group_poke，私聊用 friend_poke
        if group_id:
            action = "group_poke"
            params = {"group_id": group_id, "user_id": user_id}
            scene = f"群聊({group_id})"
        else:
            action = "friend_poke"
            params = {"user_id": user_id}
            scene = f"好友({user_id})"

        # 获取 bot 实例（AiocqhttpMessageEvent 上有 bot 属性）
        bot = getattr(event, "bot", None)
        if not bot:
            logger.error("回戳失败: 无法获取 bot 实例")
            return

        for i in range(count):
            try:
                await bot.api.call_action(action, **params)
                logger.info(f"回戳 {scene} 第 {i + 1}/{count} 次")
            except Exception as e:
                logger.error(f"回戳 {scene} 失败: {e}")
                break

    async def terminate(self):
        '''插件被卸载/停用时调用'''
        logger.info("戳一戳插件已卸载")
