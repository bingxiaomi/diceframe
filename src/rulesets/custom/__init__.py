"""自定义声明式规则运行时（骨架）。

这是给"自己想定规则"准备的第三方 runtime 模板：规则作者在规则模板 JSON 里
用 ``custom_mechanics`` 声明资源、检定、成功度分级与效果；本运行时执行声明，
**不使用** ``check_mechanic`` 那六个硬编码枚举，因此没有机制天花板。

设计边界（与 `docs/ENGINEERING_RULES.md` 一致）：

- 本包不得依赖 ``webui`` / ``compat``；
- 规则机制的唯一 authority 在这里（``ruleset_state`` + ``event_ledger``）；
- LLM 只能叙事，不能通过叙事标签改写本运行时的权威资源。

本模块故意**不**急切导入 ``runtime``：``src/rulesets/__init__`` 已经会导入
``builtin``，这里再导入 runtime 会形成循环。要取运行时类请直接
``from src.rulesets.custom.runtime import CustomDeclarativeRuntime``。

二开入口见 ``src/rulesets/custom/README.md``。
"""

from __future__ import annotations

__all__ = ["CustomDeclarativeRuntime", "RUNTIME_ID"]


def __getattr__(name: str):
    """惰性暴露运行时类，避免导入期循环。"""

    if name in {"CustomDeclarativeRuntime", "RUNTIME_ID"}:
        from src.rulesets.custom import runtime as _runtime

        return getattr(_runtime, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
