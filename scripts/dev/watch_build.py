"""构建观察器：把"还在下载"和"真的卡死了"区分开。

设计要点（针对"构建看起来卡住"这个具体问题）：

- **不依赖构建进程配合**：只读日志文件的 mtime/大小 + 状态文件 + node 进程数；
- 核心指标是 **静默时长 = now - 日志最后写入时间**。npm 下载大包时可以几分钟不打印任何东西
  （实测 naive-ui 单包 158 秒），所以静默长 ≠ 卡死，必须**结合 node 进程是否存活**判断；
- 报告里始终显示"当前步骤真实已用时"，不依赖构建脚本写入的旧值。

用法::

    python scripts/dev/watch_build.py                 # 观察 120 秒
    python scripts/dev/watch_build.py --seconds 300   # 观察 300 秒

退出码：0 = 构建成功；1 = 构建失败；2 = 观察窗口结束但仍在进行（可再次运行）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOG = REPO / "logs" / "build_frontend.log"
STATUS = REPO / "logs" / "build_frontend_status.json"

# 单个大包下载实测可达 158s，所以静默阈值按这个量级取。
SILENT_SLOW_S = 60
SILENT_SUSPECT_S = 240


def node_process_count() -> int:
    """用 tasklist 数 node.exe。

    注意：Windows 新版已移除 wmic，只能用 tasklist。
    """

    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq node.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:  # noqa: BLE001
        return -1
    return sum(1 for line in out.splitlines() if line.strip().startswith('"node.exe"'))


def read_status() -> dict:
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def elapsed_since(iso: str) -> float | None:
    try:
        return (datetime.now() - datetime.fromisoformat(str(iso))).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def tail(count: int = 8) -> list[str]:
    if not LOG.exists():
        return []
    lines = [l for l in LOG.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    return lines[-count:]


def snapshot() -> dict:
    status = read_status()
    silent = time.time() - LOG.stat().st_mtime if LOG.exists() else None
    return {
        "state": status.get("state", "?"),
        "step": f"{status.get('step_index', '?')}/{status.get('step_total', '?')} {status.get('step', '?')}",
        "step_elapsed": elapsed_since(status.get("step_started_at", "")),
        "lines": status.get("step_lines"),
        "log_kb": (LOG.stat().st_size // 1024) if LOG.exists() else 0,
        "silent": silent,
        "node_procs": node_process_count(),
        "last_line": str(status.get("last_line", ""))[:110],
    }


def verdict(snap: dict) -> str:
    if snap["state"] == "done":
        return "已完成"
    if snap["state"] == "failed":
        return "失败"
    silent = snap["silent"] or 0
    if snap["node_procs"] == 0:
        return "状态是 running 但没有 node 进程 —— 可能已死"
    if silent < SILENT_SLOW_S:
        return "活跃输出中"
    if silent < SILENT_SUSPECT_S:
        return f"静默 {silent:.0f}s —— 正常（npm 下载大包时不打印）"
    return f"静默 {silent:.0f}s 且超阈值，怀疑卡住"


def render(snap: dict, index: int) -> None:
    def fmt(value: object, suffix: str = "") -> str:
        return "?" if value is None else f"{value}{suffix}"

    step_elapsed = None if snap["step_elapsed"] is None else round(snap["step_elapsed"])
    silent = None if snap["silent"] is None else round(snap["silent"])
    print(
        f"[{index:02d}] 状态={snap['state']:<8s} 步骤={snap['step']:<10s} "
        f"本步已用={fmt(step_elapsed, 's'):>8s} 日志={snap['log_kb']:>5d}KB "
        f"静默={fmt(silent, 's'):>6s} node进程={snap['node_procs']}"
    )
    print(f"     {verdict(snap)}")
    if snap["last_line"]:
        print(f"     最近: {snap['last_line']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="前端构建观察器")
    parser.add_argument("--seconds", type=int, default=120, help="观察窗口（秒）")
    parser.add_argument("--interval", type=int, default=10, help="刷新间隔（秒）")
    args = parser.parse_args()

    deadline = time.time() + args.seconds
    index = 0
    snap: dict = {}
    while True:
        index += 1
        snap = snapshot()
        render(snap, index)
        if snap["state"] in ("done", "failed"):
            print("\n=== 构建结束，最后 8 行日志 ===")
            for line in tail(8):
                print("   ", line[:150])
            return 0 if snap["state"] == "done" else 1
        if time.time() >= deadline:
            print(f"\n=== 观察窗口结束（{args.seconds}s），构建仍在进行 ===")
            print("   再次运行本脚本可继续观察；观察不会影响后台构建。")
            return 2
        time.sleep(args.interval)


if __name__ == "__main__":
    try:  # 状态行含中文；GBK 控制台里会 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
