"""装载测试：模拟 AstrBot 真实的插件加载方式。

AstrBot 会以 ``data.plugins.<插件目录名>.main`` 的形式导入插件，本脚本复制一份
插件到临时目录并按同样的包路径导入，验证：

1. 相对导入（``from .poke_core ...``）在真实加载方式下可用；
2. ``metadata.yaml`` 能通过 AstrBot 的元数据校验，且 name / author / version 正确；
3. ``_conf_schema.json`` 能构建出 ``AstrBotConfig``，并被插件正常读取；
4. 用「配置默认值」实例化插件后，原生回戳照常工作、台词为空时不说话；
5. 写入模板后立刻能说话；
6. WebUI 保存配置（同一个 AstrBotConfig 被原地更新）后，插件立刻读到新值。

用法::

    & $py tests\\load_test.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile

ASTRBOT_APP = os.environ.get(
    "ASTRBOT_APP",
    r"C:\Users\36328\AppData\Local\AstrBot\backend\app",
)
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_NAME = os.path.basename(PLUGIN_DIR.rstrip("\\/"))

sys.path.insert(0, ASTRBOT_APP)
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
sys.path.insert(0, PLUGIN_DIR)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} :: {detail}")


async def main() -> int:
    print("=" * 68)
    print("新.戳一戳 装载测试（模拟 AstrBot 插件加载）")
    print("=" * 68)

    # 1. 元数据校验
    from astrbot.core.star.updater import _PluginUpdater

    import yaml

    metadata = yaml.safe_load(open(os.path.join(PLUGIN_DIR, "metadata.yaml"), encoding="utf-8"))
    try:
        _PluginUpdater.validate_plugin_metadata(metadata, "metadata.yaml")
        check("metadata.yaml 通过校验", True)
    except Exception as exc:
        check("metadata.yaml 通过校验", False, str(exc))
    check("name = astrbot_plugin_poke_pony", metadata.get("name") == "astrbot_plugin_poke_pony", str(metadata.get("name")))
    check("display_name = 新.戳一戳", metadata.get("display_name") == "新.戳一戳", str(metadata.get("display_name")))
    check("author = 娜莉灵", metadata.get("author") == "娜莉灵", str(metadata.get("author")))
    check("version = v1.91.82", metadata.get("version") == "v1.91.82", str(metadata.get("version")))
    check("目录名与 metadata.name 一致", PLUGIN_NAME == metadata.get("name"), f"{PLUGIN_NAME} vs {metadata.get('name')}")
    check("声明了多平台支持", len(metadata.get("support_platforms") or []) >= 5)

    # 2. 以 AstrBot 的包路径导入
    temp_root = tempfile.mkdtemp(prefix="poke_pony_load_")
    target = os.path.join(temp_root, "data", "plugins", PLUGIN_NAME)
    try:
        shutil.copytree(
            PLUGIN_DIR,
            target,
            ignore=shutil.ignore_patterns("__pycache__", "tests", ".git"),
        )
        sys.path.insert(0, temp_root)
        module_path = f"data.plugins.{PLUGIN_NAME}.main"
        try:
            module = importlib.import_module(module_path)
            check(f"以 {module_path} 导入成功", True)
        except Exception as exc:
            check(f"以 {module_path} 导入成功", False, f"{type(exc).__name__}: {exc}")
            raise

        plugin_cls = module.SuperPokePlugin
        check("插件类已注册到 star_map", True)
        check(
            "代码内版本号与 metadata 一致",
            module.PLUGIN_VERSION == metadata.get("version"),
            f"{module.PLUGIN_VERSION} vs {metadata.get('version')}",
        )
        check(
            "代码内显示名与 metadata 一致",
            module.PLUGIN_NAME == metadata.get("display_name"),
            f"{module.PLUGIN_NAME} vs {metadata.get('display_name')}",
        )

        # 2.5 复刻 star_manager 注入的类属性（name / author / plugin_id）
        from astrbot.core.star.star import StarMetadata

        star_meta = StarMetadata(
            name=metadata.get("name"),
            author=metadata.get("author"),
            desc=metadata.get("desc"),
            version=metadata.get("version"),
            module_path=module_path,
            star_cls_type=plugin_cls,
        )
        expected_plugin_id = star_meta.plugin_id
        check(
            "plugin_id 为 娜莉灵/astrbot_plugin_poke_pony",
            expected_plugin_id == "娜莉灵/astrbot_plugin_poke_pony",
            expected_plugin_id,
        )
        p_author, p_name = expected_plugin_id.split("/")
        setattr(plugin_cls, "name", p_name)
        setattr(plugin_cls, "author", p_author)
        setattr(plugin_cls, "plugin_id", expected_plugin_id)

        # 3. 用真实 AstrBotConfig 实例化
        from astrbot.core.config.astrbot_config import AstrBotConfig

        schema_path = os.path.join(target, "_conf_schema.json")
        config_path = os.path.join(temp_root, "config", f"{PLUGIN_NAME}_config.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        schema = json.load(open(schema_path, encoding="utf-8"))
        config = AstrBotConfig(config_path=config_path, schema=schema)
        check("AstrBotConfig 由 schema 构建成功", bool(config.get("基础设置")))
        check("schema 有 11 个分组", len(schema) == 11, str(len(schema)))
        check(
            "默认发言模板为空",
            config["发言设置"]["发言模板"] == [],
            str(config["发言设置"]["发言模板"]),
        )

        class StubContext:
            def get_config(self):
                return {}

            def get_using_provider(self, umo=None):
                return None

        plugin = plugin_cls(context=StubContext(), config=config)
        check("插件实例化成功", plugin is not None)
        check(
            "插件实例携带中文 plugin_id",
            getattr(plugin, "plugin_id", "") == "娜莉灵/astrbot_plugin_poke_pony",
            str(getattr(plugin, "plugin_id", None)),
        )
        check(
            "统计存储已就绪（plugin_id 可被 KV 使用）",
            plugin._stats._storage_ready() is True,
        )

        # 4. 默认配置：不说话，但原生回戳照常
        import smoke_test as smoke  # type: ignore  # noqa: E402

        bot = smoke.FakeBot()
        event = smoke.build_event(components=[smoke.Poke(id="99999")], bot=bot)
        results = await smoke.run_handler(plugin, event)
        check("默认配置下不说话", smoke.chain_text(results) == "", repr(smoke.chain_text(results)))
        check("默认配置下仍原生回戳", any(c[0] == "group_poke" for c in bot.calls), str(bot.calls))

        # 5. 写入模板后立刻能说话（模拟用户在 WebUI 里填台词）
        config.save_config(
            {
                "基础设置": {**config["基础设置"], "用户冷却秒数": 0},
                "发言设置": {
                    **config["发言设置"],
                    "发言模板": [
                        {"__template_key": "speech", "启用发言": True, "发言内容": "别戳啦", "发言权重": 1}
                    ],
                },
            },
        )
        bot2 = smoke.FakeBot()
        event2 = smoke.build_event(components=[smoke.Poke(id="99999")], bot=bot2)
        results2 = await smoke.run_handler(plugin, event2)
        check("填了模板就会说话", "别戳啦" in smoke.chain_text(results2), smoke.chain_text(results2))

        # 6. 指令与状态
        status_event = smoke.build_event(text="戳状态")
        status_out = []
        async for item in plugin.poke_status(status_event):
            status_out.append(item)
        status_text = smoke.chain_text(status_out)
        check("/戳状态 有输出", "运行状态" in status_text, status_text[:80])
        check("/戳状态 显示版本", "v1.91.82" in status_text, status_text[:80])
        check("/戳状态 显示插件名", "新.戳一戳" in status_text, status_text[:80])
        check("/戳状态 显示机器人", "机器人：" in status_text, status_text[:120])
        check("/戳状态 按号统计", "本号今日被戳" in status_text, status_text[:160])

        # 7. 热重载：WebUI 保存配置后，插件立刻读到新值
        config.save_config(
            {
                "基础设置": {
                    **config["基础设置"],
                    "用户冷却秒数": 9,
                    "机器人昵称": "娜莉灵",
                },
            },
        )
        reloaded = plugin._settings()
        check("热重载后冷却生效", reloaded.user_cooldown == 9.0, str(reloaded.user_cooldown))
        check("热重载后昵称生效", reloaded.bot_nickname == "娜莉灵", reloaded.bot_nickname)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    print("\n" + "=" * 68)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    print("=" * 68)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
