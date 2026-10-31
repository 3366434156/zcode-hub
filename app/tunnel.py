"""公网隧道（cloudflared）：把本机网关暴露到公网，让「其它网络」也能用。

两种形态：
  quick  临时隧道，不需要 Cloudflare 账号，每次启动给一个随机
         https://xxx.trycloudflare.com，进程一停就失效——适合「现在就把地址
         发给朋友」。
  token  固定隧道，在 Cloudflare 控制台建好隧道、绑好域名后，用 --token
         一条命令拉起；域名重启不变。源站地址（Public Hostname →
         http://127.0.0.1:PORT）在控制台里配一次即可。

约束：
  - 隧道只连本机回环地址，它是「不开公网入口也能被外部访问」的方案，
    不是把 3000 端口直接开到公网的替代品。
  - 公网地址只在后台接口（需 admin key）返回，不进 /meta，避免未鉴权泄露。
  - 进程意外退出自动重连（指数退避、上限 30 次）：Cloudflare 边缘偶发
    断链不该要求人守着窗口手动重启。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import logs, settings
from .store import store

# 临时隧道域名（quick tunnel）：https://<随机词>.trycloudflare.com
_QUICK_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
# cloudflared 的日志级别是行内 token（"…Z ERR precheck …"）。只认 ERR/FTL：
# quick 隧道起手会打 "UDP Connectivity … status=fail" 这类 INF 预检噪音，
# 按关键字 "failed" 匹配会把正常连接记成错误。
_ERR_RE = re.compile(r"\b(ERR|FTL)\b")

# Windows 上没有控制台窗口：.bat 启动的 python 是控制台程序，再拉一个带窗口的
# 子进程会多出一个黑框，用户关错窗口就把隧道关了。
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_MAX_RESTARTS = 30


class TunnelError(RuntimeError):
    """隧道无法启动（找不到 cloudflared、token 缺失等用户可修正的原因）。"""


class _Tunnel:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._desired = False
        self._running = False
        self._mode = ""
        self._url = ""
        self._error = ""
        self._started_at = 0.0
        self._restarts = 0
        self._token_value = ""
        self._tail: list[str] = []

    # ── 二进制发现 ───────────────────────────────────────────────────────────
    def binary(self) -> str:
        """cloudflared 可执行文件路径；找不到返回空串。"""
        override = (settings.TUNNEL_BIN or "").strip()
        if override and Path(override).exists():
            return override
        found = shutil.which("cloudflared")
        if found:
            return found
        for candidate in (
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            r"C:\Program Files\cloudflared\cloudflared.exe",
            str(Path.home() / "cloudflared.exe"),
            "/usr/local/bin/cloudflared",
            "/opt/homebrew/bin/cloudflared",
        ):
            if Path(candidate).exists():
                return candidate
        return ""

    # ── 状态 ─────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            # 用 _running（「用户要它在跑」且后台线程还活着），而不是
            # proc.poll()：spawn 到 cloudflared 真正建立连接之间有几秒，
            # 那段时间按 proc 判断会让界面闪回「未开启」。
            running = bool(self._running)
            data = {
                "available": bool(self.binary()),
                "binary": self.binary(),
                "running": running,
                "mode": self._mode,
                "url": self._url,
                "error": self._error,
                "started_at": self._started_at,
                "uptime": int(time.time() - self._started_at) if running and self._started_at else 0,
                "restarts": self._restarts,
                "enabled": self._enabled_setting(),
                "token_set": bool(self._token()),
                "port": settings.PORT,
                "tail": list(self._tail[-8:]),
                "warnings": self._warnings(),
            }
        return data

    def public_url(self) -> str:
        """当前公网地址（未开启返回空串）。只读、不建 dict，给请求热路径用。"""
        with self._lock:
            return self._url or ""

    def _warnings(self) -> list[str]:
        """暴露前的硬前提：不给启动按钮加门槛（用户可能只想试一下），
        但只要不满足，卡片上就一直挂着红字——不管隧道是开着还是还没开。"""
        out: list[str] = []
        if (store.admin_key() or "") == settings.DEFAULT_ADMIN_KEY:
            out.append("后台口令还是默认值，公网任何人试一次就可能进来，请立刻在「鉴权」里改掉")
        if not (store.gateway_key() or "").strip():
            out.append("网关 API Key 未设置，公网任何人都能免 Key 调用 /v1/*")
        return out

    # ── 持久化开关 ───────────────────────────────────────────────────────────
    def _enabled_setting(self) -> bool:
        return str(store.get_setting("tunnel_enabled", "")).strip() in ("1", "true", "yes", "on")

    def _token(self) -> str:
        return str(store.get_setting("tunnel_token", "") or "").strip()

    # ── 生命周期 ─────────────────────────────────────────────────────────────
    def start(self, mode: str = "quick", token: str = "", persist: bool = True) -> dict:
        """拉起隧道。已运行时直接返回当前状态（幂等）。"""
        mode = (mode or "quick").strip().lower()
        if mode not in ("quick", "token"):
            raise TunnelError("模式只能是 quick 或 token")
        binary = self.binary()
        if not binary:
            raise TunnelError(
                "没有找到 cloudflared。装一个再试：winget install Cloudflare.cloudflared"
                "（或把路径写进 ZCODE_CLOUDFLARED）"
            )
        if mode == "token":
            token = (token or self._token()).strip()
            if not token:
                raise TunnelError("token 模式需要在 Cloudflare 控制台建隧道后填入 token")

        with self._lock:
            if self._running and self._proc and self._proc.poll() is None:
                return self.status()
            if persist:
                store.set_setting("tunnel_mode", mode)
                store.set_setting("tunnel_enabled", "1")
                if token:
                    store.set_setting("tunnel_token", token)
            self._mode = mode
            self._token_value = token
            self._url = ""
            self._error = ""
            self._restarts = 0
            self._tail.clear()
            self._desired = True
            self._running = True
            self._thread = threading.Thread(target=self._loop, name="cloudflared", daemon=True)
            self._thread.start()
        logs.step("tunnel", f"正在建立公网隧道（{mode}）…")
        return self.status()

    def stop(self, persist: bool = True) -> dict:
        with self._lock:
            was_running = self._running
            self._desired = False
            self._running = False
            proc = self._proc
            self._proc = None
            self._url = ""
        if persist:
            store.set_setting("tunnel_enabled", "0")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if was_running:
            logs.step("tunnel", "公网隧道已关闭")
        return self.status()

    def autostart(self) -> None:
        """进程启动时按持久化开关自动建隧道（失败只留痕，不影响网关启动）。"""
        if not (settings.TUNNEL_AUTOSTART or self._enabled_setting()):
            return
        mode = str(store.get_setting("tunnel_mode", "quick") or "quick").strip().lower()
        try:
            self.start(mode=mode, persist=False)
        except TunnelError as err:
            logs.warn("tunnel", f"自动开启隧道失败：{err}")

    # ── 子进程 ───────────────────────────────────────────────────────────────
    def _argv(self, binary: str) -> list[str]:
        if self._mode == "token":
            return [binary, "tunnel", "--no-autoupdate", "run",
                    "--token", getattr(self, "_token_value", "") or self._token()]
        return [binary, "tunnel", "--no-autoupdate",
                "--url", f"http://127.0.0.1:{settings.PORT}"]

    def _loop(self) -> None:
        delay = 3.0
        try:
            self._loop_body(delay)
        finally:
            # 线程是唯一知道「已经彻底不跑了」的地方：由它收尾，status() 才能
            # 如实回报 running=False（start/stop 改的都是意愿，不是事实）。
            with self._lock:
                self._running = False

    def _loop_body(self, delay: float) -> None:
        while True:
            with self._lock:
                if not self._desired:
                    return
                binary = self.binary()
                if not binary:
                    self._error = "cloudflared 不见了"
                    self._desired = False
                    return
            try:
                proc = subprocess.Popen(
                    self._argv(binary),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except OSError as err:
                with self._lock:
                    self._error = f"启动 cloudflared 失败：{err}"
                    self._desired = False
                logs.err("tunnel", self._error)
                return

            with self._lock:
                self._proc = proc
                self._started_at = time.time()
                self._url = ""
                if self._restarts == 0:
                    self._error = ""

            got_url = self._pump(proc)
            code = proc.wait()

            with self._lock:
                self._proc = None
                if not self._desired:
                    return
                self._url = ""
                if got_url:
                    self._restarts = 0
                    delay = 3.0
                self._restarts += 1
                if self._restarts > _MAX_RESTARTS:
                    self._error = f"隧道连续退出（code {code}），已停止重试"
                    self._desired = False
                    logs.err("tunnel", self._error)
                    return
                self._error = f"隧道进程退出（code {code}），{delay:.0f}s 后重连"
            logs.warn("tunnel", self._error)
            time.sleep(delay)
            delay = min(delay * 2, 60)

    def _pump(self, proc: subprocess.Popen) -> bool:
        """读 cloudflared 输出，抓到公网地址；返回本轮是否拿到过地址。"""
        got = False
        stream = proc.stdout
        if stream is None:
            return False
        for raw in stream:
            line = raw.rstrip()
            if not line:
                continue
            first_seen = ""
            with self._lock:
                self._tail.append(line[:400])
                if len(self._tail) > 40:
                    del self._tail[:-40]
                match = _QUICK_RE.search(line)
                if match and not self._url:
                    self._url = match.group(0)
                    first_seen = self._url
            if first_seen:
                got = True
                logs.ok("tunnel", f"公网地址已就绪：{first_seen}")
            elif _ERR_RE.search(line):
                with self._lock:
                    self._error = line[:300]
        return got


tunnel = _Tunnel()
