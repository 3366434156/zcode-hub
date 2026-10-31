"""公网隧道（app/tunnel.py）的纯逻辑测试：不拉起 cloudflared，只验状态与命令构造。

真跑一条隧道要出网、要占用 Cloudflare 的随机域名，不适合放进单测；
这里锁住的是「用户能看到的判定」——找不到二进制、token 缺失、默认口令警告、
两种模式各自拼出的 argv。
"""

from __future__ import annotations

import pytest

from app import settings
from app import tunnel as tunnel_mod


class _FakeStore:
    def __init__(self, *, admin_key: str = "zcode", gateway_key: str = "", values: dict | None = None):
        self._admin = admin_key
        self._gateway = gateway_key
        self._values = dict(values or {})

    def admin_key(self) -> str:
        return self._admin

    def gateway_key(self) -> str:
        return self._gateway

    def get_setting(self, key, default=None):
        return self._values.get(key, default)

    def set_setting(self, key, value):
        self._values[key] = str(value)


@pytest.fixture()
def fake_store(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(tunnel_mod, "store", store)
    return store


def test_executable_missing_is_reported(monkeypatch, fake_store):
    monkeypatch.setattr(tunnel_mod._Tunnel, "binary", lambda self: "")
    state = tunnel_mod.tunnel.status()
    assert state["available"] is False
    assert state["running"] is False


def test_start_without_cloudflared_raises(monkeypatch, fake_store):
    monkeypatch.setattr(tunnel_mod._Tunnel, "binary", lambda self: "")
    with pytest.raises(tunnel_mod.TunnelError):
        tunnel_mod.tunnel.start(mode="quick", persist=False)


def test_token_mode_requires_token(monkeypatch, fake_store):
    monkeypatch.setattr(tunnel_mod._Tunnel, "binary", lambda self: "cloudflared")
    with pytest.raises(tunnel_mod.TunnelError):
        tunnel_mod.tunnel.start(mode="token", token="", persist=False)


def test_quick_argv_targets_loopback(monkeypatch, fake_store):
    t = tunnel_mod._Tunnel()
    t._mode = "quick"
    monkeypatch.setattr(settings, "PORT", 3000)
    argv = t._argv("cloudflared")
    assert argv[:3] == ["cloudflared", "tunnel", "--no-autoupdate"]
    assert argv[-1] == "http://127.0.0.1:3000"
    assert "--url" in argv


def test_token_argv_uses_token_not_url(fake_store):
    t = tunnel_mod._Tunnel()
    t._mode = "token"
    t._token_value = "eyJhIjoi-demo"
    argv = t._argv("cloudflared")
    assert argv[:4] == ["cloudflared", "tunnel", "--no-autoupdate", "run"]
    assert argv[-2:] == ["--token", "eyJhIjoi-demo"]
    assert "--url" not in argv


def test_warnings_ignore_unsafe_state_when_clean(monkeypatch, fake_store):
    monkeypatch.setattr(settings, "DEFAULT_ADMIN_KEY", "zcode")
    t = tunnel_mod._Tunnel()
    assert t._warnings()  # 默认口令 + 空网关 Key，两条都该报
    fake_store._admin = "a-long-random-password"
    fake_store._gateway = "sk-zcode-demo"
    assert t._warnings() == []


def test_url_regex_only_matches_trycloudflare():
    ok = "2026-10-28T01:00:00Z INF |  https://str-date-adaptation-camera.trycloudflare.com  |"
    assert tunnel_mod._QUICK_RE.search(ok).group(0).endswith(".trycloudflare.com")
    assert tunnel_mod._QUICK_RE.search("https://gw.example.com") is None
