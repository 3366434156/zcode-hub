"""官方发行清单 → 客户端版本自动跟进。

版本常量是长效隐患：上游按客户端版本门槛投放活动套餐，钉旧版会让整池账号
看不到活动（docs/development/05 §5 两次实证）。这里把「跟官方最新版」变成
运行期行为，并守住三条语义：只升不降、可关、失败不影响主链路。
"""

from __future__ import annotations

import asyncio

import pytest

from app import constants, settings, version_sync

REAL_MANIFEST = """version: 3.17.1
files:
    - url: https://cdn-zcode.z.ai/zcode/electron/releases/3.17.1/windows-x64/ZCode-3.17.1-win-x64.exe
      sha512: tCa3tVd3LOajWpzh+Anj+GeOgnpIP62ZPr9GmgKmJ8bcVE39Apv6j1YTjdRaHPLRxJAPFtGpXQAiB11PChuJ0Q==
      size: 233829888
path: https://cdn-zcode.z.ai/zcode/electron/releases/3.17.1/windows-x64/ZCode-3.17.1-win-x64.exe
releaseName: Release v3.17.1
releaseNotes: |-
    ## 新功能
    - 新增 ZCode Sites 官方插件
"""


@pytest.fixture(autouse=True)
def _isolate_billing_version(monkeypatch):
    """版本巡检测试不得真的改全局 billing 版本（只验证调用）。"""
    monkeypatch.setattr(constants, "set_billing_version", lambda v: None)


def test_parse_manifest_version():
    assert version_sync.parse_manifest_version(REAL_MANIFEST) == "3.17.1"


def test_parse_manifest_version_rejects_garbage():
    assert version_sync.parse_manifest_version("") is None
    assert version_sync.parse_manifest_version("files: []") is None
    assert version_sync.parse_manifest_version("nested:\n  version: 3.18.0\n") is None
    assert version_sync.parse_manifest_version(None) is None  # type: ignore[arg-type]


def test_is_newer_semantics():
    assert version_sync.is_newer("3.17.1", "3.17.0")
    assert version_sync.is_newer("3.18", "3.17.9")
    assert version_sync.is_newer("4.0.0", "3.99.99")
    assert not version_sync.is_newer("3.17.1", "3.17.1")
    assert not version_sync.is_newer("3.16.9", "3.17.0")
    # 段数不足按 0 补齐
    assert version_sync.is_newer("3.17.1", "3.17")
    assert not version_sync.is_newer("3.17", "3.17.0")


def test_adopt_only_moves_forward(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    adopted: list[str] = []
    monkeypatch.setattr(constants, "set_client_version", adopted.append)
    sync = version_sync.VersionSync()
    sync.current = "3.17.1"

    assert sync.adopt("3.17.1") is False       # 相同不切
    assert sync.adopt("3.16.3") is False       # 回退不切
    assert sync.adopt(None) is False
    assert adopted == []

    assert sync.adopt("3.18.0") is True
    assert adopted == ["3.18.0"]
    assert sync.current == "3.18.0"
    # 落盘：重启后离线也能用上跟进过的版本
    assert version_sync.load_saved_version() == "3.18.0"


def test_saved_version_roundtrip_and_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    assert version_sync.load_saved_version() is None      # 文件缺失
    monkeypatch.setattr(constants, "set_client_version", lambda v: None)
    version_sync.VersionSync().adopt("3.19.2")
    assert version_sync.load_saved_version() == "3.19.2"
    (tmp_path / "app_version.json").write_text("{不是 json", "utf-8")
    assert version_sync.load_saved_version() is None       # 损坏不抛


async def test_start_runs_patrol_and_stops(tmp_path, monkeypatch):
    """巡检轮无论是否开启跟进都要跑起来（默认只体检）。"""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "APP_VERSION_FOLLOW", False)

    async def _no_network(*_a, **_kw):
        return None

    monkeypatch.setattr(version_sync, "fetch_latest_version", _no_network)
    sync = version_sync.VersionSync()
    sync.start()
    assert sync._task is not None
    await sync.stop()
    assert sync._task is None


