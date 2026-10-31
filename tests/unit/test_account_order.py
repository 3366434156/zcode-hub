"""账号人工排序：顺序落库 + 决定网关轮询次序。

后台拖拽保存的顺序不是纯展示字段——它同时是 `store.select` 的轮询序（第一行
= 网关最先用），所以必须重启后仍然生效。
"""

from __future__ import annotations

from app.store import Store


def _ids(store, provider="zai"):
    return [a.id for a in store.list_accounts(provider)]


def _names(store, provider="zai"):
    return [a.name for a in store.list_accounts(provider)]


class TestReorder:
    def test_reorder_sets_pool_order_selection_and_persists(self, fresh_app):
        a = fresh_app.add_account("zai", "a", "jwt.a.a")
        b = fresh_app.add_account("zai", "b", "jwt.b.b")
        c = fresh_app.add_account("zai", "c", "jwt.c.c")
        assert _names(fresh_app) == ["a", "b", "c"]

        applied = fresh_app.reorder_accounts([c.id, a.id, b.id])
        assert applied == [c.id, a.id, b.id]
        assert _names(fresh_app) == ["c", "a", "b"]

        # 轮询次序 = 人工顺序（第一行最先用）
        picked = [fresh_app.select("zai").name for _ in range(3)]
        assert picked == ["c", "a", "b"]

        # 落库：新 Store（模拟进程重启）读回来仍是人工顺序
        assert _names(Store()) == ["c", "a", "b"]

    def test_unlisted_accounts_keep_relative_order_at_tail(self, fresh_app):
        a = fresh_app.add_account("zai", "a", "jwt.a.a")
        b = fresh_app.add_account("zai", "b", "jwt.b.b")
        c = fresh_app.add_account("zai", "c", "jwt.c.c")
        # 只提交 a、c（前端过滤时漏传 b）：b 保留原相对位置排到末尾
        fresh_app.reorder_accounts([c.id, a.id])
        assert _ids(fresh_app) == [c.id, a.id, b.id]

    def test_new_account_goes_after_manual_order(self, fresh_app):
        a = fresh_app.add_account("zai", "a", "jwt.a.a")
        b = fresh_app.add_account("zai", "b", "jwt.b.b")
        fresh_app.reorder_accounts([b.id, a.id])
        c = fresh_app.add_account("zai", "c", "jwt.c.c")
        assert _ids(fresh_app) == [b.id, a.id, c.id]

    def test_unknown_ids_ignored_and_empty_is_noop(self, fresh_app):
        a = fresh_app.add_account("zai", "a", "jwt.a.a")
        b = fresh_app.add_account("zai", "b", "jwt.b.b")
        assert fresh_app.reorder_accounts([]) == []
        fresh_app.reorder_accounts(["no-such-id", b.id, a.id])
        assert _ids(fresh_app) == [b.id, a.id]

    def test_reorder_is_per_provider(self, fresh_app):
        z1 = fresh_app.add_account("zai", "z1", "jwt.z1.z1")
        z2 = fresh_app.add_account("zai", "z2", "jwt.z2.z2")
        m1 = fresh_app.add_account("bigmodel", "m1", "sk-m1")
        m2 = fresh_app.add_account("bigmodel", "m2", "sk-m2")
        fresh_app.reorder_accounts([z2.id, z1.id, m2.id, m1.id])
        assert _ids(fresh_app, "zai") == [z2.id, z1.id]
        assert _ids(fresh_app, "bigmodel") == [m2.id, m1.id]
