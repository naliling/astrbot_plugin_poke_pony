# 原版存档 · astrbot_plugin_poke v1.0.0

这里保留的是**原版「戳一戳」插件**的完整源码，作者 **TenmaGabriel0721**，
最初由朋友赠予，是本项目的起点。

保留它的原因有两个：

1. **纪念**——那个只会回一句「呜」的小插件，是很多人接触 AstrBot 的第一课；
2. **对照**——想看清 v1.91.78 到底改了什么，可以直接 diff：

```bash
diff -u original/main.py main.py
```

## 原版为什么不能用了

原版 `main.py` 只有 147 行，核心逻辑是：

```python
raw_event = getattr(event.message_obj, "raw_message", None)
if not isinstance(raw_event, dict):
    return
if not (raw_event.get("notice_type") == "notify" and raw_event.get("sub_type") == "poke"):
    return
```

它假设「戳一戳」永远以 OneBot **通知事件**（`post_type=notice`）的形式投递。
但 AstrBot 4.2x 之后，aiocqhttp 适配器会把戳一戳转换成 **`Poke` 消息段**放进消息链，
协议端（NapCat / LLOneBot 等）也可能把它当成普通消息推送。此时 `raw_message` 里
根本没有 `notice_type` 字段，插件于是**完全失聪**——这就是「戳了没反应」的根因。

回戳部分则是直接裸调协议端接口：

```python
await bot.api.call_action("group_poke", group_id=..., user_id=...)
```

一旦协议端没实现该 action、或参数类型不对，异常就会冒泡到 AstrBot 的插件异常处理，
最终以「在调用插件 xxx 的处理函数时出现异常」的形式发到聊天窗口里——
这就是「双击头像戳一戳会报错」的根因。

新版本用 **双通道识别 + 三级回戳降级** 把这两个问题一起解决了，详见根目录
[README](../README.md) 与 [CHANGELOG](../CHANGELOG.md)。

## 授权

原版代码版权归原作者 TenmaGabriel0721 所有，此处仅作存档与对照之用，
未做任何修改。如需商用或二次分发，请先联系原作者。
