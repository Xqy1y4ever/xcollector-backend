"""配置边界自检。

    python -m tests.check_config

「配置文件解耦」不等于「拆成两个 .env」。可检查的标准有四条：

  1. 每个进程只声明自己真正用得到的配置 —— 没有「定义了但没人读」的字段
  2. .env.example 里的每一项都能对应到一个 Settings 字段（没有写错的键名）
  3. 后端不出现只属于 bot 的配置（ONEBOT_*、合并转发展开深度），反之亦然
  4. 跨进程共享的密钥在两边**同名**

第 4 条最容易出错，也最烦人：同一个值如果一边叫 A 一边叫 B，
配置的人得靠猜才知道它们必须一致。

bot 仓库不在旁边时，第 3、4 条会跳过（只校验本仓库）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
BOT = BACKEND.parent / "xcollector-bot"

# 只属于 bot 的配置：后端不该出现
BOT_ONLY = {
    "ONEBOT_MODE", "ONEBOT_WS_URL", "ONEBOT_ACCESS_TOKEN",
    "ONEBOT_LISTEN_HOST", "ONEBOT_LISTEN_PORT", "ONEBOT_LISTEN_PATH",
    "FORWARD_MAX_DEPTH", "COMMAND_PREFIX", "COMMAND_WHITELIST",
    "PENDING_TTL_SECONDS", "BOT_LISTEN_HOST", "BOT_LISTEN_PORT",
}
# 只属于后端的配置：bot 不该出现
BACKEND_ONLY = {
    "GROUP_WHITELIST", "SENDER_WHITELIST", "SENDER_WHITELIST_MODE",
    "EXTRACTOR", "LLM_PRIMARY_MODEL", "LLM_SECONDARY_MODEL",
    "DB_PATH", "ATTACHMENT_DIR", "DIGEST_TARGET_QQ", "DIGEST_TIME",
}
# 两边都必须同名（同一个共享密钥）
SHARED_MUST_MATCH = {"INGEST_API_TOKEN", "BOT_API_TOKEN"}

KEY_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=", re.M)
DECL_RE = re.compile(r"^\s*([a-z][a-z0-9_]*)\s*:\s*[^=\n]+=", re.M)


def settings_fields(config_py: Path) -> list[str]:
    body = config_py.read_text(encoding="utf-8").split("class Settings(BaseSettings):", 1)[1]
    body = body.split("\n    # ---------------- 派生属性", 1)[0]
    return DECL_RE.findall(body)


def env_keys(env_example: Path) -> list[str]:
    if not env_example.exists():
        return []
    return KEY_RE.findall(env_example.read_text(encoding="utf-8"))


def unused_fields(root: Path, fields: list[str]) -> list[str]:
    """在**整个仓库**里找引用（含 config.py 自身的 self.x）。

    只匹配到「声明那一行」的字段就是没人读的。
    """
    sources = list((root / "app").rglob("*.py")) + list((root / "tests").rglob("*.py"))
    unused = []
    for field in fields:
        pat = re.compile(rf"\b{re.escape(field)}\b")
        hits = 0
        for p in sources:
            for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not pat.search(ln):
                    continue
                if DECL_RE.match(ln) and ln.strip().startswith(field):  # 声明行本身
                    continue
                hits += 1
        if hits == 0:
            unused.append(field)
    return unused


def main() -> int:
    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(("ok    " if ok else "FAIL  ") + name + (f"  {detail}" if detail else ""))
        if not ok:
            failures += 1

    for name, root in [("backend", BACKEND), ("bot", BOT)]:
        if not root.exists():
            print(f"跳过 {name}（目录不存在：{root}）")
            continue
        print(f"\n=== {name} ===")
        fields = settings_fields(root / "app" / "config.py")
        keys = env_keys(root / ".env.example")

        dead = unused_fields(root, fields)
        check(f"{len(fields)} 个 Settings 字段都有代码在读", not dead, f"没人读：{dead}")

        unknown = [k for k in keys if k.lower() not in fields]
        check(f"{len(keys)} 个 .env.example 键都能对应到 Settings", not unknown, f"对不上：{unknown}")

    if BOT.exists():
        print("\n=== 跨进程边界 ===")
        b_keys = set(env_keys(BACKEND / ".env.example"))
        t_keys = set(env_keys(BOT / ".env.example"))

        leaked_in = sorted(b_keys & BOT_ONLY)
        check("后端没有出现只属于 bot 的配置", not leaked_in, f"泄漏：{leaked_in}")

        leaked_out = sorted(t_keys & BACKEND_ONLY)
        check("bot 没有出现只属于后端的配置", not leaked_out, f"泄漏：{leaked_out}")

        missing = sorted(k for k in SHARED_MUST_MATCH if k not in (b_keys & t_keys))
        check("共享密钥两边同名", not missing, f"缺同名项：{missing}")

        shared = sorted((b_keys & t_keys) - {"LOG_LEVEL", "LOG_PREVIEW_CHARS"})
        print(f"      两边同名的键：{shared}")
    else:
        print(f"\n（未找到 bot 仓库 {BOT}，跳过跨进程校验）")

    print()
    if failures:
        print(f"❌ {failures} 项失败")
        return 1
    print("✅ 配置边界清晰")
    return 0


if __name__ == "__main__":
    sys.exit(main())
