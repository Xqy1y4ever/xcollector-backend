"""Xcollector backend —— 纯数据层。

只做增删查改与读投影，不做任何业务判断（见 docs/api.md）：
"什么是通知""该不该抽""DDL 对不对"全部由 xcollector-bot 决定，
后端只负责把它写下来、再读回去。
"""

from __future__ import annotations

__version__ = "0.2.0"
