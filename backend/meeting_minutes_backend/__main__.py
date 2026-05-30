from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import uvicorn


def _start_parent_watchdog() -> None:
    """背景 thread：每 3 秒確認父程序（uv / Electron）是否還活著。

    若父程序消失（ppid 變成 1 = 被 reparent 到 launchd），
    立刻用 os._exit(0) 終止自身（以及所有 daemon thread 和 MLX worker 子程序）。
    這可處理 Electron crash / Force Quit 導致的孤兒殘留問題。
    """
    initial_ppid = os.getppid()

    def _watch() -> None:
        while True:
            time.sleep(3)
            if os.getppid() != initial_ppid:
                # 父程序已死亡：先強殺所有 MLX worker subprocess（os._exit 跳過 atexit
                # → 不清的話 PPE worker 會變孤兒，多次重啟後累積數十個 python3.12 殘留）
                try:
                    from .faster_asr import _shutdown_all_executors
                    _shutdown_all_executors()
                except Exception:
                    pass
                os._exit(0)

    t = threading.Thread(target=_watch, daemon=True, name="parent-watchdog")
    t.start()


def main() -> int:
    parser = argparse.ArgumentParser(prog="meeting-minutes-backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    _start_parent_watchdog()

    config = uvicorn.Config(
        "meeting_minutes_backend.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
    )
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
