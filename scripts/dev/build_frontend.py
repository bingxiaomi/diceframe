"""可观察的前端构建驱动（开发/部署辅助）。

为什么需要它：`npm ci` 的输出在非 TTY 下会被缓冲，看起来像"卡住"；
更糟的是下载一个大包时可能几分钟一行都不打印（实测 naive-ui 单包 158 秒），
此时任何"看有没有新输出"的判断都会误判。

本脚本因此做四件事：

1. 子进程 stdout 逐行读取 → 立即追加写入日志文件（带时间戳）；
2. **后台心跳线程每 5 秒刷新状态文件** —— 即使一行输出都没有，状态也在跳，
   这样"在下载"和"真卡死"才有可区分的外部信号；
3. 跳过 Playwright / Puppeteer 的浏览器下载（构建不需要，却是最容易长时间无输出的一步）；
4. 默认走 npmmirror 国内镜像（官方源实测单包可达 158 秒）。

用法::

    python scripts/dev/build_frontend.py
    python scripts/dev/build_frontend.py --registry https://registry.npmjs.org
    python scripts/dev/build_frontend.py --ci-only          # 只装依赖不构建
    python scripts/dev/build_frontend.py --build-only       # 只构建（依赖已装好）

配套观察器：`scripts/dev/watch_build.py`（显示步骤/真实已用时/静默时长/进程存活）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend-v2"
LOG_DIR = REPO / "logs"
LOG = LOG_DIR / "build_frontend.log"
STATUS = LOG_DIR / "build_frontend_status.json"

DEFAULT_REGISTRY = "https://registry.npmmirror.com"

# 心跳共享状态：主线程更新，心跳线程写入状态文件。
_BEAT: dict[str, object] = {
    "step": "", "started": 0.0, "lines": 0, "last": "", "stop": False, "total": 0,
}


def resolve_npm() -> str | None:
    """解析 npm.cmd 的绝对路径。

    不依赖 PATH（Windows 上 node 未必在 PATH 里），并且**必须用 .cmd 而不是 .ps1**：
    PowerShell 默认执行策略会拒绝 npm.ps1，报"在此系统上禁止运行脚本"。
    """

    found = shutil.which("npm.cmd") or shutil.which("npm")
    if found and found.lower().endswith(".cmd"):
        return found
    for candidate in (
        Path(r"C:\Program Files\nodejs\npm.cmd"),
        Path(r"C:\Program Files (x86)\nodejs\npm.cmd"),
        Path(os.environ.get("APPDATA", "")) / "npm" / "npm.cmd",
    ):
        if candidate.is_file():
            return str(candidate)
    return found


def log(line: str) -> None:
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now():%H:%M:%S}] {line}\n")
        handle.flush()


def write_status(**fields: object) -> None:
    data: dict[str, object] = {"updated_at": datetime.now().isoformat(timespec="seconds")}
    if STATUS.exists():
        try:
            data.update(json.loads(STATUS.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    data.update(fields)
    try:
        STATUS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _heartbeat_loop() -> None:
    """即使子进程长时间不输出，状态文件也必须保持新鲜。"""

    while not _BEAT["stop"]:
        started = float(_BEAT["started"] or 0)
        write_status(
            state="running",
            step=_BEAT["step"],
            step_elapsed_s=round(time.time() - started, 1) if started else None,
            step_lines=_BEAT["lines"],
            last_line=str(_BEAT["last"])[:200],
            step_index=_BEAT.get("index"),
            step_total=_BEAT.get("total"),
            heartbeat=True,
        )
        time.sleep(5)


def run_step(name: str, args: list[str], npm: str, env: dict[str, str],
             index: int, total: int) -> int:
    log(f"[{index}/{total}] {name}: npm {' '.join(args)}")
    _BEAT.update(step=name, started=time.time(), lines=0, last="", index=index, total=total)
    write_status(
        state="running", step=name, step_index=index, step_total=total,
        step_started_at=datetime.now().isoformat(timespec="seconds"),
        step_lines=0, last_line="",
    )
    started = time.time()
    proc = subprocess.Popen(  # noqa: S603 - 命令与参数都由本文件固定
        [npm, *args], cwd=str(FRONTEND), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    lines = 0
    assert proc.stdout is not None
    for raw in proc.stdout:
        lines += 1
        line = raw.rstrip()
        if line:
            with LOG.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        _BEAT["lines"] = lines
        _BEAT["last"] = line
        if lines % 25 == 0:
            write_status(
                step=name, step_lines=lines,
                step_elapsed_s=round(time.time() - started, 1),
                last_line=line[:200],
            )
    code = proc.wait()
    elapsed = round(time.time() - started, 1)
    log(f"[{index}/{total}] {name} 结束 exit={code} 用时={elapsed}s 输出={lines} 行")
    write_status(step=name, step_exit=code, step_elapsed_s=elapsed, step_lines=lines)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="可观察的前端构建驱动")
    parser.add_argument("--registry", default=os.environ.get(
        "DICEFRAME_NPM_REGISTRY", DEFAULT_REGISTRY),
        help=f"npm 源，默认 {DEFAULT_REGISTRY}")
    parser.add_argument("--ci-only", action="store_true", help="只执行 npm ci")
    parser.add_argument("--build-only", action="store_true", help="只执行 npm run build")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    overall = time.time()
    npm = resolve_npm()
    write_status(state="starting", npm=npm, repo=str(REPO), registry=args.registry)
    log(f"=== 前端构建开始 === 仓库={REPO}")
    log(f"registry = {args.registry}")
    if not npm:
        log("!! 找不到 npm.cmd。请先安装 Node.js 20.19+ / 22.12+ 并确保 npm 在 PATH 中。")
        write_status(state="failed", error="npm.cmd not found")
        return 1
    node_exe = Path(npm).parent / "node.exe"
    if node_exe.is_file():
        try:
            version = subprocess.run([str(node_exe), "-v"], capture_output=True,
                                     text=True, timeout=20).stdout.strip()
            log(f"npm = {npm} | node = {version}")
        except Exception as exc:  # noqa: BLE001
            log(f"npm = {npm} | node -v 读取失败: {exc}")
    log("（已跳过 Playwright/Puppeteer 浏览器下载：构建不需要）")

    env = dict(os.environ)
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    env["PUPPETEER_SKIP_DOWNLOAD"] = "1"
    env["npm_config_audit"] = "false"
    env["npm_config_fund"] = "false"
    env["npm_config_loglevel"] = "http"
    env["PATH"] = str(Path(npm).parent) + os.pathsep + env.get("PATH", "")

    steps: list[tuple[str, list[str]]] = []
    if not args.build_only:
        steps.append(("deps", ["ci", f"--registry={args.registry}", "--no-audit", "--no-fund"]))
    if not args.ci_only:
        steps.append(("build", ["run", "build"]))

    for index, (name, step_args) in enumerate(steps, 1):
        code = run_step(name, step_args, npm, env, index, len(steps))
        if code != 0:
            log(f"!! 步骤 {name} 失败（exit={code}），终止后续步骤")
            write_status(state="failed", failed_step=name, exit_code=code,
                         total_elapsed_s=round(time.time() - overall, 1))
            return code

    total = round(time.time() - overall, 1)
    log(f"=== 构建完成，总用时 {total}s ===")
    write_status(state="done", total_elapsed_s=total, failed_step=None, exit_code=0)
    return 0


if __name__ == "__main__":
    try:  # 中文/符号在 GBK 控制台里会崩，强制 UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    code = main()
    _BEAT["stop"] = True
    sys.exit(code)
