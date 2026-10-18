"""配置解析层。

AstrBot 的插件配置是「WebUI 表单 -> JSON dict」，键名是中文、类型可能是
字符串或数字，而且老用户升级后配置里往往缺字段。本模块负责把这些原始数据
收敛成一个**只读、强类型、带默认值**的 :class:`Settings`，让业务代码不用
到处写 ``.get(..., {})`` 和 try/except。

设计约定（v1.91.79 起）：

- **台词类配置一律留空**：发言模板、彩蛋模板、怼人模板默认都是空列表，
  由使用者自己写。插件不预设任何「机器人该说什么」。
- **功能类配置保留默认**：触发词这类「入口」保留占位值，保证装上就能被触发。
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
    """无法原生回戳时使用的拟真文案模板；留空表示不做拟真回戳。"""
    emoji: str = ""
    """拟真文案前的点缀表情。"""

    def render_template(self) -> str:
        if not self.text_template:
            return ""
        if self.emoji and not self.text_template.startswith(self.emoji):
            return f"{self.emoji} {self.text_template}"
        return self.text_template


def _profile(name: str, native: bool = False, at: bool = False) -> PlatformProfile:
    """构造一个「拟真文案留空」的平台画像。"""
    return PlatformProfile(
        name=name,
        native_poke=native,
        supports_at=at,
        text_template="",
        emoji="",
    )


#: 各平台默认画像。键名与 AstrBot ``ADAPTER_NAME_2_TYPE`` 保持一致。
#: 拟真文案一律留空：想用哪个平台，就在「平台适配」里自己写。
DEFAULT_PROFILES: dict[str, PlatformProfile] = {
    "aiocqhttp": _profile("aiocqhttp", native=True, at=True),
    "satori": _profile("satori", native=True, at=True),
    "qq_official": _profile("qq_official"),
    "qq_official_webhook": _profile("qq_official_webhook"),
    "telegram": _profile("telegram"),
    "lark": _profile("lark"),
    "wecom": _profile("wecom"),
    "wecom_ai_bot": _profile("wecom_ai_bot"),
    "weixin_oc": _profile("weixin_oc"),
    "weixin_official_account": _profile("weixin_official_account"),
    "dingtalk": _profile("dingtalk"),
    "discord": _profile("discord"),
    "slack": _profile("slack"),
    "kook": _profile("kook"),
    "line": _profile("line"),
    "misskey": _profile("misskey"),
    "mattermost": _profile("mattermost"),
    "webchat": _profile("webchat"),
    "matrix": _profile("matrix"),
    "vocechat": _profile("vocechat"),
}

#: 未知适配器的兜底画像：保证「任何平台都能跑」，只是没有拟真文案。
FALLBACK_PROFILE = _profile("default")

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
    "matrix",
    "vocechat",
)

#: 触发词是「入口」而不是「台词」，所以保留默认值，保证非 QQ 平台装上就能被戳。
DEFAULT_TRIGGERS: tuple[str, ...] = ("戳一戳", "戳戳", "拍一拍", "戳我", "nudge")

#: 防骚扰触发后的动作
HARASS_SILENT = "静默不回应"
HARASS_SILENT_AND_REPLY = "静默并怼人"
HARASS_REPLY_ONLY = "只怼人"


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


def parse_bot_aliases(value: Any) -> dict[str, str]:
    """解析「多机器人别名」：``"3632823490=娜莉灵,2625379556=露娜"``。

    分隔符容忍逗号 / 中文逗号 / 分号 / 换行；``=`` ``:`` ``：`` 都可以当分隔符。
    解析不出来的片段直接跳过，绝不因为一行写错就丢掉整份别名表。
    """
    aliases: dict[str, str] = {}
    text = as_str(value).strip()
    if not text:
        return aliases
    for raw_line in text.replace("；", ";").replace("\n", ";").split(";"):
        for chunk in raw_line.replace("，", ",").split(","):
            item = chunk.strip()
            if not item:
                continue
            for sep in ("=", ":", "："):
                if sep in item:
                    left, _, right = item.partition(sep)
                    key = left.strip().lstrip("@")
                    name = right.strip()
                    if key and name:
                        aliases[key] = name
                    break
    return aliases


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
    bot_nickname: str = "我"
    bot_aliases: dict[str, str] = field(default_factory=dict)
    """``{机器人ID: 名字}``，多机器人时给每个号一个名字（模板变量 ``{bot}``）。"""
    user_cooldown: float = 3.0
    dedupe_window_ms: int = 800
    quiet_enabled: bool = False
    quiet_start: int | None = None
    quiet_end: int | None = None

    # ---- 发言 ----
    speech_enabled: bool = True
    speech_probability: int = 100
    speech_mode: str = "权重随机"
    speech_at: bool = True
    speech_pool: tuple[dict, ...] = ()
    combo_threshold: int = 5
    combo_pool: tuple[dict, ...] = ()

    # ---- LLM ----
    llm_enabled: bool = False
    llm_probability: int = 100
    llm_max_chars: int = 40
    llm_timeout: float = 10.0
    llm_per_minute: int = 5
    llm_system_prompt: str = ""
    llm_extra_prompt: str = ""

    # ---- 回戳 ----
    poke_back_enabled: bool = True
    poke_back_probability: int = 100
    poke_back_times: int = 1
    poke_back_interval_ms: int = 300
    prefer_native: bool = True
    native_fallback: bool = True
    chain_poke: bool = False
    link_mode: str = "发言与回戳独立"

    # ---- 防骚扰 ----
    harass_enabled: bool = True
    harass_threshold: int = 5
    harass_action: str = HARASS_SILENT
    harass_pool: tuple[dict, ...] = ()
    harass_cooldown: float = 60.0
    per_minute_limit: int = 30

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

    # ---- 人形化联动 ----
    humanoid_enabled: bool = False
    humanoid_state_path: str = ""
    humanoid_silent_when_asleep: bool = True

    # ---- 平台 ----
    profiles: dict[str, PlatformProfile] = field(default_factory=dict)
    custom_platforms: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, raw: Any) -> "Settings":
        raw = raw if isinstance(raw, dict) else {}

        base = _section(raw, "基础设置", "base")
        speech = _section(raw, "发言设置", "发言", "speech")
        llm = _section(raw, "LLM 生成", "LLM生成", "llm")
        poke = _section(raw, "回戳设置", "回戳", "poke")
        harass = _section(raw, "防骚扰", "harass")
        cmd = _section(raw, "指令设置", "command")
        lists = _section(raw, "名单设置", "lists")
        stats = _section(raw, "统计设置", "stats")
        humanoid = _section(raw, "人形化联动", "humanoid")
        platforms_cfg = _section(raw, "平台适配", "platforms")
        custom_platforms = raw.get("自定义平台")

        profiles = _build_profiles(platforms_cfg, custom_platforms)

        return cls(
            enabled=as_bool(base.get("总开关"), True),
            platforms=as_str_list(base.get("启用平台")),
            group_enabled=as_bool(base.get("群聊启用"), True),
            private_enabled=as_bool(base.get("私聊启用"), True),
            only_self_target=as_bool(base.get("只响应戳机器人"), True),
            bot_nickname=as_str(base.get("机器人昵称"), "我") or "我",
            bot_aliases=parse_bot_aliases(base.get("机器人别名")),
            user_cooldown=float(as_int(base.get("用户冷却秒数"), 3, 0, 3600)),
            dedupe_window_ms=as_int(base.get("去重窗口毫秒"), 800, 0, 10000),
            quiet_enabled=as_bool(base.get("安静时段开关"), False),
            quiet_start=parse_hhmm(base.get("安静时段开始")),
            quiet_end=parse_hhmm(base.get("安静时段结束")),
            speech_enabled=as_bool(speech.get("发言开关"), True),
            speech_probability=as_int(speech.get("发言概率"), 100, 0, 100),
            speech_mode=as_str(speech.get("选取模式"), "权重随机"),
            speech_at=as_bool(speech.get("附带艾特"), True),
            speech_pool=_normalize_pool(speech.get("发言模板")),
            combo_threshold=as_int(speech.get("连击彩蛋阈值"), 5, 0, 1000),
            combo_pool=_normalize_pool(speech.get("连击彩蛋模板")),
            llm_enabled=as_bool(llm.get("LLM台词开关"), False),
            llm_probability=as_int(llm.get("LLM触发概率"), 100, 0, 100),
            llm_max_chars=as_int(llm.get("LLM最大字数"), 40, 1, 500),
            llm_timeout=float(as_int(llm.get("LLM超时秒数"), 10, 1, 120)),
            llm_per_minute=as_int(llm.get("LLM每分钟上限"), 5, 0, 120),
            llm_system_prompt=as_str(llm.get("LLM系统提示"), ""),
            llm_extra_prompt=as_str(llm.get("LLM附加要求"), ""),
            poke_back_enabled=as_bool(poke.get("回戳开关"), True),
            poke_back_probability=as_int(poke.get("回戳概率"), 100, 0, 100),
            poke_back_times=as_int(poke.get("回戳次数"), 1, 0, 10),
            poke_back_interval_ms=as_int(poke.get("回戳间隔毫秒"), 300, 0, 5000),
            prefer_native=as_bool(poke.get("优先原生回戳"), True),
            native_fallback=as_bool(poke.get("原生失败降级"), True),
            chain_poke=as_bool(poke.get("链式回戳（实验）"), False),
            link_mode=as_str(poke.get("联动模式"), "发言与回戳独立"),
            harass_enabled=as_bool(harass.get("防骚扰开关"), True),
            harass_threshold=as_int(harass.get("每分钟阈值"), 5, 1, 600),
            harass_action=as_str(harass.get("触发动作"), HARASS_SILENT),
            harass_pool=_normalize_pool(harass.get("怼人模板")),
            harass_cooldown=float(as_int(harass.get("怼人冷却秒数"), 60, 0, 3600)),
            per_minute_limit=as_int(harass.get("全局每分钟上限"), 30, 0, 600),
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
            humanoid_enabled=as_bool(humanoid.get("联动开关"), False),
            humanoid_state_path=as_str(humanoid.get("状态文件路径"), ""),
            humanoid_silent_when_asleep=as_bool(humanoid.get("睡着时不回应"), True),
            profiles=profiles,
            custom_platforms=_custom_platform_names(custom_platforms),
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def profile_for(self, platform: str) -> PlatformProfile:
        return self.profiles.get(platform) or DEFAULT_PROFILES.get(platform) or FALLBACK_PROFILE

    def bot_name(self, self_id: Any = "") -> str:
        """当前这个号的显示名：先查别名表，再退回「机器人昵称」。"""
        key = as_str(self_id).strip()
        if key and key in self.bot_aliases:
            return self.bot_aliases[key]
        return self.bot_nickname

    def platform_allowed(self, platform: str) -> bool:
        if not self.platforms:
            return True
        return platform in self.platforms

    def text_trigger_allowed(self, platform: str) -> bool:
        # 自定义平台是使用者主动加的，默认就当它需要触发词入口
        if platform in self.custom_platforms:
            return True
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

    def harass_should_reply(self) -> bool:
        """防骚扰触发时是否要发一条「怼人」文案。"""
        return self.harass_action in (HARASS_SILENT_AND_REPLY, HARASS_REPLY_ONLY)

    def harass_should_silent(self) -> bool:
        """防骚扰触发时是否要静默（不回话、不回戳）。"""
        return self.harass_action in (HARASS_SILENT, HARASS_SILENT_AND_REPLY)


def _build_profiles(platforms_cfg: dict, custom_platforms: Any) -> dict[str, PlatformProfile]:
    """把「平台适配」与「自定义平台」合并进默认画像。"""
    merged: dict[str, PlatformProfile] = dict(DEFAULT_PROFILES)

    def apply(name: str, raw: dict) -> None:
        default = merged.get(name) or _profile(name)
        merged[name] = PlatformProfile(
            name=name,
            native_poke=as_bool(raw.get("原生回戳"), default.native_poke),
            supports_at=as_bool(raw.get("支持艾特"), default.supports_at),
            text_template=as_str(raw.get("拟真文案"), default.text_template),
            emoji=as_str(raw.get("表情前缀"), default.emoji),
        )

    for name, raw in platforms_cfg.items():
        if isinstance(raw, dict):
            apply(name, raw)

    # 自定义平台：允许使用者给任何「还没内置画像」的适配器补一套能力
    if isinstance(custom_platforms, list):
        for item in custom_platforms:
            if not isinstance(item, dict):
                continue
            name = as_str(item.get("平台ID")).strip()
            if name:
                apply(name, item)

    return merged


def _custom_platform_names(custom_platforms: Any) -> tuple[str, ...]:
    if not isinstance(custom_platforms, list):
        return ()
    names: list[str] = []
    for item in custom_platforms:
        if isinstance(item, dict):
            name = as_str(item.get("平台ID")).strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


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
