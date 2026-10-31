"""客户端一键接入的后台 API（/admin/api/agents*）集成测试。

覆盖：鉴权、看板字段、接入写入、还原、坏文件拒绝、非法客户端。
全部跑在 tmp 目录（ZCODE_MAGPIE_DIR）里，不碰真实 magpie 配置。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ADMIN = {"Authorization": "Bearer zcode"}


@pytest.fixture
def magpie_dir(tmp_path, monkeypatch):
    directory = tmp_path / "magpie"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ZCODE_MAGPIE_DIR", str(directory))

    from app import clients

    monkeypatch.setitem(clients.CLIENTS["magpie"], "running", lambda: False)
    return directory


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.integration
class TestAgentsApi:
    async def test_requires_admin_key(self, gateway_client, magpie_dir):
        client, _ = gateway_client
        res = await client.get("/admin/api/agents")
        assert res.status_code == 401
        res = await client.post("/admin/api/agents/apply", json={"client": "magpie"})
        assert res.status_code == 401

    async def test_overview_lists_magpie(self, gateway_client, magpie_dir):
        client, _ = gateway_client
        res = await client.get("/admin/api/agents", headers=ADMIN)
        assert res.status_code == 200
        data = res.json()
        assert [c["id"] for c in data["clients"]] == ["magpie"]
        magpie = data["clients"][0]
        assert magpie["installed"] is True
        assert magpie["configured"] is False
        assert magpie["write_target"] == str(magpie_dir / "providers.json")
        assert magpie["import_link"].startswith("magpie://import?")
        assert data["models"]
        assert data["gateway"]["base_url"]

    async def test_apply_writes_provider_then_restore(self, gateway_client, magpie_dir):
        client, _ = gateway_client
        res = await client.post("/admin/api/agents/apply", json={"client": "magpie"}, headers=ADMIN)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ok"] is True
        assert body["records"][0]["action"] == "created"

        providers = _read_json(magpie_dir / "providers.json")
        entry = providers["providers"][0]
        assert entry["id"] == "zcode2api"
        assert entry["chat"].endswith("/v1")
        assert entry["anthropic"].startswith("http://")

        from app import constants

        assert entry["models"] == list(constants.AVAILABLE_MODELS)

        # 看板应显示已接入
        overview = (await client.get("/admin/api/agents", headers=ADMIN)).json()
        magpie = overview["clients"][0]
        assert magpie["configured"] is True
        assert magpie["applied"]["files"] == 1

        restore = await client.post("/admin/api/agents/restore", json={"client": "magpie"},
                                   headers=ADMIN)
        assert restore.status_code == 200, restore.text
        assert restore.json()["restored"][0]["action"] == "deleted"
        assert not (magpie_dir / "providers.json").exists()

    async def test_apply_preserves_existing_providers(self, gateway_client, magpie_dir):
        magpie_dir.mkdir(parents=True, exist_ok=True)
        original = {"providers": [{"id": "deepseek", "name": "DeepSeek",
                                  "key": "sk-x", "chat": "https://api.deepseek.com/v1"}],
                    "order": ["deepseek"]}
        (magpie_dir / "providers.json").write_text(json.dumps(original), encoding="utf-8")

        client, _ = gateway_client
        res = await client.post("/admin/api/agents/apply", json={"client": "magpie"}, headers=ADMIN)
        assert res.status_code == 200, res.text
        data = _read_json(magpie_dir / "providers.json")
        ids = [p["id"] for p in data["providers"]]
        assert ids == ["deepseek", "zcode2api"]
        assert data["order"] == ["deepseek"]

    async def test_apply_refuses_broken_file(self, gateway_client, magpie_dir):
        magpie_dir.mkdir(parents=True, exist_ok=True)
        target = magpie_dir / "providers.json"
        target.write_text("{broken", encoding="utf-8")

        client, _ = gateway_client
        res = await client.post("/admin/api/agents/apply", json={"client": "magpie"}, headers=ADMIN)
        assert res.status_code == 400
        assert "JSON" in res.json()["detail"]
        assert target.read_text(encoding="utf-8") == "{broken"

    async def test_apply_unknown_client(self, gateway_client, magpie_dir):
        client, _ = gateway_client
        res = await client.post("/admin/api/agents/apply", json={"client": "nope"}, headers=ADMIN)
        assert res.status_code == 400
        assert "不支持的客户端" in res.json()["detail"]

    async def test_apply_rejects_bad_models_type(self, gateway_client, magpie_dir):
        client, _ = gateway_client
        res = await client.post("/admin/api/agents/apply",
                                json={"client": "magpie", "models": "GLM-5.3"},
                                headers=ADMIN)
        assert res.status_code == 400

    async def test_apply_uses_custom_models_and_base_url(self, gateway_client, magpie_dir):
        client, _ = gateway_client
        res = await client.post(
            "/admin/api/agents/apply",
            json={"client": "magpie", "base_url": "http://192.168.1.9:3000", "models": ["glm-5.3-flash"]},
            headers=ADMIN,
        )
        assert res.status_code == 200, res.text
        entry = _read_json(magpie_dir / "providers.json")["providers"][0]
        assert entry["chat"] == "http://192.168.1.9:3000/v1"
        assert entry["anthropic"] == "http://192.168.1.9:3000"
        assert entry["models"] == ["glm-5.3-flash"]

    async def test_restore_without_apply_is_noop(self, gateway_client, magpie_dir):
        client, _ = gateway_client
        res = await client.post("/admin/api/agents/restore", json={"client": "magpie"},
                                headers=ADMIN)
        assert res.status_code == 200
        assert res.json()["restored"] == []

    async def test_apply_records_gateway_key(self, gateway_client, magpie_dir, fresh_app):
        fresh_app.set_setting("gateway_key", "sk-gw-secret")
        client, _ = gateway_client
        res = await client.post("/admin/api/agents/apply", json={"client": "magpie"}, headers=ADMIN)
        assert res.status_code == 200, res.text
        entry = _read_json(magpie_dir / "providers.json")["providers"][0]
        assert entry["key"] == "sk-gw-secret"
        # 看板不能回显明文 key
        overview = (await client.get("/admin/api/agents", headers=ADMIN)).json()
        assert "sk-gw-secret" not in json.dumps(overview["clients"][0]["applied"])


@pytest.mark.integration
class TestRemoteShareApi:
    async def test_overview_carries_remote_plan(self, gateway_client, magpie_dir, monkeypatch):
        from app import clients

        monkeypatch.setattr(clients, "lan_ipv4", lambda: ["192.168.1.50"])
        (magpie_dir / "settings.json").write_text('{"lan": true}', encoding="utf-8")
        (magpie_dir / "caller-keys.json").write_text(json.dumps([
            {"id": "k1", "name": "zcode-hub", "secret": "sk-magpie-key-abc"},
        ]), encoding="utf-8")

        client, _ = gateway_client
        data = (await client.get("/admin/api/agents", headers=ADMIN)).json()
        remote = data["remote"]
        assert remote["lan_ips"] == ["192.168.1.50"]
        assert remote["gateway"]["lan_url"].endswith(":3000")
        assert remote["gateway"]["direct_command"].startswith("magpie provider add zcode2api")
        assert remote["magpie"]["sharing"] is True
        assert "magpie provider add remote-magpie sk-magpie-key-abc" in remote["magpie"]["remote_command"]
        assert remote["magpie"]["remote_url"] == "http://192.168.1.50:3425"

    async def test_share_endpoint_enables_and_creates_key(self, gateway_client, magpie_dir, monkeypatch):
        from app import clients

        monkeypatch.setattr(clients, "lan_ipv4", lambda: ["192.168.1.50"])
        (magpie_dir / "settings.json").write_text('{"theme": "system"}', encoding="utf-8")
        (magpie_dir / "caller-keys.json").write_text("[]", encoding="utf-8")

        class _Proc:
            stdout = "sk-magpie-key-fresh\n"
            stderr = ""
            returncode = 0

        def fake_run(args, **kwargs):
            (magpie_dir / "caller-keys.json").write_text(json.dumps([
                {"id": "new-id", "name": "zcode-hub", "secret": "sk-magpie-key-fresh"},
            ]), encoding="utf-8")
            return _Proc()

        monkeypatch.setattr(clients, "magpie_cli_path", lambda: "magpie")
        monkeypatch.setattr(clients.subprocess, "run", fake_run)

        client, _ = gateway_client
        res = await client.post("/admin/api/agents/magpie/share", headers=ADMIN)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["sharing"]["changed"] is True
        assert body["key"]["secret"] == "sk-magpie-key-fresh"
        assert body["key"]["created"] is True
        assert "remote-magpie sk-magpie-key-fresh" in body["magpie"]["remote_command"]
        # settings.json 被补上 lan，其它字段保留
        settings_data = _read_json(magpie_dir / "settings.json")
        assert settings_data["lan"] is True and settings_data["theme"] == "system"

    async def test_share_without_cli_and_without_key_is_400(self, gateway_client, magpie_dir, monkeypatch):
        from app import clients

        (magpie_dir / "settings.json").write_text("{}", encoding="utf-8")
        (magpie_dir / "caller-keys.json").write_text("[]", encoding="utf-8")
        monkeypatch.setattr(clients, "magpie_cli_path", lambda: None)
        client, _ = gateway_client
        res = await client.post("/admin/api/agents/magpie/share", headers=ADMIN)
        assert res.status_code == 400
        assert "Gateway keys" in res.json()["detail"]

    async def test_generate_gateway_key_updates_configured_client(
        self, gateway_client, magpie_dir, fresh_app
    ):
        client, _ = gateway_client
        # 先接入（此时网关 Key 为空）
        assert (await client.post("/admin/api/agents/apply", json={"client": "magpie"},
                                  headers=ADMIN)).status_code == 200
        assert _read_json(magpie_dir / "providers.json")["providers"][0]["key"] == ""

        res = await client.post("/admin/api/agents/gateway-key", headers=ADMIN)
        assert res.status_code == 200, res.text
        token = res.json()["gateway_key"]
        assert token.startswith("sk-zcode-")
        assert fresh_app.gateway_key() == token
        # 已接入的客户端就地换成新 Key，避免本机自己 401
        entry = _read_json(magpie_dir / "providers.json")["providers"][0]
        assert entry["key"] == token
        assert res.json()["refreshed_clients"] == ["magpie"]
