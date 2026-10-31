"""着色器工单后台轮询：周期性推进 burner_core 状态机（派单/验收）。

单例 + 守护线程（抄 tunnel.py 的 _Tunnel 模式）。轮询间隔 10s；
"立即派单"等 API 直接调 burner_core.tick(force=True)，与本线程共用 RLock。
"""

from __future__ import annotations

import threading

from . import burner_core, logs

_INTERVAL = 10.0


class _Burner:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._desired = False
        self._stop_event = threading.Event()
        self._last_result: dict = {}

    def status(self) -> dict:
        with self._lock:
            return {
                "running": bool(self._desired and self._thread and self._thread.is_alive()),
                "lastTick": self._last_result,
            }

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._desired = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="burner", daemon=True)
            self._thread.start()
            logs.info("burner", "后台轮询已启动（10s/tick）")
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._desired = False
            self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        logs.info("burner", "后台轮询已停止")
        return self.status()

    def _loop(self) -> None:
        while self._desired:
            try:
                self._last_result = burner_core.tick()
            except Exception as err:
                # 状态机异常绝不能带死轮询线程，记日志下轮再战
                try:
                    logs.err("burner", f"tick 异常: {err}")
                except Exception:
                    pass
                self._last_result = {"action": "error", "reason": str(err)}
            self._stop_event.wait(_INTERVAL)


burner = _Burner()