async def test_patrol_never_adopts_by_default(mock_server, monkeypatch, tmp_path):
    """默认只体检：清单给出更新的版本也不改钉版（3.17.1 实测 3012 的教训）。"""
    mock_app, port = mock_server
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "APP_VERSION_FOLLOW", False)
    monkeypatch.setattr(settings, "ZCODE_RELEASES_URL",
                        f"http://127.0.0.1:{port}/api/v1/releases/electron/manifest")
    adopted: list[str] = []
    monkeypatch.setattr(constants, "set_client_version", adopted.append)
    mock_app.state.release_version = "9.9.9"
    sync = version_sync.VersionSync()
    sync.current = "3.16.3"
    try:
        await sync._tick()
    finally:
        mock_app.state.release_version = None
    assert adopted == []              # 钉版不动
    assert sync.latest == "9.9.9"     # 但官方最新要能查到（/admin/api/status 用）
    assert version_sync.load_saved_version() == "9.9.9"


async def test_patrol_ignores_rollout_downgrade(mock_server, monkeypatch, tmp_path):
    """官方清单按设备灰度投放：拿到「更旧」的值不得让 latest 回落（否则告警横跳）。"""
    mock_app, port = mock_server
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "APP_VERSION_FOLLOW", False)
    monkeypatch.setattr(settings, "ZCODE_RELEASES_URL",
                        f"http://127.0.0.1:{port}/api/v1/releases/electron/manifest")
    sync = version_sync.VersionSync()
    sync.current = "3.16.3"
    mock_app.state.release_version = "3.17.1"
    try:
        await sync._tick()
        assert sync.latest == "3.17.1"
        mock_app.state.release_version = "3.16.3"     # 灰度回落
        await sync._tick()
        assert sync.latest == "3.17.1"
        assert version_sync.load_saved_version() == "3.17.1"
    finally:
        mock_app.state.release_version = None


async def test_patrol_adopts_when_explicitly_enabled(mock_server, monkeypatch, tmp_path):
    """显式 ZCODE_APP_VERSION_FOLLOW=1 时才跟进（调用方自负形状验证）。"""
    mock_app, port = mock_server
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "APP_VERSION_FOLLOW", True)
    monkeypatch.setattr(settings, "ZCODE_RELEASES_URL",
                        f"http://127.0.0.1:{port}/api/v1/releases/electron/manifest")
    adopted: list[str] = []
    monkeypatch.setattr(constants, "set_client_version", adopted.append)
    mock_app.state.release_version = "9.9.9"
    sync = version_sync.VersionSync()
    sync.current = "3.16.3"
    try:
        await sync._tick()
    finally:
        mock_app.state.release_version = None
    assert adopted == ["9.9.9"]
    assert sync.current == "9.9.9"


async def test_tick_survives_manifest_failure(mock_server, monkeypatch, tmp_path):
    """清单拉取失败（404）时只留痕，不抛、不改版本。"""
    mock_app, port = mock_server
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "ZCODE_RELEASES_URL",
                        f"http://127.0.0.1:{port}/api/v1/no-such-manifest")
    adopted: list[str] = []
    monkeypatch.setattr(constants, "set_client_version", adopted.append)
    sync = version_sync.VersionSync()
    sync.current = "3.17.1"
    await sync._tick()
    assert adopted == []
    assert sync.latest is None


async def test_stop_cancels_loop(monkeypatch):
    monkeypatch.setattr(settings, "VERSION_SYNC_INTERVAL", 3600)

    async def _no_network(*_a, **_kw):
        return None

    monkeypatch.setattr(version_sync, "fetch_latest_version", _no_network)
    sync = version_sync.VersionSync()
    sync._task = asyncio.create_task(sync._loop())
    await asyncio.sleep(0)
    await sync.stop()
    assert sync._task is None


async def test_tick_updates_billing_version_without_follow(monkeypatch, tmp_path):
    """billing 版本始终跟官方清单；messages 是否跟进由 APP_VERSION_FOLLOW 决定。"""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(constants, "set_billing_version", calls.append)
    sync = version_sync.VersionSync()
    sync.latest = "3.16.3"

    async def fake_fetch():
        return "3.17.2"

    monkeypatch.setattr(version_sync, "fetch_latest_version", fake_fetch)
    await sync._tick()
    assert calls == ["3.17.2"]
