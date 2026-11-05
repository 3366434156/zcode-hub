"""续派工单页：zburn 队列 API + supervisor 白名单/插件状态（合并后的联调）。"""

from __future__ import annotations

import pytest

ADMIN_AUTH = {"Authorization": "Bearer zcode"}


@pytest.fixture()
def burner_env(tmp_path, monkeypatch):
    """把 burner 与 supervisor 的文件协议全部隔离到 tmp_path。"""
    from app import burner
    from app.routes import admin_api

    sup = tmp_path / "_sup"
    sup.mkdir()
    monkeypatch.setattr(burner, "SUP_DIR", sup)
    monkeypatch.setattr(burner, "DIR", tmp_path / "burner")
    monkeypatch.setattr(burner, "QUEUE_FILE", tmp_path / "burner" / "queue.json")
    monkeypatch.setattr(burner, "STATE_FILE", tmp_path / "burner" / "state.json")
    monkeypatch.setattr(burner, "PROMPT_FILE", sup / "next-prompt.txt")
    monkeypatch.setattr(burner, "ENABLE_FLAG", sup / "continue.enabled")
    monkeypatch.setattr(burner, "SESSIONS_FILE", sup / "continue.sessions")
    monkeypatch.setattr(burner, "AUTO_TASK_FILE", sup / "auto-task.txt")
    monkeypatch.setattr(burner, "AUTO_HANDOFF_FILE", sup / "auto-handoff.md")
    monkeypatch.setattr(admin_api, "_SUP_DIR", str(sup))
    return sup


