"""券商适配器包 — 小粒度 Protocol, 上层只用统一领域状态.

- base.py: BrokerAdapter Protocol (无实现)
- mock.py: 内存测试适配器 (可配置延迟/部分成交/重复回调/回调先于响应)
- xtquant.py: (未来) QMT/XtQuant 适配器, 本版不实现
"""
