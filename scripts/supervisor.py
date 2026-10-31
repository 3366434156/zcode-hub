"""网关守护进程：拉起 cli.py serve，异常退出时自动重启并记录证据。

为什么需要：实测网关「自动关闭」从来不是崩溃（Windows 事件日志里没有
python 崩溃记录、gateway.out 里没有 traceback），而是**被终止**——最常见的
就是 launcher 那个控制台窗口被顺手关掉（.bat 里写着 "Keep this window
open. Closing it stops the server."）。守护进程做两件事：

1. 记录每次子进程退出的「退出码 + 存活时长」，追加到 data/logs/supervisor.log
   ——下次再关，至少知道是几号几点被什么码终止的。
2. 退出后自动重启（指数退避，上限 60s），窗口没关服务就一直在。

用法（start-zcode-proxy.bat 已接入）：
    python scripts/supervisor.py [--port 3000] [--max-restarts 50]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "data" / "logs" / "supervisor.log"


def log(line: str) -> None:
    """证据落盘（supervisor 自己的日志）：时间 + 事件 + 退出码 + 存活时长。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{stamp}] {line}"
    print(text, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="zcode-hub 守护进程")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--max-restarts", type=int, default=50,
                        help="连续异常重启上限（防 crash loop 烧 CPU），默认 50")
    args = parser.parse_args()

    python = sys.executable
    cli = str(ROOT / "cli.py")
    restarts = 0
    backoff = 2.0

    log(f"supervisor 启动：port={args.port} python={python}")

    while restarts < args.max_restarts:
        started = time.time()
        log(f"启动网关（第 {restarts + 1} 次）")
        try:
            proc = subprocess.run(
                [python, cli, "serve", "--port", str(args.port)],
                cwd=str(ROOT),
            )
            code = proc.returncode
        except KeyboardInterrupt:
            log("收到 Ctrl+C：停止守护（子进程已随 Ctrl+C 结束）")
            return 0
        except Exception as err:  # noqa: BLE001 - 启动失败也要记录并退避
            code = -1
            log(f"启动异常: {err}")

        uptime = time.time() - started
        # 正常退出（0）或跑了很久才退：不退避，立即重启——用户可能就是手动关的
        if code == 0 or uptime > 300:
            log(f"网关退出 code={code} 存活 {uptime:.0f}s（长跑/正常退出，立即重启）")
            backoff = 2.0
            restarts += 1
            continue

        restarts += 1
        log(f"网关异常退出 code={code} 存活 {uptime:.0f}s，{backoff:.0f}s 后重启"
            f"（{restarts}/{args.max_restarts}）")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60.0)

    log(f"连续重启达到上限 {args.max_restarts}，守护停止（避免 crash loop 空转）。"
        f"请查 data/logs/ 下最近的 gateway-*.jsonl 定位原因。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
