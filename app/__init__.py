"""Xcollector backend —— 把 QQ 官方通知抽取为带 DDL 的任务条目。

这个文件在 `app` 包被第一次导入时执行，**早于任何 `import litellm`**，
所以它也是关闭 litellm 联网行为唯一可靠的位置。
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"

_BASE_DIR = Path(__file__).resolve().parent.parent

# 先把 .env 灌进进程环境，下面几行的 setdefault 才不会覆盖掉用户的自定义配置。
# （pydantic-settings 只把 .env 读进 Settings 对象，不写 os.environ，
#   而 litellm 只认 os.environ，所以这一步是必需的。）
try:
    from dotenv import load_dotenv

    load_dotenv(_BASE_DIR / ".env", override=False)
except Exception:  # dotenv 缺失不应阻止服务启动
    pass


# ---------------------------------------------------------------------------
# litellm 的联网行为
#
# litellm 在 **导入时** 会去 raw.githubusercontent.com 拉模型价格表。
# 网络不通时它会阻塞导入、再起后台线程重试 3 次，并刷一串 WARNING。
# 本机实测：导入耗时 87.2s → 设上开关后 2.8s。
#
# 我们只用 `usage.total_tokens`，完全不需要这张表，所以强制它用包里自带的副本。
# 这两项都只是 setdefault，用户仍可在真实环境变量或 .env 里覆盖。
# ---------------------------------------------------------------------------
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOCAL_BLOG_POSTS", "True")
