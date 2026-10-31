"""客户端一键接入（app.clients）单元测试。

覆盖：目录口径（复刻 Go os.UserConfigDir）、providers.json 非破坏性合并、幂等、
备份/还原语义、外部修改后的保守还原、导入链接编码、CLI 入口。

全部用例都跑在 tmp 目录里（ZCODE_AGENT_HOME / ZCODE_MAGPIE_DIR /
ZCODE_AGENT_STATE_DIR），不会碰到真实的 %APPDATA%\\magpie。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import clients


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """隔离主目录 / magpie 配置目录 / 状态目录（并且不看见开发机真实的 magpie.exe）。"""
    home = tmp_path / "home"
    magpie = tmp_path / "magpie"
    state = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("ZCODE_AGENT_HOME", str(home))
    monkeypatch.setenv("ZCODE_MAGPIE_DIR", str(magpie))
    monkeypatch.setenv("ZCODE_AGENT_STATE_DIR", str(state))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setattr(clients.shutil, "which", lambda _name: None)
    monkeypatch.setitem(clients.CLIENTS["magpie"], "running", lambda: False)
    return {"home": home, "magpie": magpie, "state": state,
            "providers": magpie / "providers.json"}


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 目录口径 ─────────────────────────────────────────────────────────────────
class TestPaths:
    def test_default_is_dot_config(self, sandbox, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert clients.user_config_dir() == sandbox["home"] / ".config"
        # magpie 目录跟着走：<home>/.config/magpie
        monkeypatch.delenv("ZCODE_MAGPIE_DIR", raising=False)
        assert clients.magpie_dir() == sandbox["home"] / ".config" / "magpie"

    def test_respects_absolute_xdg(self, sandbox, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert clients.user_config_dir() == tmp_path / "xdg"

    def test_relative_xdg_ignored(self, sandbox, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
        assert clients.user_config_dir() == sandbox["home"] / ".config"

    def test_windows_also_uses_dot_config(self, sandbox, monkeypatch):
        """magpie 的 appdir.Config() 在 Windows 也不走 %APPDATA%（实测口径）。"""
        monkeypatch.setattr(clients, "_IS_WINDOWS", True)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("APPDATA", "C:/should/not/be/used")
        assert clients.user_config_dir() == sandbox["home"] / ".config"

    def test_windows_rooted_xdg(self, sandbox, monkeypatch):
        monkeypatch.setattr(clients, "_IS_WINDOWS", True)
        monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg-root")
        assert clients.user_config_dir() == Path("/xdg-root")

    def test_magpie_dir_override(self, sandbox):
        assert clients.magpie_dir() == sandbox["magpie"]
        assert clients.magpie_paths()["providers"] == sandbox["providers"]

    def test_state_dir_follows_env(self, sandbox):
        assert clients.state_dir() == sandbox["state"]
        assert clients.backups_root() == sandbox["state"] / "backups"

    def test_portable_dir_beside_exe(self, tmp_path, monkeypatch):
        """便携模式：可执行文件旁边 data/ 里已有 magpie 的文件 → 用它。"""
        monkeypatch.delenv("ZCODE_MAGPIE_DIR", raising=False)
        monkeypatch.setenv("ZCODE_AGENT_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        monkeypatch.setattr(clients.shutil, "which", lambda _name: None)
        exe = tmp_path / "localappdata" / "Programs" / "magpie" / "magpie.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        # 空的 data/ 不算便携模式
        (exe.parent / "data").mkdir()
        assert clients.magpie_portable_dir() is None
        assert clients.magpie_dir() == tmp_path / "home" / ".config" / "magpie"
        (exe.parent / "data" / "providers.json").write_text("{}", encoding="utf-8")
        assert clients.magpie_dir() == exe.parent / "data"

    def test_probe_sees_config_dir(self, sandbox):
        assert clients.magpie_installed() is False
        sandbox["magpie"].mkdir()
        assert clients.magpie_installed() is True

    def test_probe_sees_installed_exe(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ZCODE_MAGPIE_DIR", raising=False)
        monkeypatch.setenv("ZCODE_AGENT_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        monkeypatch.setattr(clients.shutil, "which", lambda _name: None)
        assert clients.magpie_installed() is False
        exe = tmp_path / "localappdata" / "Programs" / "magpie" / "magpie.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        assert clients.magpie_installed() is True


# ── 网关地址与 provider 条目 ───────────────────────────────────────────────────
class TestProviderEntry:
    def test_endpoints_normalized(self):
        root, v1 = clients.gateway_endpoints("http://127.0.0.1:3000/")
        assert root == "http://127.0.0.1:3000"
        assert v1 == "http://127.0.0.1:3000/v1"
        root, v1 = clients.gateway_endpoints("http://127.0.0.1:3000/v1")
        assert (root, v1) == ("http://127.0.0.1:3000", "http://127.0.0.1:3000/v1")

    def test_endpoints_reject_garbage(self):
        with pytest.raises(clients.AgentConfigError):
            clients.gateway_endpoints("")
        with pytest.raises(clients.AgentConfigError):
            clients.gateway_endpoints("127.0.0.1:3000")

    def test_entry_shape(self):
        entry = clients.magpie_provider(base_url="http://127.0.0.1:3000", api_key="sk-gw",
                                        models=["GLM-5.3-Flash"])
        assert entry == {
            "id": "zcode2api",
            "name": "zcode2api",
            "key": "sk-gw",
            "chat": "http://127.0.0.1:3000/v1",
            "anthropic": "http://127.0.0.1:3000",
            "models": ["GLM-5.3-Flash"],
        }

    def test_entry_defaults_models(self):
        entry = clients.magpie_provider(base_url="http://127.0.0.1:3000")
        assert entry["models"] == clients.default_models()
        assert entry["key"] == ""


# ── 计划（不落盘）──────────────────────────────────────────────────────────────
class TestPlan:
    def test_fresh_file(self, sandbox):
        files = clients.plan("magpie", base_url="http://127.0.0.1:3000", api_key="sk-gw",
                             models=["GLM-5.3-Flash", "GLM-5.3"])
        assert list(files) == [sandbox["providers"]]
        data = json.loads(files[sandbox["providers"]])
        assert [p["id"] for p in data["providers"]] == ["zcode2api"]
        assert data["providers"][0]["chat"].endswith("/v1")
        assert not sandbox["providers"].exists(), "plan 不该落盘"

    def test_preserves_other_content(self, sandbox):
        original = {
            "providers": [
                {"id": "deepseek", "name": "DeepSeek", "key": "sk-x", "chat": "https://api.deepseek.com/v1"},
            ],
            "groups": [{"id": "daily", "name": "daily", "models": ["deepseek/deepseek-v4-pro"]}],
            "order": ["deepseek"],
            "noAutoGroups": True,
            "pluginLeft": [{"id": "gone"}],
        }
        _write(sandbox["providers"], json.dumps(original, ensure_ascii=False))
        files = clients.plan("magpie", base_url="http://127.0.0.1:3000")
        data = json.loads(files[sandbox["providers"]])
        assert data["providers"][0] == original["providers"][0]
        assert data["providers"][-1]["id"] == "zcode2api"
        for key in ("groups", "order", "noAutoGroups", "pluginLeft"):
            assert data[key] == original[key]

    def test_updates_in_place_keeping_extra_keys(self, sandbox):
        original = {
            "providers": [
                {"id": "zcode2api", "name": "old name", "key": "old", "chat": "http://old/v1",
                 "proxy": "http://127.0.0.1:7890", "routing": "rotate", "models": ["old-model"]},
                {"id": "other", "name": "Other", "key": "k", "chat": "https://o/v1"},
            ],
        }
        _write(sandbox["providers"], json.dumps(original, ensure_ascii=False))
        files = clients.plan("magpie", base_url="http://127.0.0.1:3000", api_key="sk-new",
                             models=["GLM-5.3-Flash"])
        data = json.loads(files[sandbox["providers"]])
        assert len(data["providers"]) == 2, "同 id 应就地更新而不是追加"
        entry = data["providers"][0]
        assert entry["key"] == "sk-new"
        assert entry["name"] == "zcode2api"
        assert entry["models"] == ["GLM-5.3-Flash"]
        assert entry["proxy"] == "http://127.0.0.1:7890"
        assert entry["routing"] == "rotate"
        assert data["providers"][1] == original["providers"][1]

    def test_bom_tolerated(self, sandbox):
        _write(sandbox["providers"], b'\xef\xbb\xbf{"providers": []}')
        files = clients.plan("magpie", base_url="http://127.0.0.1:3000")
        data = json.loads(files[sandbox["providers"]])
        assert data["providers"][0]["id"] == "zcode2api"

    def test_empty_file_is_first_use(self, sandbox):
        _write(sandbox["providers"], "   \n")
        files = clients.plan("magpie", base_url="http://127.0.0.1:3000")
        assert json.loads(files[sandbox["providers"]])["providers"][0]["id"] == "zcode2api"

    def test_broken_json_refused(self, sandbox):
        _write(sandbox["providers"], "{not json")
        with pytest.raises(clients.AgentConfigError):
            clients.plan("magpie", base_url="http://127.0.0.1:3000")

    def test_non_object_refused(self, sandbox):
        _write(sandbox["providers"], "[]")
        with pytest.raises(clients.AgentConfigError):
            clients.plan("magpie", base_url="http://127.0.0.1:3000")

    def test_providers_not_list_refused(self, sandbox):
        _write(sandbox["providers"], '{"providers": {}}')
        with pytest.raises(clients.AgentConfigError):
            clients.plan("magpie", base_url="http://127.0.0.1:3000")

    def test_unknown_client(self, sandbox):
        with pytest.raises(clients.AgentConfigError):
            clients.plan("nope", base_url="http://127.0.0.1:3000")


# ── 接入 / 还原 ──────────────────────────────────────────────────────────────
class TestIntegrateRestore:
    def test_creates_file_with_key_and_backup_dir(self, sandbox):
        report = clients.integrate("magpie", base_url="http://127.0.0.1:3000", api_key="sk-gw")
        assert report["records"][0]["action"] == "created"
        data = _read_json(sandbox["providers"])
        assert data["providers"][0]["id"] == "zcode2api"
        assert data["providers"][0]["key"] == "sk-gw"
        if os.name != "nt":
            assert (sandbox["providers"].stat().st_mode & 0o777) == 0o600
        state = _read_json(Path(report["state_file"]))
        assert state["magpie"]["base_url"] == "http://127.0.0.1:3000"
        assert state["magpie"]["key_set"] is True

    def test_backs_up_existing_file(self, sandbox):
        original = '{"providers":[{"id":"x","name":"X","key":"k","chat":"https://x/v1"}]}'
        _write(sandbox["providers"], original)
        report = clients.integrate("magpie", base_url="http://127.0.0.1:3000")
        backup = Path(report["backup_dir"]) / report["records"][0]["backup"]
        assert backup.read_text(encoding="utf-8") == original
        assert report["records"][0]["action"] == "updated"

    def test_idempotent_second_run(self, sandbox):
        first = clients.integrate("magpie", base_url="http://127.0.0.1:3000")
        second = clients.integrate("magpie", base_url="http://127.0.0.1:3000")
        assert first["records"][0]["sha256"] == second["records"][0]["sha256"]
        assert second["records"][0]["action"] == "unchanged"
        # 内容没变就不该多出备份
        backups = list((Path(second["backup_dir"])).glob("*-providers.json"))
        assert len(backups) <= 1

    def test_reapply_keeps_first_backup_pointer(self, sandbox):
        original = '{"providers":[]}'
        _write(sandbox["providers"], original)
        first = clients.integrate("magpie", base_url="http://127.0.0.1:3000")
        clients.integrate("magpie", base_url="http://127.0.0.1:3000", api_key="sk-changed")
        restored = clients.restore("magpie")
        assert restored["restored"][0]["action"] == "restored"
        assert sandbox["providers"].read_text(encoding="utf-8") == original
        assert first["records"][0]["backup"]

    def test_dry_run_touches_nothing(self, sandbox):
        report = clients.integrate("magpie", base_url="http://127.0.0.1:3000", dry_run=True)
        assert report["dry_run"] is True
        assert str(sandbox["providers"]) in report["contents"]
        assert not sandbox["providers"].exists()
        assert not clients.state_path().exists()
        assert not clients.backups_root().exists()

    def test_restore_deletes_our_own_file(self, sandbox):
        clients.integrate("magpie", base_url="http://127.0.0.1:3000")
        result = clients.restore("magpie")
        assert result["restored"][0]["action"] == "deleted"
        assert not sandbox["providers"].exists()
        assert _read_json(clients.state_path()) == {}

    def test_restore_without_state(self, sandbox):
        result = clients.restore("magpie")
        assert result["restored"] == []
        assert "尚未接入" in result["note"]

    def test_restore_after_external_change_only_strips_our_provider(self, sandbox):
        clients.integrate("magpie", base_url="http://127.0.0.1:3000")
        data = _read_json(sandbox["providers"])
        data["providers"].append({"id": "user-added", "name": "User", "key": "k",
                                  "chat": "https://u/v1"})
        data["order"] = ["user-added"]
        _write(sandbox["providers"], json.dumps(data, ensure_ascii=False))
        result = clients.restore("magpie")
        assert result["restored"][0]["action"] == "provider-removed"
        after = _read_json(sandbox["providers"])
        assert [p["id"] for p in after["providers"]] == ["user-added"]
        assert after["order"] == ["user-added"]

    def test_restore_missing_file(self, sandbox):
        clients.integrate("magpie", base_url="http://127.0.0.1:3000")
        sandbox["providers"].unlink()
        result = clients.restore("magpie")
        assert result["restored"][0]["action"] == "missing"

    def test_restore_is_safe_when_backup_lost(self, sandbox):
        _write(sandbox["providers"], '{"providers":[]}')
        report = clients.integrate("magpie", base_url="http://127.0.0.1:3000")
        (Path(report["backup_dir"]) / report["records"][0]["backup"]).unlink()
        result = clients.restore("magpie")
        assert result["restored"][0]["action"] == "skipped"
        assert _read_json(sandbox["providers"])["providers"][0]["id"] == "zcode2api"


# ── 看板 / 导入链接 / CLI ────────────────────────────────────────────────────
class TestOverview:
    def test_reports_state(self, sandbox):
        sandbox["magpie"].mkdir()  # 配置目录存在 = 已安装
        data = clients.overview(base_url="http://127.0.0.1:3000", api_key="sk-gw")
        client = data["clients"][0]
        assert client["id"] == "magpie"
        assert client["installed"] is True          # ZCODE_MAGPIE_DIR 已设置
        assert client["configured"] is False
        assert client["external_change"] is False
        assert data["gateway"]["base_url"] == "http://127.0.0.1:3000"
        assert data["gateway"]["key_set"] is True
        assert client["import_link"].startswith("magpie://import?")

    def test_flags_external_change(self, sandbox):
        clients.integrate("magpie", base_url="http://127.0.0.1:3000")
        sandbox["providers"].write_text('{"providers":[]}', encoding="utf-8")
        client = clients.overview(base_url="http://127.0.0.1:3000")["clients"][0]
        assert client["external_change"] is True

    def test_import_link_encoding(self):
        link = clients.magpie_import_link(
            base_url="http://127.0.0.1:3000", api_key="sk-a b",
            models=["GLM-5.3-Flash", "GLM-5.3"],
        )
        assert link.startswith("magpie://import?")
        assert "chat=http%3A%2F%2F127.0.0.1%3A3000%2Fv1" in link
        assert "anthropic=http%3A%2F%2F127.0.0.1%3A3000" in link
        assert "models=GLM-5.3-Flash%2CGLM-5.3" in link
        assert "key=sk-a%20b" in link

    def test_import_link_omits_empty_key(self):
        link = clients.magpie_import_link(base_url="http://127.0.0.1:3000")
        assert "key=" not in link


class TestCli:
    def test_list(self, sandbox, capsys):
        assert clients.main(["list", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["id"] == "magpie"

    def test_apply_dry_run(self, sandbox, capsys):
        code = clients.main(["apply", "--client", "magpie", "--base-url",
                             "http://127.0.0.1:3000", "--dry-run", "--json"])
        assert code == 0
        report = json.loads(capsys.readouterr().out)
        assert report["dry_run"] is True
        assert not sandbox["providers"].exists()

    def test_apply_then_restore(self, sandbox, capsys):
        assert clients.main(["apply", "--client", "magpie", "--base-url",
                             "http://127.0.0.1:3000", "--key", "sk-gw", "--json"]) == 0
        capsys.readouterr()
        assert _read_json(sandbox["providers"])["providers"][0]["key"] == "sk-gw"
        assert clients.main(["restore", "--client", "magpie", "--json"]) == 0
        capsys.readouterr()
        assert not sandbox["providers"].exists()

    def test_unknown_client_exit_code(self, sandbox, capsys):
        assert clients.main(["apply", "--client", "nope", "--base-url",
                             "http://127.0.0.1:3000"]) == 1
        assert "不支持的客户端" in capsys.readouterr().err
