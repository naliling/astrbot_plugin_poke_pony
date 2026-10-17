"""冒烟测试：在真实 AstrBot 运行时里驱动插件，验证关键链路。

用法（Windows，AstrBot 桌面版自带 Python）::

    $py = "C:\\Users\\<你>\\AppData\\Local\\AstrBot\\backend\\python\\python.exe"
    & $py tests\\smoke_test.py

也可以通过环境变量 ``ASTRBOT_APP`` 指定 AstrBot 的 ``app`` 目录。

覆盖场景：
1. 配置 schema 默认值能被正确解析；
2. QQ 原生戳一戳（Poke 消息段）→ 发言 + group_poke；
3. 原生回戳失败 → 自动降级为拟真文案，不抛异常；
4. 通知事件（notice.poke）双通道识别 + 去重；
5. Telegram 触发词 → 发言 + 拟真回戳；
6. ``/戳 @某人 3`` 指令；
7. 用户冷却生效。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

ASTRBOT_APP = os.environ.get(
    "ASTRBOT_APP",
    r"C:\Users\36328\AppData\Local\AstrBot\backend\app",
)
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ASTRBOT_APP)
sys.path.insert(0, PLUGIN_DIR)

from astrbot.core.message.components import At, Plain, Poke  # noqa: E402
from astrbot.api.platform import MessageMember  # noqa: E402
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


class StubContext:
    def get_config(self):
        return {}

    async def send_message(self, *args, **kwargs):  # pragma: no cover
        return None


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
    event = AiocqhttpMessageEvent(
        message_str=text,
        message_obj=message,
        platform_meta=meta,
        session_id=message.session_id,
        bot=bot or FakeBot(),
    )
    return event


def capture_sends(event) -> list[list]:
    """拦截 event.send，记录发出去的消息链。"""
    sent: list[list] = []

    async def _send(chain):
        sent.append(list(getattr(chain, "chain", []) or []))

    event.send = _send  # type: ignore[method-assign]
    return sent


def make_plugin(config: dict | None = None) -> plugin_main.SuperPokePlugin:
    return plugin_main.SuperPokePlugin(context=StubContext(), config=config or {})


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
    check("发言模板解析出 6 条", len(settings.speech_pool) == 6, str(len(settings.speech_pool)))
    check("彩蛋模板解析出 3 条", len(settings.combo_pool) == 3, str(len(settings.combo_pool)))
    check("默认开启回戳", settings.poke_back_enabled is True)
    check("默认连击阈值 5", settings.combo_threshold == 5, str(settings.combo_threshold))
    check("默认冷却 3 秒", settings.user_cooldown == 3.0, str(settings.user_cooldown))
    check(
        "aiocqhttp 画像支持原生戳",
        settings.profile_for("aiocqhttp").native_poke is True,
    )
    check(
        "telegram 画像为拟真模式",
        settings.profile_for("telegram").native_poke is False,
    )
    check(
        "未知平台有兜底画像",
        settings.profile_for("some_unknown_adapter").text_template != "",
    )
    empty = Settings.from_config({})
    check(
        "空配置也能构造 Settings 并带兜底发言",
        empty.enabled is True and len(empty.speech_pool) >= 1,
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
        check("降级文案含「戳」或「拍」", ("戳" in texts) or ("拍" in texts), texts)


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
    plugin = make_plugin({"基础设置": {"用户冷却秒数": 60}})
    event_a = build_event(components=[Poke(id="99999")])
    first = await run_handler(plugin, event_a)
    event_b = build_event(components=[Poke(id="99999")])
    second = await run_handler(plugin, event_b)
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


async def main() -> int:
    print("=" * 68)
    print("新·戳一戳 冒烟测试（真实 AstrBot 运行时）")
    print("=" * 68)
    await case_schema_defaults()
    await case_native_poke()
    await case_native_fallback()
    await case_dedupe()
    await case_telegram()
    await case_command()
    await case_cooldown()
    await case_private()
    print("\n" + "=" * 68)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    print("=" * 68)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
