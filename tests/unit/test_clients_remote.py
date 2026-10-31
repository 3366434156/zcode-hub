"""局域网共享 / 其他电脑（magpie 的 Remote magpie）单元测试。

覆盖：LAN 地址探测后的命令拼装、magpie 网关 Key 的复用与新建、共享开关的幂等写入
（保留其它设置、写前备份、坏 JSON 拒绝）、遥控命令格式、看板 remote 区块。

全部跑在 tmp 目录（ZCODE_MAGPIE_DIR / ZCODE_AGENT_STATE_DIR），不会碰真实 magpie。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app import clients


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    magpie = tmp_path / "magpie"
    magpie.mkdir()
    monkeypatch.setenv("ZCODE_MAGPIE_DIR", str(magpie))
    monkeypatch.setenv("ZCODE_AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COMPUTERNAME", "Office-PC")
    monkeypatch.setattr(clients, "lan_ipv4", lambda: ["192.168.1.50"])
    monkeypatch.setitem(clients.CLIENTS["magpie"], "running", lambda: True)
    return {"magpie": magpie, "settings": magpie / "settings.json",
            "keys": magpie / "caller-keys.json", "tmp": tmp_path}


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestHostAndCommands:
    def test_host_id_from_computer_name(self, sandbox):
        assert clients._share_host_id() == "zcode-hub-office-pc"

    def test_host_id_falls_back(self, sandbox, monkeypatch):
        monkeypatch.delenv("COMPUTERNAME", raising=False)
        monkeypatch.delenv("HOSTNAME", raising=False)
        assert clients._share_host_id() == "zcode-hub"

    def test_remote_magpie_command(self):
        cmd = clients.remote_magpie_command(secret="sk-magpie-key-x",
                                            url="http://192.168.1.50:3425", host_id="office")
        assert cmd == "magpie provider add remote-magpie sk-magpie-key-x url=http://192.168.1.50:3425 id=office"

    def test_direct_provider_command(self):
        cmd = clients.direct_provider_command(base_url="http://192.168.1.50:3000",
                                              api_key="sk-zcode-abc",
                                              models=["GLM-5.3-Flash"])
        assert cmd.startswith("magpie provider add zcode2api key=sk-zcode-abc")
        assert "url=http://192.168.1.50:3000/v1" in cmd
        assert "anthropic=http://192.168.1.50:3000" in cmd
        assert "models=GLM-5.3-Flash" in cmd

    def test_direct_provider_command_without_key(self):
        cmd = clients.direct_provider_command(base_url="http://192.168.1.50:3000", api_key="")
        assert "key=" not in cmd


class TestGatewayKey:
    def test_reuses_existing_enabled_key(self, sandbox):
        _write_json(sandbox["keys"], [
            {"id": "k1", "name": "other", "secret": "sk-magpie-key-other"},
            {"id": "k2", "name": "zcode-hub", "secret": "sk-magpie-key-mine"},
        ])
        key = clients.magpie_gateway_key()
        assert key == {"id": "k2", "name": "zcode-hub",
                       "secret": "sk-magpie-key-mine", "created": False}

    def test_ignores_disabled_key(self, sandbox):
        _write_json(sandbox["keys"], [
            {"id": "k1", "name": "zcode-hub", "secret": "sk-magpie-key-old", "off": True},
        ])
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(clients, "magpie_cli_path", lambda: "magpie")
            monkeypatch.setattr(clients.subprocess, "run",
                                lambda *a, **k: _Proc(stdout="sk-magpie-key-new\n"))
            key = clients.magpie_gateway_key()
        finally:
            monkeypatch.undo()
        assert key["secret"] == "sk-magpie-key-new"
        assert key["created"] is True

    def test_creates_via_cli_and_reads_id(self, sandbox):
        _write_json(sandbox["keys"], [])

        def fake_run(args, **kwargs):
            assert args[1:] == ["gateway-key", "add", "zcode-hub"]
            _write_json(sandbox["keys"], [
                {"id": "new-id", "name": "zcode-hub", "secret": "sk-magpie-key-fresh"},
            ])
            return _Proc(stdout="sk-magpie-key-fresh\n")

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(clients, "magpie_cli_path", lambda: "magpie")
            monkeypatch.setattr(clients.subprocess, "run", fake_run)
            key = clients.magpie_gateway_key()
        finally:
            monkeypatch.undo()
        assert key == {"id": "new-id", "name": "zcode-hub",
                       "secret": "sk-magpie-key-fresh", "created": True}

    def test_missing_cli_is_reported(self, sandbox):
        _write_json(sandbox["keys"], [])
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(clients, "magpie_cli_path", lambda: None)
            with pytest.raises(clients.AgentConfigError, match="Gateway keys"):
                clients.magpie_gateway_key()
        finally:
            monkeypatch.undo()

    def test_cli_without_key_output_is_reported(self, sandbox):
        _write_json(sandbox["keys"], [])
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(clients, "magpie_cli_path", lambda: "magpie")
            monkeypatch.setattr(clients.subprocess, "run",
                                lambda *a, **k: _Proc(stdout="boom", stderr="nope", returncode=1))
            with pytest.raises(clients.AgentConfigError, match="未返回 Key"):
                clients.magpie_gateway_key()
        finally:
            monkeypatch.undo()

    def test_cli_timeout_is_reported(self, sandbox):
        _write_json(sandbox["keys"], [])

        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="magpie", timeout=60)

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(clients, "magpie_cli_path", lambda: "magpie")
            monkeypatch.setattr(clients.subprocess, "run", boom)
            with pytest.raises(clients.AgentConfigError, match="gateway-key add"):
                clients.magpie_gateway_key()
        finally:
            monkeypatch.undo()


class TestSharing:
    def test_enable_writes_lan_and_keeps_other_fields(self, sandbox):
        _write_json(sandbox["settings"], {"theme": "system", "tray": "panel", "lang": "system"})
        result = clients.enable_magpie_sharing()
        assert result["changed"] is True
        data = json.loads(sandbox["settings"].read_text(encoding="utf-8"))
        assert data["lan"] is True
        assert data["theme"] == "system" and data["tray"] == "panel"
        backups = list((Path(result["backup_dir"])).glob("*-settings.json"))
        assert backups, "写前必须留备份"

    def test_enable_is_idempotent(self, sandbox):
        _write_json(sandbox["settings"], {"lan": True})
        result = clients.enable_magpie_sharing()
        assert result["changed"] is False
        assert clients.magpie_sharing_enabled() is True

    def test_broken_settings_refused(self, sandbox):
        sandbox["settings"].write_text("{not json", encoding="utf-8")
        with pytest.raises(clients.AgentConfigError):
            clients.enable_magpie_sharing()
        assert sandbox["settings"].read_text(encoding="utf-8") == "{not json"

    def test_sharing_state_lists_keys_masked(self, sandbox):
        _write_json(sandbox["settings"], {"lan": True, "lanKeyId": "k1"})
        _write_json(sandbox["keys"], [
            {"id": "k1", "name": "Magpie", "lan": True, "secret": "sk-magpie-key-aaaaaaaaaaaa"},
            {"id": "k2", "name": "zcode-hub", "secret": "sk-magpie-key-bbbbbbbbbbbb", "off": True},
        ])
        state = clients.magpie_share_state()
        assert state["sharing"] is True
        assert state["urls"] == ["http://192.168.1.50:3425"]
        assert [k["enabled"] for k in state["keys"]] == [True, False]
        assert state["keys"][0]["masked"].startswith("sk-magpie-key")
        assert state["keys"][0]["masked"] != state["keys"][0]["secret"]


class TestSharePlan:
    def test_prefers_our_own_key(self, sandbox):
        _write_json(sandbox["settings"], {"lan": True, "lanKeyId": "k1"})
        _write_json(sandbox["keys"], [
            {"id": "k1", "name": "Magpie", "lan": True, "secret": "sk-magpie-key-lan"},
            {"id": "k2", "name": "zcode-hub", "secret": "sk-magpie-key-mine"},
        ])
        plan = clients.remote_share_plan(base_url="http://127.0.0.1:3000")
        assert plan["magpie"]["remote_key_name"] == "zcode-hub"
        assert "sk-magpie-key-mine" in plan["magpie"]["remote_command"]
        assert plan["magpie"]["host_id"] == "zcode-hub-office-pc"

    def test_falls_back_to_default_lan_key(self, sandbox):
        _write_json(sandbox["settings"], {"lan": True, "lanKeyId": "k1"})
        _write_json(sandbox["keys"], [
            {"id": "k1", "name": "Magpie", "lan": True, "secret": "sk-magpie-key-lan"},
            {"id": "k2", "name": "other", "secret": "sk-magpie-key-other"},
        ])
        plan = clients.remote_share_plan(base_url="http://127.0.0.1:3000")
        assert plan["magpie"]["remote_key_name"] == "Magpie"
        assert "sk-magpie-key-lan" in plan["magpie"]["remote_command"]

    def test_plan_carries_lan_gateway_commands(self, sandbox):
        _write_json(sandbox["settings"], {"lan": True})
        _write_json(sandbox["keys"], [{"id": "k2", "name": "zcode-hub",
                                       "secret": "sk-magpie-key-mine"}])
        plan = clients.remote_share_plan(base_url="http://127.0.0.1:3000", api_key="sk-zcode-gw")
        gateway = plan["gateway"]
        assert gateway["lan_url"] == "http://192.168.1.50:3000"
        assert "key=sk-zcode-gw" in gateway["direct_command"]
        assert gateway["import_link"].startswith("magpie://import?")
        assert "192.168.1.50%3A3000" in gateway["import_link"]
        assert "--key <网关 Key>" in gateway["cli_command"]

    def test_plan_without_keys_has_no_remote_command(self, sandbox):
        _write_json(sandbox["settings"], {"lan": False})
        _write_json(sandbox["keys"], [])
        plan = clients.remote_share_plan(base_url="http://127.0.0.1:3000")
        assert plan["magpie"]["remote_command"] == ""
        assert plan["magpie"]["sharing"] is False


class TestConfiguredClients:
    def test_reads_state_ledger(self, sandbox, monkeypatch):
        state_path = clients.state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"magpie": {"applied_at": "x"}}), encoding="utf-8")
        assert clients.configured_clients() == ["magpie"]

    def test_empty_when_no_state(self, sandbox):
        assert clients.configured_clients() == []


class TestGithubSync:
    def _write_sync(self, sandbox, config, state=None):
        (sandbox["magpie"] / "sync.json").write_text(json.dumps(config), encoding="utf-8")
        if state is not None:
            (sandbox["magpie"] / "sync-state.json").write_text(json.dumps(state),
                                                               encoding="utf-8")

    def test_state_disabled_without_config(self, sandbox):
        state = clients.magpie_sync_state()
        assert state["enabled"] is False
        assert state["url"] == ""

    def test_state_reports_repo_and_masks_token(self, sandbox):
        self._write_sync(sandbox, {
            "url": "github://owner/magpie-sync", "password": "gho_secret_token_value",
            "passphrase": "pp", "keys": True, "agents": True, "library": True,
        }, {"last": "2026-10-26T20:18:32+08:00"})
        state = clients.magpie_sync_state()
        assert state["enabled"] is True
        assert state["repo"] == "owner/magpie-sync"
        assert state["last"].startswith("2026-10-26")
        assert "gho_secret_token_value" not in json.dumps(state)
        assert state["token_masked"].startswith("gho_")
        assert state["passphrase_set"] is True

    def test_other_machine_command_comes_from_plan(self, sandbox):
        self._write_sync(sandbox, {"url": "github://owner/magpie-sync", "password": "x",
                                   "passphrase": "y", "keys": True})
        plan = clients.remote_share_plan(base_url="http://127.0.0.1:3000")
        assert plan["magpie"]["other_machine_command"] == "magpie github on github://owner/magpie-sync"

    def test_enable_pipes_token_and_passphrase(self, sandbox):
        seen = {}

        class _Proc:
            stdout = "GitHub sync github://owner/repo token saved\n✓ synced at 20:18"
            stderr = ""
            returncode = 0

        def fake_run(args, **kwargs):
            seen["args"] = args
            seen["input"] = kwargs.get("input")
            self._write_sync(sandbox, {"url": "github://owner/repo", "password": "tok",
                                       "passphrase": "pp", "keys": True})
            return _Proc()

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(clients, "magpie_cli_path", lambda: "magpie")
            monkeypatch.setattr(clients.subprocess, "run", fake_run)
            result = clients.enable_magpie_sync(repo="owner/repo", token="tok", passphrase="pp")
        finally:
            monkeypatch.undo()
        assert result["ok"] is True
        assert seen["args"][:4] == ["magpie", "github", "on", "github://owner/repo"]
        assert "keys=yes" in seen["args"]
        assert seen["input"] == "tok\npp\n"     # 令牌/口令只走 stdin，不进命令行
        assert result["state"]["enabled"] is True

    def test_enable_rejects_incomplete_input(self, sandbox):
        for kwargs in ({"repo": "", "token": "t", "passphrase": "p"},
                       {"repo": "owner/repo", "token": "", "passphrase": "p"},
                       {"repo": "owner/repo", "token": "t", "passphrase": ""},
                       {"repo": "justname", "token": "t", "passphrase": "p"}):
            with pytest.raises(clients.AgentConfigError):
                clients.enable_magpie_sync(**kwargs)

    def test_enable_surfaces_cli_failure(self, sandbox):
        class _Proc:
            stdout = ""
            stderr = "repository not found"
            returncode = 1

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(clients, "magpie_cli_path", lambda: "magpie")
            monkeypatch.setattr(clients.subprocess, "run", lambda *a, **k: _Proc())
            with pytest.raises(clients.AgentConfigError, match="repository not found"):
                clients.enable_magpie_sync(repo="owner/repo", token="t", passphrase="p")
        finally:
            monkeypatch.undo()

    def test_sync_now(self, sandbox):
        class _Proc:
            stdout = "✓ synced at 20:30"
            stderr = ""
            returncode = 0

        self._write_sync(sandbox, {"url": "github://owner/repo", "password": "x",
                                   "passphrase": "y"})
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(clients, "magpie_cli_path", lambda: "magpie")
            monkeypatch.setattr(clients.subprocess, "run", lambda *a, **k: _Proc())
            result = clients.magpie_sync_now()
        finally:
            monkeypatch.undo()
        assert result["ok"] is True
        assert "synced at 20:30" in result["output"]
        assert result["state"]["enabled"] is True
