"""冒烟测试：在真实 AstrBot 运行时里驱动插件，验证关键链路。

用法（Windows，AstrBot 桌面版自带 Python）::

    $py = "C:\\Users\\<你>\\AppData\\Local\\AstrBot\\backend\\python\\python.exe"
    & $py tests\\smoke_test.py

也可以通过环境变量 ``ASTRBOT_APP`` 指定 AstrBot 的 ``app`` 目录。

覆盖场景：
1. 配置 schema 默认值能被正确解析（且台词类默认是空的）；
2. QQ 原生戳一戳（Poke 消息段）→ 发言 + group_poke；
3. 原生回戳失败 → 自动降级为拟真文案，不抛异常；
4. 通知事件（notice.poke）双通道识别 + 去重；
5. Telegram 触发词 → 发言 + 拟真回戳；
6. ``/戳 @某人 3`` 指令；
7. 用户冷却；
8. 私聊 → friend_poke；
9. 防骚扰：超过每分钟阈值后静默；
10. 防骚扰：可选的「怼人」文案；
11. LLM 台词：provider 不可用时回退模板；
12. LLM 台词：provider 正常时使用生成结果；
13. 人形化联动：契约里 asleep=true 时静默；
14. 自定义平台：给未知适配器补画像后能拟真回戳；
15. 台词为空时：不发言，但原生回戳照常。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

ASTRBOT_APP = os.environ.get(
    "ASTRBOT_APP",
    r"C:\Users\36328\AppData\Local\AstrBot\backend\app",
)
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ASTRBOT_APP)
sys.path.insert(0, PLUGIN_DIR)

from astrbot.api.platform import MessageMember  # noqa: E402
from astrbot.core.message.components import At, Plain, Poke  # noqa: E402
from astrbot.core.platform.astrbot_message import AstrBotMessage  # noqa: E402
from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.platform.platform_metadata import PlatformMetadata  # noqa: E402
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (  # noqa: E402
    AiocqhttpMessageEvent,
)

import main as plugin_main  # noqa: E402
from poke_core.settings import Settings  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} :: {detail}")


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        if self.fail:
            raise RuntimeError("ActionFailed: 协议端不支持该接口")
        return {"status": "ok"}


class FakeProvider:
    """假的 LLM Provider，记录调用并返回固定文本。"""

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def text_chat(self, prompt: str | None = None, system_prompt: str | None = None, **kwargs):
        self.calls.append((prompt or "", system_prompt or ""))
        if self.error:
            raise self.error
        return SimpleNamespace(completion_text=self.text)


class StubContext:
    def __init__(self, provider=None) -> None:
        self.provider = provider

    def get_config(self):
        return {}

    def get_using_provider(self, umo: str | None = None):
        return self.provider


def speech_item(text: str, weight: int = 1) -> dict:
    return {
        "__template_key": "speech",
        "启用发言": True,
        "发言内容": text,
        "发言权重": weight,
    }


def harass_item(text: str) -> dict:
    return {
        "__template_key": "harass",
        "启用发言": True,
        "发言内容": text,
        "发言权重": 1,
    }


def base_config(**overrides) -> dict:
    """一份「有台词、无冷却」的测试配置，避免默认空模板影响断言。"""
    config = {
        "基础设置": {"用户冷却秒数": 0, "每分钟上限": 100},
        "发言设置": {"发言模板": [speech_item("呜")]},
        "回戳设置": {"回戳次数": 1},
        "防骚扰": {"防骚扰开关": False},
        "平台适配": {
            "aiocqhttp": {"拟真文案": "（{bot} 戳了戳 {target}）"},
            "telegram": {"拟真文案": "（{bot} 戳了戳 {target}）"},
        },
    }
    for section, values in overrides.items():
        if isinstance(values, dict):
            config.setdefault(section, {}).update(values)
        else:
            config[section] = values
    return config


def build_event(
    *,
    text: str = "",
    components: list | None = None,
    platform: str = "aiocqhttp",
    group_id: str = "686724942",
    sender_id: str = "10001",
    self_id: str = "99999",
    private: bool = False,
    raw: dict | None = None,
    bot: FakeBot | None = None,
) -> AiocqhttpMessageEvent:
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    message.self_id = self_id
    message.session_id = sender_id if private else group_id
    message.message_id = "msg-1"
    message.group_id = "" if private else group_id
    message.sender = MessageMember(user_id=sender_id, nickname="小测试")
    message.message_str = text
    message.message = components or []
    message.raw_message = raw or {}
    meta = PlatformMetadata(name=platform, description=platform, id=platform)
    return AiocqhttpMessageEvent(
        message_str=text,
        message_obj=message,
        platform_meta=meta,
        session_id=message.session_id,
        bot=bot or FakeBot(),
    )


def capture_sends(event) -> list[list]:
    """拦截 event.send，记录发出去的消息链。"""
    sent: list[list] = []

    async def _send(chain):
        sent.append(list(getattr(chain, "chain", []) or []))

    event.send = _send  # type: ignore[method-assign]
    return sent


def make_plugin(config: dict | None = None, provider=None) -> plugin_main.SuperPokePlugin:
    return plugin_main.SuperPokePlugin(
        context=StubContext(provider=provider),
        config=config if config is not None else base_config(),
    )


async def run_handler(plugin, event) -> list:
    results = []
    async for result in plugin.on_poke_event(event):
        results.append(result)
    return results


def chain_text(results: list) -> str:
    out = []
    for result in results:
        for comp in getattr(result, "chain", []) or []:
            text = getattr(comp, "text", None)
            if text:
                out.append(str(text))
    return "".join(out)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


async def case_schema_defaults() -> None:
    print("\n[1] 配置 schema 默认值解析")
    schema_path = os.path.join(PLUGIN_DIR, "_conf_schema.json")
    schema = json.load(open(schema_path, encoding="utf-8"))

    from astrbot.core.config.default import DEFAULT_VALUE_MAP

    def build(node: dict) -> dict:
        out = {}
        for key, value in node.items():
            if "default" in value:
                out[key] = value["default"]
            elif value["type"] == "object":
                out[key] = build(value["items"])
            else:
                out[key] = DEFAULT_VALUE_MAP[value["type"]]
        return out

    config = build(schema)
    settings = Settings.from_config(config)

    check("schema 共 11 个分组", len(schema) == 11, str(len(schema)))
    check("发言模板默认为空", settings.speech_pool == (), str(len(settings.speech_pool)))
    check("彩蛋模板默认为空", settings.combo_pool == (), str(len(settings.combo_pool)))
    check("怼人模板默认为空", settings.harass_pool == (), str(len(settings.harass_pool)))
    check("默认不启用 LLM", settings.llm_enabled is False)
    check("默认启用防骚扰", settings.harass_enabled is True)
    check("默认防骚扰阈值 5", settings.harass_threshold == 5, str(settings.harass_threshold))
    check("默认触发动作是静默", settings.harass_action == "静默不回应", settings.harass_action)
    check("默认不联动人形化", settings.humanoid_enabled is False)
    check(
        "aiocqhttp 支持原生戳",
        settings.profile_for("aiocqhttp").native_poke is True,
    )
    check(
        "默认拟真文案为空",
        settings.profile_for("telegram").text_template == "",
        settings.profile_for("telegram").text_template,
    )
    check(
        "未知平台有兜底画像",
        settings.profile_for("some_unknown_adapter").native_poke is False,
    )
    check(
        "schema 每项都有 hint",
        _all_items_have_hint(schema),
        "有配置项缺 hint",
    )

    empty = Settings.from_config({})
    check(
        "空配置也能构造 Settings 且无兜底台词",
        empty.enabled is True and empty.speech_pool == (),
        str(len(empty.speech_pool)),
    )
    check(
        "安静时段跨零点判断",
        Settings.from_config(
            {
                "基础设置": {
                    "安静时段开关": True,
                    "安静时段开始": "23:00",
                    "安静时段结束": "07:00",
                }
            }
        ).in_quiet_hours(2 * 60)
        is True,
    )


def _all_items_have_hint(node: dict) -> bool:
    """叶子项必须有 hint，分组（object）必须有 description。"""
    for key, value in node.items():
        if not isinstance(value, dict):
            return False
        kind = value.get("type")
        if kind == "object":
            if not str(value.get("description") or "").strip():
                return False
            if not _all_items_have_hint(value.get("items") or {}):
                return False
            continue
        if not str(value.get("hint") or "").strip():
            return False
        if kind == "template_list":
            for tmpl in (value.get("templates") or {}).values():
                if not _all_items_have_hint(tmpl.get("items") or {}):
                    return False
    return True


async def case_native_poke() -> None:
    print("\n[2] QQ 原生戳一戳（Poke 消息段）")
    bot = FakeBot()
    plugin = make_plugin()
    event = build_event(
        components=[Poke(id="99999")],
        raw={"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
             "target_id": 99999, "user_id": 10001, "self_id": 99999,
             "group_id": 686724942},
        bot=bot,
    )
    results = await run_handler(plugin, event)
    text = chain_text(results)
    check("产生了发言", bool(text), repr(text))
    check("发言里带艾特", any(isinstance(c, At) for r in results for c in (r.chain or [])), text)
    check(
        "调用了 group_poke",
        any(call[0] == "group_poke" for call in bot.calls),
        str(bot.calls),
    )
    poke_call = next((c for c in bot.calls if c[0] == "group_poke"), None)
    check(
        "group_poke 参数为 int",
        bool(poke_call)
        and isinstance(poke_call[1].get("group_id"), int)
        and isinstance(poke_call[1].get("user_id"), int),
        str(poke_call),
    )
    check("事件被标记为已处理", event.get_extra(plugin_main.SEEN_FLAG) is True)
    check("事件已终止传播（防止 LLM 二次回复）", event.is_stopped() is True)


async def case_native_fallback() -> None:
    print("\n[3] 原生回戳失败 → 拟真降级")
    bot = FakeBot(fail=True)
    plugin = make_plugin()
    event = build_event(components=[Poke(id="99999")], bot=bot)
    sent = capture_sends(event)
    results = await run_handler(plugin, event)
    check("没有抛异常", True)
    check("调用了原生接口", len(bot.calls) >= 1, str(bot.calls))
    check("降级发出了拟真文案", bool(sent), str(sent))
    if sent:
        texts = "".join(getattr(c, "text", "") for c in sent[0])
        check("降级文案含「戳」", "戳" in texts, texts)


async def case_dedupe() -> None:
    print("\n[4] 双通道去重")
    plugin = make_plugin()
    event = build_event(
        components=[Poke(id="99999")],
        raw={"sub_type": "poke", "target_id": 99999, "user_id": 10001},
    )
    first = await run_handler(plugin, event)
    second = await run_handler(plugin, event)
    check("第一次有响应", bool(first))
    check("第二次被去重", second == [], str(len(second)))


async def case_telegram() -> None:
    print("\n[5] Telegram 触发词")
    plugin = make_plugin()
    event = build_event(text="戳一戳", platform="telegram", components=[Plain("戳一戳")])
    sent = capture_sends(event)
    results = await run_handler(plugin, event)
    check("Telegram 有发言", bool(chain_text(results)), chain_text(results))
    check("Telegram 走拟真回戳", bool(sent), str(sent))
    if sent:
        texts = "".join(getattr(c, "text", "") for c in sent[0])
        check("拟真文案非空", bool(texts.strip()), texts)


async def case_command() -> None:
    print("\n[6] /戳 @某人 3 指令")
    bot = FakeBot()
    plugin = make_plugin()
    event = build_event(
        text="戳 3",
        components=[At(qq="123456"), Plain(" 3")],
        bot=bot,
    )
    results = []
    async for result in plugin.poke_command(event, arg="3"):
        results.append(result)
    text = chain_text(results)
    check("指令有回复", bool(text), text)
    group_calls = [c for c in bot.calls if c[0] == "group_poke"]
    check("连续戳 3 次", len(group_calls) == 3, str(bot.calls))
    if group_calls:
        check(
            "目标为被 At 的人",
            group_calls[0][1].get("user_id") == 123456,
            str(group_calls[0]),
        )


async def case_cooldown() -> None:
    print("\n[7] 用户冷却")
    plugin = make_plugin(base_config(**{"基础设置": {"用户冷却秒数": 60}}))
    first = await run_handler(plugin, build_event(components=[Poke(id="99999")]))
    second = await run_handler(plugin, build_event(components=[Poke(id="99999")]))
    check("首次响应", bool(first))
    check("冷却期内不响应", second == [], str(len(second)))


async def case_private() -> None:
    print("\n[8] 私聊戳一戳 → friend_poke")
    bot = FakeBot()
    plugin = make_plugin()
    event = build_event(components=[Poke(id="99999")], private=True, group_id="", bot=bot)
    await run_handler(plugin, event)
    check(
        "私聊使用 friend_poke",
        any(call[0] == "friend_poke" for call in bot.calls),
        str(bot.calls),
    )


async def case_harass_silent() -> None:
    print("\n[9] 防骚扰：超过阈值静默")
    bot = FakeBot()
    plugin = make_plugin(
        base_config(**{"防骚扰": {"防骚扰开关": True, "每分钟阈值": 5, "触发动作": "静默不回应"}}),
    )
    results = []
    for _ in range(6):
        results.append(await run_handler(plugin, build_event(components=[Poke(id="99999")], bot=bot)))
    check("前 5 次有响应", all(results[i] for i in range(5)), str([len(r) for r in results]))
    check("第 6 次静默", results[5] == [], str(results[5]))
    check("第 6 次也没有回戳", len([c for c in bot.calls if c[0] == "group_poke"]) == 5, str(len(bot.calls)))


async def case_harass_reply() -> None:
    print("\n[10] 防骚扰：可选「怼人」文案")
    plugin = make_plugin(
        base_config(
            **{
                "防骚扰": {
                    "防骚扰开关": True,
                    "每分钟阈值": 3,
                    "触发动作": "静默并怼人",
                    "怼人模板": [harass_item("戳够了吧，{count} 次了")],
                    "怼人冷却秒数": 0,
                }
            }
        ),
    )
    out = []
    for _ in range(4):
        out.append(await run_handler(plugin, build_event(components=[Poke(id="99999")])))
    check("第 4 次触发怼人", bool(out[3]), str(len(out[3])))
    text = chain_text(out[3])
    check("怼人文案渲染了变量", "4 次了" in text, text)


async def case_harass_reply_only() -> None:
    print("\n[17] 防骚扰：只怼人（怼完照常回应）")
    plugin = make_plugin(
        base_config(
            **{
                "防骚扰": {
                    "防骚扰开关": True,
                    "每分钟阈值": 2,
                    "触发动作": "只怼人",
                    "怼人模板": [harass_item("别戳了，{count} 次了")],
                    "怼人冷却秒数": 0,
                }
            }
        ),
    )
    out = []
    for _ in range(3):
        out.append(await run_handler(plugin, build_event(components=[Poke(id="99999")])))
    text = chain_text(out[2])
    check("第 3 次触发怼人", "别戳了" in text and "3 次了" in text, text)
    check("只怼人仍照常发言", "呜" in text, text)


async def case_harass_counts_through_cooldown() -> None:
    print("\n[18] 防骚扰：冷却期内的戳也计数")
    bot = FakeBot()
    plugin = make_plugin(
        base_config(
            **{
                "基础设置": {"用户冷却秒数": 60},
                "防骚扰": {
                    "防骚扰开关": True,
                    "每分钟阈值": 2,
                    "触发动作": "静默并怼人",
                    "怼人模板": [harass_item("第 {count} 次了！")],
                    "怼人冷却秒数": 0,
                },
            }
        ),
    )
    first = await run_handler(plugin, build_event(components=[Poke(id="99999")], bot=bot))
    check("第 1 次正常回应", bool(first), str(len(first)))
    second = await run_handler(plugin, build_event(components=[Poke(id="99999")], bot=bot))
    check("第 2 次被冷却挡住", second == [], str(len(second)))
    third = await run_handler(plugin, build_event(components=[Poke(id="99999")], bot=bot))
    check("第 3 次虽在冷却中仍触发怼人", "第 3 次了" in chain_text(third), chain_text(third))
    check(
        "静默并怼人：怼完不回戳",
        len([c for c in bot.calls if c[0] == "group_poke"]) == 1,
        str(bot.calls),
    )


async def case_group_limit() -> None:
    print("\n[19] 群聊防护：每个群各自每分钟上限")
    plugin = make_plugin(
        base_config(
            **{
                "基础设置": {"用户冷却秒数": 0},
                "防骚扰": {"防骚扰开关": False, "群聊每分钟上限": 1},
            }
        ),
    )
    a = await run_handler(
        plugin,
        build_event(components=[Poke(id="99999")], sender_id="10001", group_id="111"),
    )
    b = await run_handler(
        plugin,
        build_event(components=[Poke(id="99999")], sender_id="10002", group_id="111"),
    )
    c = await run_handler(
        plugin,
        build_event(components=[Poke(id="99999")], sender_id="10003", group_id="222"),
    )
    d = await run_handler(
        plugin,
        build_event(components=[Poke(id="99999")], sender_id="10004", private=True, group_id=""),
    )
    check("同群第 1 次回应", bool(a), str(len(a)))
    check("同群超过上限静默", b == [], str(len(b)))
    check("另一个群不受影响", bool(c), str(len(c)))
    check("私聊不受群上限影响", bool(d), str(len(d)))


async def case_llm_fallback() -> None:
    print("\n[11] LLM 不可用 → 回退模板")
    plugin = make_plugin(
        base_config(**{"LLM 生成": {"LLM台词开关": True, "LLM触发概率": 100}}),
        provider=None,
    )
    results = await run_handler(plugin, build_event(components=[Poke(id="99999")]))
    check("回退到模板发言", chain_text(results).strip() == "呜", repr(chain_text(results)))


async def case_llm_success() -> None:
    print("\n[12] LLM 正常 → 使用生成结果")
    provider = FakeProvider(text="（被戳醒了）干嘛呀")
    plugin = make_plugin(
        base_config(**{"LLM 生成": {"LLM台词开关": True, "LLM触发概率": 100, "LLM最大字数": 40}}),
        provider=provider,
    )
    results = await run_handler(plugin, build_event(components=[Poke(id="99999")]))
    check("调用了 LLM", len(provider.calls) == 1, str(len(provider.calls)))
    check("使用 LLM 文本", chain_text(results).endswith("干嘛呀"), chain_text(results))


async def case_humanoid_asleep() -> None:
    print("\n[13] 人形化联动：睡着时不回应")
    state = {
        "roles": {
            "99999": {
                "self": {
                    "contract": {
                        "v": 1,
                        "feelings": ["很困"],
                        "body": {"asleep": True, "energy_text": "累瘫了"},
                        "activity": {"name": "补觉"},
                        "persona": "娜莉灵",
                        "form": {"max_chars": 12},
                    }
                },
                "users": {"10001": {"mood": {"affection": 62.0}}},
            }
        }
    }
    tmpdir = tempfile.mkdtemp(prefix="poke_pony_state_")
    path = os.path.join(tmpdir, "state.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    try:
        plugin = make_plugin(
            base_config(
                **{
                    "人形化联动": {
                        "联动开关": True,
                        "状态文件路径": path,
                        "睡着时不回应": True,
                    }
                }
            )
        )
        results = await run_handler(plugin, build_event(components=[Poke(id="99999")]))
        check("睡着时静默", results == [], str(len(results)))

        snapshot = plugin._humanoid_snapshot(
            plugin._settings(), build_event(components=[Poke(id="99999")]), "10001"
        )
        check("读到体感", snapshot is not None and snapshot.mood == "很困", str(snapshot))
        check("读到好感度", snapshot is not None and snapshot.affection == "62", str(snapshot))
        check("读到字数上限", snapshot is not None and snapshot.max_chars == 12, str(snapshot))
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


async def case_custom_platform() -> None:
    print("\n[14] 自定义平台")
    plugin = make_plugin(
        base_config(
            **{
                "自定义平台": [
                    {
                        "__template_key": "platform",
                        "平台ID": "whtc",
                        "原生回戳": False,
                        "支持艾特": False,
                        "拟真文案": "（{bot} 拍了 {target} 一下）",
                        "表情前缀": "",
                    }
                ]
            }
        )
    )
    settings = plugin._settings()
    check(
        "自定义平台画像生效",
        settings.profile_for("whtc").text_template == "（{bot} 拍了 {target} 一下）",
        settings.profile_for("whtc").text_template,
    )
    event = build_event(text="戳一戳", platform="whtc", components=[Plain("戳一戳")])
    sent = capture_sends(event)
    await run_handler(plugin, event)
    check("自定义平台能拟真回戳", bool(sent), str(sent))


async def case_empty_templates() -> None:
    print("\n[15] 台词为空：不说话但仍回戳")
    bot = FakeBot()
    plugin = make_plugin(
        {
            "基础设置": {"用户冷却秒数": 0, "每分钟上限": 100},
            "防骚扰": {"防骚扰开关": False},
            "发言设置": {"发言模板": []},
        }
    )
    event = build_event(components=[Poke(id="99999")], bot=bot)
    results = await run_handler(plugin, event)
    check("没有发言", results == [], str(len(results)))
    check("仍然原生回戳", any(c[0] == "group_poke" for c in bot.calls), str(bot.calls))


async def case_multi_bot() -> None:
    print("\n[16] 多机器人：冷却 / 连击 / 统计 / 回声互不干扰")
    config = base_config(
        **{
            "基础设置": {
                "用户冷却秒数": 60,
                "机器人昵称": "我",
                "机器人别名": "99999=娜莉灵,88888=露娜",
            },
            "发言设置": {"发言模板": [speech_item("{bot}：第 {count} 次")]},
        }
    )
    plugin = make_plugin(config)
    check("别名表解析正确", plugin._settings().bot_name("88888") == "露娜")
    check("没配别名的号退回昵称", plugin._settings().bot_name("77777") == "我")

    a1 = await run_handler(plugin, build_event(components=[Poke(id="99999")], self_id="99999"))
    check("A 号首次回应", bool(a1), str(len(a1)))
    check("A 号用别名渲染 {bot}", "娜莉灵" in chain_text(a1), chain_text(a1))
    check("A 号 {count} 从 1 开始", "第 1 次" in chain_text(a1), chain_text(a1))

    a2 = await run_handler(plugin, build_event(components=[Poke(id="99999")], self_id="99999"))
    check("A 号冷却期内静默", a2 == [], str(len(a2)))

    b1 = await run_handler(plugin, build_event(components=[Poke(id="88888")], self_id="88888"))
    check("A 号冷却不挡 B 号", bool(b1), str(len(b1)))
    check("B 号用自己的别名", "露娜" in chain_text(b1), chain_text(b1))
    check("B 号的 {count} 也是第 1 次", "第 1 次" in chain_text(b1), chain_text(b1))

    scoped = plugin._stats.today.bots
    check(
        "统计按号分开计数",
        scoped.get("aiocqhttp:99999") == 1 and scoped.get("aiocqhttp:88888") == 1,
        str(scoped),
    )

    # 排行榜按号独立：同一个号被戳两次、另一个号被戳一次，各自只看到自己的
    rank_plugin = make_plugin(base_config(**{"基础设置": {"用户冷却秒数": 0}}))
    await run_handler(rank_plugin, build_event(components=[Poke(id="99999")], self_id="99999"))
    await run_handler(rank_plugin, build_event(components=[Poke(id="99999")], self_id="99999"))
    await run_handler(rank_plugin, build_event(components=[Poke(id="88888")], self_id="88888"))
    rank_a = rank_plugin._stats.ranking(10, scope="aiocqhttp:99999")
    rank_b = rank_plugin._stats.ranking(10, scope="aiocqhttp:88888")
    check(
        "排行榜按号独立",
        rank_a[0][1] == 2 and rank_b[0][1] == 1,
        f"{rank_a} / {rank_b}",
    )

    # 回声：协议端把「戳 A 号」广播到 B 号，B 号不该跟着再回一遍
    echo_plugin = make_plugin(
        base_config(**{"基础设置": {"用户冷却秒数": 0, "只响应戳机器人": False}}),
    )
    await run_handler(echo_plugin, build_event(components=[Poke(id="99999")], self_id="99999"))
    echo_bot = FakeBot()
    echo = await run_handler(
        echo_plugin,
        build_event(components=[Poke(id="99999")], self_id="88888", bot=echo_bot),
    )
    check("B 号收到「戳 A 号」的回声不回", echo == [], str(len(echo)))
    check("回声也不会触发 B 号回戳", echo_bot.calls == [], str(echo_bot.calls))

    # 同一个号被戳才回
    hit = await run_handler(echo_plugin, build_event(components=[Poke(id="88888")], self_id="88888"))
    check("B 号被戳自己会回", bool(hit), str(len(hit)))


async def main() -> int:
    print("=" * 68)
    print("新.戳一戳 冒烟测试（真实 AstrBot 运行时）")
    print("=" * 68)
    await case_schema_defaults()
    await case_native_poke()
    await case_native_fallback()
    await case_dedupe()
    await case_telegram()
    await case_command()
    await case_cooldown()
    await case_private()
    await case_harass_silent()
    await case_harass_reply()
    await case_harass_reply_only()
    await case_harass_counts_through_cooldown()
    await case_group_limit()
    await case_llm_fallback()
    await case_llm_success()
    await case_humanoid_asleep()
    await case_custom_platform()
    await case_empty_templates()
    await case_multi_bot()
    print("\n" + "=" * 68)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    print("=" * 68)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
