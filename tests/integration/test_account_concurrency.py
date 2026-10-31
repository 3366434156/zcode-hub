"""账号并发限制集成测试（2026-09-13）：单账号默认 1，可配置，0 = 不限。

默认 1 的原因：上游对「账号 × 模型」并发硬限很低，超了直接回
`429 {"code":3009,"msg":"model concurrency limit exceeded"}`（2026-10-27 实测），
所以网关自己一账号一路、满号跳下一个，并发能力 = 账号数。

2026-10-28 起「所有号都满」不再立刻 503，而是排队等任一槽释放
（QUEUE_WAIT_MAX，0 = 恢复旧行为）；排队超时 / 排队已满才 503。

依赖 mock 上游 slow 场景（0.3s 延迟）制造重叠；用 conc_max 仪表（按凭证）
断言单账号同时刻在飞请求数不超过限制。
"""

from __future__ import annotations

import asyncio

import pytest

from app import settings

_MSG_BODY = {"model": "GLM-5.2", "messages": [{"role": "user", "content": "hi"}]}


def _seed(store, secret: str, name: str):
    return store.add_account("zai", name, secret)


def _per_key_max(mock, key: str) -> int:
    """某凭证观测到的最大同时刻在飞 messages 请求数。"""
    return (getattr(mock.state, "conc_max", {}) or {}).get(key, 0)


