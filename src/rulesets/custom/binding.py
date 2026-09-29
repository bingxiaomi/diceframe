"""规则绑定与版本常量。

``GameInstance.bind_ruleset_runtime`` 要求四个字段全部非空，缺一个就拒绝绑定：
``runtime_id`` / ``runtime_version`` / ``content_version`` / ``state_schema_version``。

绑定一旦写入就不能改成别的 runtime —— 这是"存档绑定了哪套规则"的身份。改动
``STATE_SCHEMA_VERSION`` 时必须同时提供 ``migrate_state`` 的迁移分支。
"""

from __future__ import annotations

from typing import Any

RUNTIME_ID = "custom:declarative"
RUNTIME_VERSION = 1

# 规则内容版本：改了 ``custom_mechanics`` 的语义（不是改数值）时递增，
# 让旧存档能被识别为"规则版本不一致"。
CONTENT_VERSION = "custom-1"

# ``ruleset_state`` 的 schema 版本。递增时必须在 runtime.migrate_state 里加分支。
STATE_SCHEMA_VERSION = 1

# event_ledger 只做诊断与回放，保留有界长度避免存档无限增长。
MAX_EVENT_LEDGER = 64


def rule_binding() -> dict[str, Any]:
    """本运行时的规则绑定。角色卡提交时需带在 ``rule_binding`` 字段里。"""

    return {
        "runtime_id": RUNTIME_ID,
        "runtime_version": RUNTIME_VERSION,
        "content_version": CONTENT_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
    }


def binding_matches(binding: Any) -> bool:
    """判断一个 ``rule_binding`` 是否属于当前版本的本运行时。"""

    if not isinstance(binding, dict):
        return False
    return (
        str(binding.get("runtime_id") or "") == RUNTIME_ID
        and int(binding.get("runtime_version", 0) or 0) == RUNTIME_VERSION
        and str(binding.get("content_version") or "") == CONTENT_VERSION
        and int(binding.get("state_schema_version", 0) or 0) == STATE_SCHEMA_VERSION
    )


__all__ = [
    "CONTENT_VERSION",
    "MAX_EVENT_LEDGER",
    "RUNTIME_ID",
    "RUNTIME_VERSION",
    "STATE_SCHEMA_VERSION",
    "binding_matches",
    "rule_binding",
]