@pytest.mark.integration
class TestSupervisorPage:
    async def test_page_renders(self, gateway_client):
        client, _ = gateway_client
        res = await client.get("/admin/supervisor")
        assert res.status_code == 200
        assert "续派工单" in res.text

    async def test_status_reports_last_decision(self, gateway_client, burner_env):
        client, _ = gateway_client
        (burner_env / "hook-state.json").write_text(
            '{"count": 3, "lastAt": 123, "last": {"decision": "skipped",'
            ' "reason": "监听名单为空：未勾选任何会话，不续派"}}', encoding="utf-8")
        res = await client.get("/admin/api/supervisor", headers=ADMIN_AUTH)
        body = res.json()
        assert body["count"] == 3
        assert body["last"]["decision"] == "skipped"
        assert "监听名单为空" in body["last"]["reason"]
        assert "loop" not in body and "projects" not in body, "循环/项目白名单已移除"

    async def test_hooklog_returns_tail(self, gateway_client, burner_env):
        client, _ = gateway_client
        (burner_env / "hook.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
        res = await client.get("/admin/api/supervisor/hooklog?lines=2", headers=ADMIN_AUTH)
        assert res.json()["lines"] == ["line2", "line3"]


@pytest.mark.integration
class TestBurnerApi:
    async def test_enqueue_and_dispatch(self, gateway_client, burner_env):
        client, _ = gateway_client
        res = await client.get("/admin/api/burner/state", headers=ADMIN_AUTH)
        assert res.status_code == 200
        assert res.json()["enabled"] is False, "总闸未开时队列只读"

        res = await client.post("/admin/api/burner/enqueue", headers=ADMIN_AUTH,
                                json={"title": "测试工单", "prompt": "做完停下"})
        assert res.status_code == 200
        order_id = res.json()["order"]["id"]

        # 总闸关闭：立即派单不写文件
        res = await client.post("/admin/api/burner/dispatch-now", headers=ADMIN_AUTH, json={})
        assert res.json()["action"] == "idle"
        assert not (burner_env / "next-prompt.txt").exists()

        (burner_env / "continue.enabled").write_text("", encoding="utf-8")
        # 监听名单为空：即使总闸开了也不派单（钩子不会消费）
        res = await client.post("/admin/api/burner/dispatch-now", headers=ADMIN_AUTH, json={})
        assert res.json()["action"] == "idle"
        assert "没有绑定的监听会话" in res.json()["reason"]
        assert not (burner_env / "next-prompt.txt").exists()

        (burner_env / "continue.sessions").write_text("s1\n", encoding="utf-8")
        res = await client.post("/admin/api/burner/dispatch-now", headers=ADMIN_AUTH, json={})
        body = res.json()
        assert body["action"] == "dispatched" and body["id"] == order_id
        assert (burner_env / "next-prompt.txt").read_text(
            encoding="utf-8").strip() == "做完停下"

        # 取消在途工单：待发指令被撤回
        res = await client.post(f"/admin/api/burner/order/{order_id}/cancel",
                                headers=ADMIN_AUTH, json={})
        assert res.json()["order"]["status"] == "cancelled"
        assert not (burner_env / "next-prompt.txt").exists()

    async def test_enqueue_validation(self, gateway_client, burner_env):
        client, _ = gateway_client
        res = await client.post("/admin/api/burner/enqueue", headers=ADMIN_AUTH,
                                json={"title": "", "prompt": "x"})
        assert res.status_code == 400

    async def test_auto_config_and_task(self, gateway_client, burner_env):
        client, _ = gateway_client
        res = await client.post("/admin/api/burner/auto", headers=ADMIN_AUTH,
                                json={"enabled": True, "minRemaining": 5_000_000,
                                      "maxRounds": 3})
        auto = res.json()["auto"]
        assert auto["enabled"] is True
        assert auto["minRemaining"] == 5_000_000
        assert auto["maxRounds"] == 3
        assert (burner_env / "auto.enabled").exists()

        res = await client.post("/admin/api/burner/auto/task", headers=ADMIN_AUTH,
                                json={"text": "把文档收口"})
        assert res.json()["auto"]["task"] == "把文档收口"
        assert (burner_env / "auto-task.txt").read_text(
            encoding="utf-8").strip() == "把文档收口"

        state = (await client.get("/admin/api/burner/state",
                                  headers=ADMIN_AUTH)).json()
        assert state["auto"]["task"] == "把文档收口"
        assert state["auto"]["paused"] is False

        res = await client.post("/admin/api/burner/auto/reset", headers=ADMIN_AUTH, json={})
        assert res.json()["auto"]["rounds"] == 0


@pytest.mark.integration
async def test_shutdown_sets_uvicorn_should_exit(gateway_client, monkeypatch):
    """优雅停服端点：只置位 should_exit，不直接杀进程。"""
    from app import runtime

    class _Server:
        should_exit = False

    stub = _Server()
    monkeypatch.setattr(runtime, "server", stub)
    client, _ = gateway_client
    res = await client.post("/admin/api/shutdown", headers=ADMIN_AUTH)
    assert res.status_code == 200
    assert res.json()["draining"] is True
    assert stub.should_exit is True


@pytest.mark.integration
class TestSessionMonitor:
    async def test_sessions_api_lists_zcode_sessions(self, gateway_client, burner_env,
                                                     tmp_path, monkeypatch):
        import sqlite3

        from app import zsession

        db = tmp_path / "db.sqlite"
        con = sqlite3.connect(db)
        con.executescript(
            """
            create table session (id text primary key, title text, directory text,
              time_created integer, time_updated integer, last_outcome text, time_deleted integer);
            create table message (id text primary key, session_id text, time_created integer);
            """
        )
        con.execute("insert into session values ('s1','对话一','E:/p',0,2000,'completed',null)")
        con.execute("insert into session values ('s2','对话二','E:/p',0,1000,'',null)")
        con.execute("insert into session values ('s3','已删','E:/p',0,3000,'',1)")
        con.execute("insert into message values ('m1','s1',0)")
        con.commit()
        con.close()
        monkeypatch.setattr(zsession, "DB_PATH", db)

        client, _ = gateway_client
        res = await client.get("/admin/api/sessions?limit=10", headers=ADMIN_AUTH)
        data = res.json()
        assert data["error"] is None
        assert [s["id"] for s in data["sessions"]] == ["s1", "s2"], "按更新倒序且跳过已删除"
        assert data["sessions"][0]["messages"] == 1

    async def test_monitor_list_roundtrip(self, gateway_client, burner_env):
        client, _ = gateway_client
        res = await client.post("/admin/api/supervisor/sessions", headers=ADMIN_AUTH,
                                json={"sessions": ["s1", "s2", "s1"]})
        assert res.json()["sessions"] == ["s1", "s2"], "去重"
        got = await client.get("/admin/api/supervisor", headers=ADMIN_AUTH)
        assert got.json()["sessions"] == ["s1", "s2"]
        assert (burner_env / "continue.sessions").read_text(encoding="utf-8").split() == ["s1", "s2"]

    async def test_sessions_api_reports_missing_db(self, gateway_client, burner_env,
                                                   tmp_path, monkeypatch):
        from app import zsession

        monkeypatch.setattr(zsession, "DB_PATH", tmp_path / "nope.sqlite")
        client, _ = gateway_client
        res = await client.get("/admin/api/sessions", headers=ADMIN_AUTH)
        assert res.status_code == 200
        assert res.json()["sessions"] == []
        assert "不存在" in res.json()["error"]
