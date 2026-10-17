"""配置解析层。

AstrBot 的插件配置是「WebUI 表单 -> JSON dict」，键名是中文、类型可能是
字符串或数字，而且老用户升级后配置里往往缺字段。本模块负责把这些原始数据
收敛成一个**只读、强类型、带默认值**的 :class:`Settings`，让业务代码不用
到处写 ``.get(..., {})`` 和 try/except。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 平台能力画像
# ---------------------------------------------------------------------------

#: 这些平台的「戳一戳」是协议层原生能力，可以直接调用 API 回戳
NATIVE_POKE_PLATFORMS: frozenset[str] = frozenset({"aiocqhttp", "satori"})

#: 这些平台支持 At 消息段（可以真的 @ 到人）
AT_CAPABLE_PLATFORMS: frozenset[str] = frozenset({"aiocqhttp", "satori"})


@dataclass(frozen=True)
class PlatformProfile:
    """某个平台适配器的「戳一戳」能力画像。"""

    name: str
    native_poke: bool
    """是否支持原生戳一戳 API。"""
    supports_at: bool
    """是否支持 At 消息段。"""
    text_template: str
    """无法原生回戳时使用的拟真文案模板。"""
    emoji: str = ""
    """拟真文案前的点缀表情。"""

    def render_template(self) -> str:
        if self.emoji and not self.text_template.startswith(self.emoji):
            return f"{self.emoji} {self.text_template}"
        return self.text_template


#: 各平台默认画像。键名与 AstrBot ``ADAPTER_NAME_2_TYPE`` 保持一致。
DEFAULT_PROFILES: dict[str, PlatformProfile] = {
    "aiocqhttp": PlatformProfile(
        name="aiocqhttp",
        native_poke=True,
        supports_at=True,
        text_template="（{bot} 反手戳了戳 {target}）",
    ),
    "satori": PlatformProfile(
        name="satori",
        native_poke=True,
        supports_at=True,
        text_template="（{bot} 反手戳了戳 {target}）",
    ),
    "qq_official": PlatformProfile(
        name="qq_official",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
    "qq_official_webhook": PlatformProfile(
        name="qq_official_webhook",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
    "telegram": PlatformProfile(
        name="telegram",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
    "lark": PlatformProfile(
        name="lark",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👆",
    ),
    "wecom": PlatformProfile(
        name="wecom",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 拍了拍 {target}",
        emoji="👋",
    ),
    "wecom_ai_bot": PlatformProfile(
        name="wecom_ai_bot",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 拍了拍 {target}",
        emoji="👋",
    ),
    "weixin_oc": PlatformProfile(
        name="weixin_oc",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 拍了拍 {target}",
        emoji="👋",
    ),
    "weixin_official_account": PlatformProfile(
        name="weixin_official_account",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 拍了拍 {target}",
        emoji="👋",
    ),
    "dingtalk": PlatformProfile(
        name="dingtalk",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
    "discord": PlatformProfile(
        name="discord",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
    "slack": PlatformProfile(
        name="slack",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
    "kook": PlatformProfile(
        name="kook",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
    "line": PlatformProfile(
        name="line",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
    "misskey": PlatformProfile(
        name="misskey",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
    "mattermost": PlatformProfile(
        name="mattermost",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
    "webchat": PlatformProfile(
        name="webchat",
        native_poke=False,
        supports_at=False,
        text_template="{bot} 戳了戳 {target}",
        emoji="👉",
    ),
}

#: 未知适配器的兜底画像：纯文本拟真，保证「任何平台都能跑」。
FALLBACK_PROFILE = PlatformProfile(
    name="default",
    native_poke=False,
    supports_at=False,
    text_template="{bot} 戳了戳 {target}",
    emoji="👉",
)

#: 「触发词」默认生效的平台（原生戳能力之外的平台）
DEFAULT_TEXT_TRIGGER_PLATFORMS: tuple[str, ...] = (
    "telegram",
    "lark",
    "wecom",
    "wecom_ai_bot",
    "weixin_oc",
    "weixin_official_account",
    "qq_official",
    "qq_official_webhook",
    "dingtalk",
    "discord",
    "slack",
    "kook",
    "line",
    "misskey",
    "mattermost",
    "webchat",
)

DEFAULT_TRIGGERS: tuple[str, ...] = ("戳一戳", "戳戳", "拍一拍", "戳我", "nudge")

#: 配置里一条发言模板都没有时的兜底发言（避免「装好插件却一声不吭」）
DEFAULT_SPEECH_POOL: tuple[dict, ...] = (
    {"text": "（被戳得一激灵）谁啊！", "weight": 1, "enabled": True},
    {"text": "戳什么戳，再戳我可要还手了。", "weight": 1, "enabled": True},
    {"text": "（抖了抖）今天第 {count} 次了，手指不疼吗？", "weight": 2, "enabled": True},
    {"text": "在的在的，别戳啦～", "weight": 1, "enabled": True},
)


# ---------------------------------------------------------------------------
# 取值小工具
# ---------------------------------------------------------------------------


def _section(raw: Any, *names: str) -> dict:
    """按顺序取第一个存在的配置小节，取不到返回空 dict。"""
    if not isinstance(raw, dict):
        return {}
    for name in names:
        value = raw.get(name)
        if isinstance(value, dict):
            return value
    return {}


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "开", "是"}:
            return True
        if lowered in {"false", "0", "no", "off", "关", "否"}:
            return False
    return default


def as_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        number = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def as_str_list(value: Any) -> tuple[str, ...]:
    """把 list / 逗号分隔字符串 统一成去重后的字符串元组。"""
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("，", ",").split(",")]
    elif isinstance(value, Iterable) and not isinstance(value, dict | bytes):
        parts = [as_str(p).strip() for p in value]
    else:
        return ()
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return tuple(seen)


def parse_hhmm(value: Any) -> int | None:
    """``"23:30"`` -> ``1410``（当天第几分钟）。解析失败返回 None。"""
    text = as_str(value).strip()
    if not text:
        return None
    for sep in (":", "：", "."):
        if sep in text:
            left, _, right = text.partition(sep)
            try:
                hour, minute = int(left), int(right)
            except ValueError:
                return None
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour * 60 + minute
            return None
    try:
        hour = int(text)
    except ValueError:
        return None
    if 0 <= hour <= 23:
        return hour * 60
    return None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """一次事件处理所需的全部配置快照。"""

    # ---- 基础 ----
    enabled: bool = True
    platforms: tuple[str, ...] = ()
    group_enabled: bool = True
    private_enabled: bool = True
    only_self_target: bool = True
    user_cooldown: float = 3.0
    per_minute_limit: int = 30
    dedupe_window_ms: int = 800
    quiet_enabled: bool = False
    quiet_start: int | None = None
    quiet_end: int | None = None
    bot_nickname: str = "我"

    # ---- 发言 ----
    speech_enabled: bool = True
    speech_probability: int = 100
    speech_mode: str = "权重随机"
    speech_at: bool = True
    speech_pool: tuple[dict, ...] = ()
    combo_threshold: int = 0
    combo_pool: tuple[dict, ...] = ()
    link_mode: str = "发言与回戳独立"

    # ---- 回戳 ----
    poke_back_enabled: bool = True
    poke_back_probability: int = 100
    poke_back_times: int = 1
    poke_back_interval_ms: int = 300
    prefer_native: bool = True
    native_fallback: bool = True
    chain_poke: bool = False
    poke_back_text: str = "（{bot} 反手戳了戳 {target}）"

    # ---- 指令 ----
    triggers: tuple[str, ...] = DEFAULT_TRIGGERS
    text_trigger_platforms: tuple[str, ...] = DEFAULT_TEXT_TRIGGER_PLATFORMS
    active_poke_enabled: bool = True
    active_poke_admin_only: bool = False
    active_poke_default_times: int = 1
    active_poke_cooldown: float = 5.0

    # ---- 名单 ----
    group_whitelist: tuple[str, ...] = ()
    group_blacklist: tuple[str, ...] = ()
    user_blacklist: tuple[str, ...] = ()

    # ---- 统计 ----
    stats_enabled: bool = True
    ranking_size: int = 10

    # ---- 智能 ----
    llm_tool_enabled: bool = False

    # ---- 平台 ----
    profiles: dict[str, PlatformProfile] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, raw: Any) -> "Settings":
        raw = raw if isinstance(raw, dict) else {}

        base = _section(raw, "基础设置", "base")
        speech = _section(raw, "发言设置", "发言", "speech")
        poke = _section(raw, "回戳设置", "回戳", "poke")
        cmd = _section(raw, "指令设置", "command")
        lists = _section(raw, "名单设置", "lists")
        stats = _section(raw, "统计设置", "stats")
        smart = _section(raw, "智能功能", "smart")
        platforms_cfg = _section(raw, "平台适配", "platforms")

        profiles = _build_profiles(platforms_cfg)

        return cls(
            enabled=as_bool(base.get("总开关"), True),
            platforms=as_str_list(base.get("启用平台")),
            group_enabled=as_bool(base.get("群聊启用"), True),
            private_enabled=as_bool(base.get("私聊启用"), True),
            only_self_target=as_bool(base.get("只响应戳机器人"), True),
            user_cooldown=float(as_int(base.get("用户冷却秒数"), 3, 0, 3600)),
            per_minute_limit=as_int(base.get("每分钟上限"), 30, 0, 600),
            dedupe_window_ms=as_int(base.get("去重窗口毫秒"), 800, 0, 10000),
            quiet_enabled=as_bool(base.get("安静时段开关"), False),
            quiet_start=parse_hhmm(base.get("安静时段开始")),
            quiet_end=parse_hhmm(base.get("安静时段结束")),
            bot_nickname=as_str(base.get("机器人昵称"), "我") or "我",
            speech_enabled=as_bool(speech.get("发言开关"), True),
            speech_probability=as_int(speech.get("发言概率"), 100, 0, 100),
            speech_mode=as_str(speech.get("选取模式"), "权重随机"),
            speech_at=as_bool(speech.get("附带艾特"), True),
            speech_pool=_normalize_pool(speech.get("发言模板")) or DEFAULT_SPEECH_POOL,
            combo_threshold=as_int(speech.get("连击彩蛋阈值"), 0, 0, 1000),
            combo_pool=_normalize_pool(speech.get("连击彩蛋模板")),
            link_mode=as_str(poke.get("联动模式"), "发言与回戳独立"),
            poke_back_enabled=as_bool(poke.get("回戳开关"), True),
            poke_back_probability=as_int(poke.get("回戳概率"), 100, 0, 100),
            poke_back_times=as_int(poke.get("回戳次数"), 1, 0, 10),
            poke_back_interval_ms=as_int(poke.get("回戳间隔毫秒"), 300, 0, 5000),
            prefer_native=as_bool(poke.get("优先原生回戳"), True),
            native_fallback=as_bool(poke.get("原生失败降级"), True),
            chain_poke=as_bool(poke.get("链式回戳（实验）"), False),
            poke_back_text=as_str(poke.get("回戳文案"), "（{bot} 反手戳了戳 {target}）"),
            triggers=as_str_list(cmd.get("触发词")) or DEFAULT_TRIGGERS,
            text_trigger_platforms=as_str_list(cmd.get("文本触发平台"))
            or DEFAULT_TEXT_TRIGGER_PLATFORMS,
            active_poke_enabled=as_bool(cmd.get("主动戳开关"), True),
            active_poke_admin_only=as_bool(cmd.get("主动戳仅管理员"), False),
            active_poke_default_times=as_int(cmd.get("主动戳默认次数"), 1, 1, 10),
            active_poke_cooldown=float(as_int(cmd.get("主动戳冷却秒数"), 5, 0, 3600)),
            group_whitelist=as_str_list(lists.get("群白名单")),
            group_blacklist=as_str_list(lists.get("群黑名单")),
            user_blacklist=as_str_list(lists.get("用户黑名单")),
            stats_enabled=as_bool(stats.get("统计开关"), True),
            ranking_size=as_int(stats.get("排行榜长度"), 10, 1, 50),
            llm_tool_enabled=as_bool(smart.get("LLM工具开关"), False),
            profiles=profiles,
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def profile_for(self, platform: str) -> PlatformProfile:
        return self.profiles.get(platform) or DEFAULT_PROFILES.get(platform) or FALLBACK_PROFILE

    def platform_allowed(self, platform: str) -> bool:
        if not self.platforms:
            return True
        return platform in self.platforms

    def text_trigger_allowed(self, platform: str) -> bool:
        if not self.text_trigger_platforms:
            return True
        return platform in self.text_trigger_platforms

    def group_allowed(self, group_id: str) -> bool:
        gid = as_str(group_id).strip()
        if not gid:
            return True
        if self.group_blacklist and gid in self.group_blacklist:
            return False
        if self.group_whitelist:
            return gid in self.group_whitelist
        return True

    def user_allowed(self, user_id: str) -> bool:
        uid = as_str(user_id).strip()
        return not (uid and uid in self.user_blacklist)

    def in_quiet_hours(self, minutes_of_day: int) -> bool:
        """判断当前是否处于安静时段（支持跨零点）。"""
        if not self.quiet_enabled:
            return False
        start, end = self.quiet_start, self.quiet_end
        if start is None or end is None or start == end:
            return False
        if start < end:
            return start <= minutes_of_day < end
        return minutes_of_day >= start or minutes_of_day < end


def _build_profiles(platforms_cfg: dict) -> dict[str, PlatformProfile]:
    """把「平台适配」配置小节合并进默认画像。"""
    merged: dict[str, PlatformProfile] = dict(DEFAULT_PROFILES)
    for name, raw in platforms_cfg.items():
        if not isinstance(raw, dict):
            continue
        default = DEFAULT_PROFILES.get(name) or PlatformProfile(
            name=name,
            native_poke=False,
            supports_at=False,
            text_template=FALLBACK_PROFILE.text_template,
            emoji=FALLBACK_PROFILE.emoji,
        )
        text_template = as_str(raw.get("拟真文案"), default.text_template) or default.text_template
        merged[name] = PlatformProfile(
            name=name,
            native_poke=as_bool(raw.get("原生回戳"), default.native_poke),
            supports_at=as_bool(raw.get("支持艾特"), default.supports_at),
            text_template=text_template,
            emoji=as_str(raw.get("表情前缀"), default.emoji),
        )
    return merged


def _normalize_pool(value: Any) -> tuple[dict, ...]:
    """把 template_list 配置整理成统一结构，丢弃空条目。"""
    if not isinstance(value, list):
        return ()
    pool: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = as_str(item.get("发言内容") or item.get("内容") or item.get("text")).strip()
        if not text:
            continue
        pool.append(
            {
                "text": text,
                "weight": as_int(item.get("发言权重") or item.get("权重"), 1, 1, 1000),
                "enabled": as_bool(item.get("启用发言", True), True),
            }
        )
    return tuple(pool)
