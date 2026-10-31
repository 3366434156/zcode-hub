"""账号排序 API：拖拽顺序落库 = 网关轮询优先序。

覆盖：正常重排 / 缺 ids / 未知 id / 重启后仍是新顺序（顺序最终由 store 落库，
这里断 HTTP 口径与池内次序）。
"""

from __future__ import annotations

import pytest

from app.store import Store

ADMIN = {"Authorization": "Bearer zcode"}


@pytest.mark.integration
class TestAccountsReorderApi:
    @pytest.fixture
    def three(self, gateway_client, fresh_app):
        client, _mock = gateway_client
        accs = [fresh_app.add_account("zai", name, f"jwt.{name}.sig")
                for name in ("a", "b", "c")]
        return client, fresh_app, accs

    async def test_reorder_returns_applied_ids_and_updates_pool(self, three):
        client, store, (a, b, c) = three
        res = await client.post("/admin/api/accounts/reorder",
                                json={"ids": [c.id, a.id, b.id]}, headers=ADMIN)
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["ids"] == [c.id, a.id, b.id]

        listed = [x["name"] for x in
                  (await client.get("/admin/api/accounts", headers=ADMIN)).json()["accounts"]
                  if x["id"] in {a.id, b.id, c.id}]
        assert listed == ["c", "a", "b"]
        # 轮询次序跟着变（第一行最先用）
        assert [store.select("zai").name for _ in range(3)] == ["c", "a", "b"]
        # 落库：重启（新 Store 实例读同一 DB）后顺序不变
        assert [x.name for x in Store().list_accounts("zai")] == ["c", "a", "b"]

    async def test_reorder_requires_ids(self, three):
        client, _store, _accs = three
        res = await client.post("/admin/api/accounts/reorder", json={}, headers=ADMIN)
        assert res.status_code == 400
        res = await client.post("/admin/api/accounts/reorder", json={"ids": []}, headers=ADMIN)
        assert res.status_code == 400

    async def test_reorder_unknown_ids_404(self, three):
        client, _store, _accs = three
        res = await client.post("/admin/api/accounts/reorder",
                                json={"ids": ["no-such-account"]}, headers=ADMIN)
        assert res.status_code == 404

    async def test_reorder_requires_admin_key(self, three):
        client, _store, _accs = three
        res = await client.post("/admin/api/accounts/reorder", json={"ids": ["x"]})
        assert res.status_code == 401
