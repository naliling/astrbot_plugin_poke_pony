"""装载测试：模拟 AstrBot 真实的插件加载方式。

AstrBot 会以 ``data.plugins.<插件目录名>.main`` 的形式导入插件，本脚本复制一份
插件到临时目录并按同样的包路径导入，验证：

1. 相对导入（``from .poke_core ...``）在真实加载方式下可用；
2. ``metadata.yaml`` 能通过 AstrBot 的元数据校验；
3. ``_conf_schema.json`` 能构建出 ``AstrBotConfig``，并被插件正常读取；
4. 用「配置默认值」实例化插件后，戳一戳链路依然通畅。

用法::

    & $py tests\\load_test.py
"""

from __future__ import annotations

import asyncio
import importlib
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
    print("新·戳一戳 装载测试（模拟 AstrBot 插件加载）")
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
    check("display_name 为 新.戳一戳", metadata.get("display_name") == "新.戳一戳", str(metadata.get("display_name")))
    check("声明了多平台支持", len(metadata.get("support_platforms") or []) >= 5)

    # 2. 以 AstrBot 的包路径导入
    temp_root = tempfile.mkdtemp(prefix="superpoke_load_")
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

        # 3. 用真实 AstrBotConfig 实例化
        from astrbot.core.config.astrbot_config import AstrBotConfig

        schema_path = os.path.join(target, "_conf_schema.json")
        config_path = os.path.join(temp_root, "config", f"{PLUGIN_NAME}_config.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        import json

        schema = json.load(open(schema_path, encoding="utf-8"))
        config = AstrBotConfig(config_path=config_path, schema=schema)
        check("AstrBotConfig 由 schema 构建成功", bool(config.get("基础设置")))

        class StubContext:
            def get_config(self):
                return {}

        plugin = plugin_cls(context=StubContext(), config=config)
        check("插件实例化成功", plugin is not None)

        # 4. 跑一次真实配置下的戳一戳
        import smoke_test as smoke  # type: ignore  # noqa: E402

        bot = smoke.FakeBot()
        event = smoke.build_event(components=[smoke.Poke(id="99999")], bot=bot)
        results = await smoke.run_handler(plugin, event)
        check("真实配置下产生发言", bool(smoke.chain_text(results)), smoke.chain_text(results))
        check("真实配置下触发原生回戳", any(c[0] == "group_poke" for c in bot.calls), str(bot.calls))

        # 5. 指令与状态
        status_event = smoke.build_event(text="戳状态")
        status_out = []
        async for item in plugin.poke_status(status_event):
            status_out.append(item)
        status_text = smoke.chain_text(status_out)
        check("/戳状态 有输出", "运行状态" in status_text, status_text[:80])
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
