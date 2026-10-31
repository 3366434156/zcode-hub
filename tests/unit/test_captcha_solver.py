"""验证码求解器测试：浏览器路径探测 + 失败原因透出。

背景（2026-10-27 现场）：Windows 上 CHROMIUM_PATH 缺省写死 `/usr/local/bin/chromium`，
求解器直接以「未找到可执行 Chromium」退出；而网关把求解器的 stderr 丢进了 DEVNULL，
于是界面上只剩一句「验证码求解失败: 多次重试无结果」，看不出真正原因。
"""

from __future__ import annotations

import asyncio

import pytest

from app import captcha as captcha_module
from app import settings


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """求解走直连：测试里也不该让代理变量影响子进程环境。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)


class TestFindChromium:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("ZCODE_CHROMIUM_PATH", r"D:\custom\chrome.exe")
        assert settings._find_chromium() == r"D:\custom\chrome.exe"

    def test_first_existing_candidate(self, monkeypatch):
        monkeypatch.delenv("ZCODE_CHROMIUM_PATH", raising=False)
        monkeypatch.setattr(settings, "_CHROMIUM_CANDIDATES", [r"C:\nope.exe", r"C:\yes.exe"])
        monkeypatch.setattr(settings.os.path, "exists", lambda p: p == r"C:\yes.exe")
        assert settings._find_chromium() == r"C:\yes.exe"

    def test_falls_back_to_first_candidate(self, monkeypatch):
        """全都不存在时返回首选项，让求解器报出可读错误而不是空值。"""
        monkeypatch.delenv("ZCODE_CHROMIUM_PATH", raising=False)
        monkeypatch.setattr(settings, "_CHROMIUM_CANDIDATES", [r"C:\nope.exe"])
        monkeypatch.setattr(settings.os.path, "exists", lambda p: False)
        assert settings._find_chromium() == r"C:\nope.exe"


class _FakeProc:
    """假的 node 子进程：communicate 直接返回预置的 stdout/stderr。"""

    def __init__(self, out: bytes = b"", err: bytes = b"", code: int = 0) -> None:
        self._out, self._err, self.returncode = out, err, code
        self.killed = False

    async def communicate(self):
        return self._out, self._err

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.fixture
def solver_ctx(tmp_path, monkeypatch):
    """把求解器指向一个临时存在文件，并避免真起 node。"""
    script = tmp_path / "solver_pw.js"
    script.write_text("// noop", encoding="utf-8")
    monkeypatch.setattr(settings, "CAPTCHA_SOLVER_JS", script)
    monkeypatch.setattr(settings, "NODE_PATH", "node")
    monkeypatch.setattr(settings, "CHROMIUM_PATH", r"C:\Program Files\msedge.exe")
    return script


def _patch_proc(monkeypatch, proc: _FakeProc) -> None:
    async def fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


class TestRunSolver:
    async def test_returns_verify_param(self, monkeypatch, solver_ctx):
        _patch_proc(monkeypatch, _FakeProc(out=b"VERIFY_PARAM=abc123\n"))
        manager = captcha_module.CaptchaManager()
        assert await manager._run_solver("scene", "cn", "prefix") == "abc123"

    async def test_missing_chromium_reason_surfaces(self, monkeypatch, solver_ctx):
        """求解器失败时把 stderr 最后一行抛出来（原先是 DEVNULL，什么都看不到）。"""
        _patch_proc(monkeypatch, _FakeProc(
            err="[pw] 未找到可执行 Chromium（设 ZCODE_CHROMIUM_PATH）\n".encode(), code=3))
        manager = captcha_module.CaptchaManager()
        with pytest.raises(RuntimeError) as info:
            await manager._run_solver("scene", "cn", "prefix")
        assert "未找到可执行 Chromium" in str(info.value)
        assert "3" in str(info.value)  # 退出码也带上，便于定位

    async def test_timeout_reported(self, monkeypatch, solver_ctx):
        """超时也要有原因（原先只是静默返回 None）。"""
        proc = _FakeProc()
        hung = asyncio.Event()

        async def slow_communicate():
            await hung.wait()
            return b"", b""

        proc.communicate = slow_communicate  # type: ignore[method-assign]
        _patch_proc(monkeypatch, proc)
        monkeypatch.setattr(settings, "CAPTCHA_SOLVE_TIMEOUT", 0.05)
        manager = captcha_module.CaptchaManager()
        with pytest.raises(RuntimeError) as info:
            await manager._run_solver("scene", "cn", "prefix")
        assert "超时" in str(info.value)
        assert proc.killed is True


class TestSolveOne:
    async def test_last_error_carries_solver_reason(self, monkeypatch, solver_ctx):
        """_solve_one 的失败原因（会显示在领取回执里）来自求解器 stderr。"""
        _patch_proc(monkeypatch, _FakeProc(err="[pw] 浏览器启动失败\n".encode(), code=1))
        monkeypatch.setattr(settings, "CAPTCHA_SOLVE_RETRIES", 1)
        manager = captcha_module.CaptchaManager()
        assert await manager._solve_one({"sceneId": "s", "region": "cn", "prefix": "p"}) is None
        assert "浏览器启动失败" in manager._last_error