@pytest.mark.integration
class TestAccountConcurrency:
    @pytest.fixture(autouse=True)
    def _slow_upstream(self, gateway_client):
        """并发用例把默认场景替换为 slow（0.3s 延迟，制造重叠）；仪表逐用例清零。

        mock 上游是会话级实例：slow_all / conc_* 状态跨用例残留，进出各清一次。
        排队等待者也是模块级状态：用例结束必须清空，否则残留的 future 会被
        下一个用例的槽释放唤醒。
        """
        from app.routes import gateway as gw

        _client, mock = gateway_client
        mock.state.slow_all = True
        mock.state.conc_in = {}
        mock.state.conc_max = {}
        gw._slot_waiters.clear()
        yield
        mock.state.slow_all = False
        mock.state.conc_in = {}
        mock.state.conc_max = {}
        gw._slot_waiters.clear()

    async def test_single_account_limits_to_two(self, gateway_client, fresh_app):
        """单号上限 2、4 并发：全部成功（后两个排队等槽），单号 max_concurrent ≤ 2。"""
        client, mock = gateway_client
        fresh_app.set_setting("account_concurrency", "2")
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-cc")

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(4)]
        statuses = sorted(t.result().status_code for t in tasks)
        assert statuses == [200, 200, 200, 200]
        assert _per_key_max(mock, acc.jwt_token[:16]) <= 2

    async def test_two_accounts_all_succeed(self, gateway_client, fresh_app):
        """双账号、每号上限 2、4 并发：每号 ≤2，全部成功。"""
        client, mock = gateway_client
        fresh_app.set_setting("account_concurrency", "2")
        a = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-cc1")
        b = _seed(fresh_app, "hD.eyJzdWIiOiJkIn0.sig", "b-cc2")

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(4)]
        statuses = [t.result().status_code for t in tasks]
        assert statuses == [200, 200, 200, 200]
        assert _per_key_max(mock, a.jwt_token[:16]) <= 2
        assert _per_key_max(mock, b.jwt_token[:16]) <= 2

    async def test_full_account_skips_to_next(self, gateway_client, fresh_app):
        """A 号满 2 → 调度跳到 B 号，第 3 个请求不排队也不 503。"""
        client, _mock = gateway_client
        fresh_app.set_setting("account_concurrency", "2")
        _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-full")
        _seed(fresh_app, "hD.eyJzdWIiOiJkIn0.sig", "b-full")

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(3)]
        statuses = [t.result().status_code for t in tasks]
        assert statuses == [200, 200, 200]

    async def test_zero_limit_unbounded(self, gateway_client, fresh_app):
        """limit=0：并发不封顶（4 请求同飞全 200，单号 max_concurrent > 2）。"""
        client, mock = gateway_client
        fresh_app.set_setting("account_concurrency", "0")
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-zero")

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(4)]
        statuses = [t.result().status_code for t in tasks]
        assert statuses == [200, 200, 200, 200]
        assert _per_key_max(mock, acc.jwt_token[:16]) > 2

    async def test_runtime_setting_changes_limit(self, gateway_client, fresh_app):
        """后台改 limit=1 即生效：第二路排队等第一路放槽，单号 max_concurrent ≤ 1。"""
        client, mock = gateway_client
        fresh_app.set_setting("account_concurrency", "1")
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-one")

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(2)]
        statuses = [t.result().status_code for t in tasks]
        assert statuses == [200, 200]
        assert _per_key_max(mock, acc.jwt_token[:16]) <= 1

    async def test_full_pool_queues_instead_of_503(self, gateway_client, fresh_app):
        """单号 limit=1、3 并发：第 2、3 路排队等槽，不再直接 503。"""
        from app.routes import gateway as gw

        client, mock = gateway_client
        fresh_app.set_setting("account_concurrency", "1")
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-queue")
        gw._slot_waiters.clear()

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.post("/v1/messages", json=_MSG_BODY))
                     for _ in range(3)]
        statuses = [t.result().status_code for t in tasks]
        assert statuses == [200, 200, 200]
        assert _per_key_max(mock, acc.jwt_token[:16]) <= 1
        assert not gw._slot_waiters

    async def test_sticky_account_waits_instead_of_switching(self, gateway_client, fresh_app):
        """粘号在忙时**排队等它**，而不是把请求赶到另一个号（「正常就一直用一个账号」）。

        场景：全局粘号钉在 A 上，A 的并发槽被占满，B 有空。老行为是把请求发给 B
        （后台看起来一直在跳号、上游缓存也换一份）；新行为是等 A 空出来。
        """
        from app import affinity
        from app.routes import gateway as gw

        client, mock = gateway_client
        fresh_app.set_setting("account_concurrency", "1")
        fresh_app.set_setting("account_sticky", "1")
        # 会话粘性优先级高于全局粘号，而 affinity 是模块级状态、会跨用例残留
        #（_MSG_BODY 相同 → 同一个 conv_key）：不复位就会被上一个用例钉住的号覆盖。
        affinity.reset()
        a = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-sticky")
        b = _seed(fresh_app, "hD.eyJzdWIiOiJkIn0.sig", "b-sticky")
        # A 被钉为全局粘号，且假装它有一路在飞（槽满）；B 完全空闲
        fresh_app.set_sticky_account("zai", a.id)
        gw._inflight[a.id] = 1
        gw._slot_waiters.clear()
        # 前置条件：粘号候选必须真的是 A（不成立的话这条用例测的就不是粘号等待了）
        assert gw._sticky_prefer("zai") == a.id, (
            f"sticky_account={fresh_app.sticky_account('zai')!r} a.id={a.id!r} "
            f"enabled={fresh_app.account_sticky_enabled()}")

        # 先让上一个用例的残留请求全部落地，再记下调用序列的起点：
        # 否则「B 没被用过」会被别人的尾巴打成假失败（mock 是会话级共享的）。
        await asyncio.sleep(0.6)
        base = len(mock.state.calls)

        def _creds() -> set[str]:
            return {(h.get("authorization") or h.get("x-api-key") or "")
                    for _m, p, h, _b in mock.state.calls[base:] if p.endswith("/v1/messages")}

        try:
            task = asyncio.create_task(client.post("/v1/messages", json=_MSG_BODY))
            await asyncio.sleep(0.1)
            assert task.done() is False, "粘号满时应当排队等待，而不是立刻换号成功"
            assert b.jwt_token[:16] not in " ".join(_creds()), "不该把请求发给另一个账号"
            gw._release_slot(a.id)        # 粘号空出来 → 立刻唤醒
            resp = await asyncio.wait_for(task, timeout=5)
            assert resp.status_code == 200, resp.text
            # 最终服务的必须是粘号 A（上游收到的凭证里只有 A）
            auths = " ".join(_creds())
            assert a.jwt_token[:16] in auths, auths
            assert b.jwt_token[:16] not in auths, auths
        finally:
            gw._inflight.pop(a.id, None)
            gw._slot_waiters.clear()

    async def test_queue_wakes_when_slot_released(self, gateway_client, fresh_app):
        """排队中的请求在槽释放那一刻放行（不靠轮询满 timeout 才重扫）。"""
        from app.routes import gateway as gw

        client, mock = gateway_client
        mock.state.slow_all = False
        fresh_app.set_setting("account_concurrency", "1")
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-wake")
        gw._inflight[acc.id] = 1          # 假装该号有一路在飞
        gw._slot_waiters.clear()
        try:
            task = asyncio.create_task(client.post("/v1/messages", json=_MSG_BODY))
            await asyncio.sleep(0.1)      # 让它进入排队
            assert task.done() is False
            gw._release_slot(acc.id)      # 槽释放 → 立即唤醒
            resp = await asyncio.wait_for(task, timeout=5)
            assert resp.status_code == 200, resp.text
        finally:
            gw._inflight.pop(acc.id, None)
            gw._slot_waiters.clear()

    async def test_queue_timeout_returns_503(self, gateway_client, fresh_app, monkeypatch):
        """排队等满 QUEUE_WAIT_MAX 仍无空位 → 503，原因写明「排队超时」。"""
        from app.routes import gateway as gw

        client, _mock = gateway_client
        fresh_app.set_setting("account_concurrency", "1")
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-timeout")
        monkeypatch.setattr(settings, "QUEUE_WAIT_MAX", 0.2)
        gw._inflight[acc.id] = 1
        gw._slot_waiters.clear()
        try:
            resp = await client.post("/v1/messages", json=_MSG_BODY)
            assert resp.status_code == 503
            assert "排队" in resp.text
        finally:
            gw._inflight.pop(acc.id, None)
            gw._slot_waiters.clear()

    async def test_queue_disabled_keeps_503(self, gateway_client, fresh_app, monkeypatch):
        """QUEUE_WAIT_MAX=0：恢复旧行为，满号立即 503（不等）。"""
        from app.routes import gateway as gw

        client, _mock = gateway_client
        fresh_app.set_setting("account_concurrency", "1")
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-off")
        monkeypatch.setattr(settings, "QUEUE_WAIT_MAX", 0)
        gw._inflight[acc.id] = 1
        try:
            resp = await client.post("/v1/messages", json=_MSG_BODY)
            assert resp.status_code == 503
            assert "排队" not in resp.text
        finally:
            gw._inflight.pop(acc.id, None)

    async def test_queue_full_returns_503(self, gateway_client, fresh_app, monkeypatch):
        """排队人数到 QUEUE_MAX_WAITERS 上限 → 立即 503「排队已满」，不再堆连接。"""
        from app.routes import gateway as gw

        client, _mock = gateway_client
        fresh_app.set_setting("account_concurrency", "1")
        acc = _seed(fresh_app, "hC.eyJzdWIiOiJjIn0.sig", "a-cap")
        monkeypatch.setattr(settings, "QUEUE_MAX_WAITERS", 0)
        gw._inflight[acc.id] = 1
        gw._slot_waiters.clear()
        try:
            resp = await client.post("/v1/messages", json=_MSG_BODY)
            assert resp.status_code == 503
            assert "排队已满" in resp.text
        finally:
            gw._inflight.pop(acc.id, None)

    def test_env_default_is_one(self):
        """默认一账号一路（上游 3009 模型并发硬限）。"""
        assert settings.ACCOUNT_CONCURRENCY == 1

    async def test_429_wait_does_not_hold_concurrency_slot(
        self, gateway_client, fresh_app, monkeypatch
    ):
        """429 等待不得占住并发槽：一号 429 睡着时，第二路请求仍能占用该号成功。"""
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        client, mock = gateway_client
        mock.state.slow_all = False
        fresh_app.set_setting("account_concurrency", "1")
        monkeypatch.setattr(settings, "RETRY_429_TIMES", 1)
        monkeypatch.setattr(settings, "RETRY_429_WAIT", 60)
        monkeypatch.setattr(gw, "_parse_retry_after", lambda _: None)

        gate = asyncio.Event()
        entered = asyncio.Event()

        real_sleep = gw._sleep

        async def gated_sleep(seconds: float):
            if seconds and seconds > 0:
                entered.set()
                await gate.wait()
                return
            await real_sleep(seconds)

        monkeypatch.setattr(gw, "_sleep", gated_sleep)
        jwt = "hQ.eyJzdWIiOiJxIn0.sig"
        seed_account(fresh_app, jwt, name="a-slot")
        mock.state.sequences[jwt[:16]] = ["rate_limited", "ok"]

        first = asyncio.create_task(client.post("/v1/messages", json=_MSG_BODY))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = await client.post("/v1/messages", json=_MSG_BODY)
        assert second.status_code == 200, second.text
        gate.set()
        first_res = await first
        assert first_res.status_code == 200

    async def test_skip_full_does_not_consume_account_attempts(
        self, gateway_client, fresh_app
    ):
        """满号跳过不得计入 MAX_ACCOUNT_ATTEMPTS。

        6 号 limit=1、前 5 号已占满时，round-robin 起点 2 会让当前实现
        连续抽到 5 个满号就 503，永远抽不到空闲的第 6 号（2026-09 review P2）。
        """
        from app.routes import gateway as gw

        client, mock = gateway_client
        mock.state.slow_all = False
        fresh_app.set_setting("account_concurrency", "1")
        accounts = [
            _seed(fresh_app, f"h{i}.eyJzdWIiOiJjIn0.sig", f"a-skip-{i}")
            for i in range(6)
        ]
        for acc in accounts[:5]:
            gw._inflight[acc.id] = 1
        fresh_app._rotation["zai"] = 2
        try:
            resp = await client.post("/v1/messages", json=_MSG_BODY)
            assert resp.status_code == 200, resp.text
        finally:
            for acc in accounts[:5]:
                gw._inflight.pop(acc.id, None)
