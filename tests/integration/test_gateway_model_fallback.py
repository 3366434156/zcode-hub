"""模型兜底（自动换模型）集成测试 —— 走 mock 上游，钉住「什么时候换模型、什么时候绝不换」。

额度是按「账号 × 模型」计的：4 个账号的 GLM-5.3 都用完时，池子里的 GLM-5.3-Flash
往往还有额度。这里的用例逐条钉住 task-2 的 1–10 条契约：

1. 1005 全池 → 换到下一个模型，客户端 200，响应头与台账如实回报（换没换、原本要什么）；
2. 3006（两种承载形态）同样换模型；
3. 3009 模型并发**绝不**换模型、**绝不**判死（仍走短等重试整轮）；
4. 401 / 403 / 5xx / 3012 风控**不**换模型（只换号或失败）；
5. `x-zcode-no-fallback: 1` / `x-zcode-model-fallback: off` 单请求关掉兜底 → 503；
6. 整条链都不可用 → 503 且 message 点名试过的模型；
7. 兜底成功后记住 TTL：下一个请求直接跳到可用模型，零上游请求打到死模型；
8. 主动式：额度快照里该模型全池 remaining <= 0 → 直接跳过，零上游请求；
9. `model_fallback_enabled=0` → 完全不兜底（旧行为 503）；
10. 没换模型也带 `x-zcode-model`；
11. `/admin/api/settings` 三个新字段能存能读。

mock 是会话级实例、modelpool 是模块级单例，所以每个用例进出都清一次状态，
否则用例互相污染（「上一个用例判死的模型」会把下一个用例直接带跑偏）。
"""

from __future__ import annotations

import json

import pytest

_ADMIN_AUTH = {"Authorization": "Bearer zcode"}   # 默认后台密钥
_GLM53 = "GLM-5.3"
_FLASH = "GLM-5.3-Flash"
_CHAIN = [_GLM53, _FLASH, "GLM-5.2", "GLM-5-Turbo", "GLM-4.7"]


# ── 夹具与工具 ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolated_fallback_state(mock_server, monkeypatch):
    """清 modelpool / mock 的模型判死集合 / 会话粘性，并掐掉后台额度刷新。

    后台刷新会拿 mock 的 billing/balance 改写 `account.quota`（那里 GLM-5.3 恒有
    余额），与「主动式额度判死」用例预置的快照互相打架，也会给上游调用计数添噪音。
    """
    from app import affinity
    from app import modelpool as modelpool_module
    from app.routes import gateway as gateway_module

    mock, _port = mock_server

    async def _noop_refresh(_account) -> None:
        return None

    monkeypatch.setattr(gateway_module, "_safe_refresh", _noop_refresh)

    def _reset() -> None:
        modelpool_module.modelpool.clear()
        mock.state.dead_models = set()
        mock.state.unavailable_models = set()
        mock.state.unavailable_models_400 = set()
        mock.state.sequences.clear()
        mock.state.counters.clear()
        affinity.reset()

    _reset()
    yield
    _reset()


def _secrets(tag: str, count: int = 4) -> list[str]:
    """一组 JWT 凭证：mock 按凭证前 16 字符分账，tag 不同即互不串场。"""
    return [f"{tag}{i}.eyJzdWIiOiJhIn0.sig" for i in range(count)]


def _seed_pool(store, secrets: list[str]) -> list:
    """按给定凭证铺满账号池。"""
    return [store.add_account("zai", f"fb-{secret[:6]}", secret) for secret in secrets]


def _call_mark(mock) -> int:
    """记下 mock 当前调用条数：后面只统计「本请求之后」的上游调用。"""
    return len(mock.state.calls)


def _messages_payloads(mock, mark: int = 0) -> list[dict]:
    """第 mark 条之后的 messages 请求体（跳过 billing 等后台 GET）。"""
    out: list[dict] = []
    for _method, path, _headers, body in mock.state.calls[mark:]:
        if path.endswith("/v1/messages"):
            out.append(json.loads(body))
    return out


def _models_hit(mock, mark: int = 0) -> list[str]:
    """上游实际收到的模型名序列（按调用顺序）。"""
    return [str(payload.get("model") or "") for payload in _messages_payloads(mock, mark)]


def _set_sequence(mock, secret: str, sequence: list[str]) -> None:
    """给某凭证排场景队列并清零计数（mock 是会话级实例，计数会跨用例累加）。"""
    bind = secret[:16]
    mock.state.counters[bind] = 0
    mock.state.sequences[bind] = sequence


def _no_sleep(monkeypatch) -> None:
    """把网关的等待函数换成空实现：重试轮次照跑，但用例不必真等秒级。"""
    from app.routes import gateway as gateway_module

    async def _skip(_seconds: float) -> None:
        return None

    monkeypatch.setattr(gateway_module, "_sleep", _skip)


def _body(model: str = _GLM53, **extra) -> dict:
    payload = {"model": model, "max_tokens": 64,
               "messages": [{"role": "user", "content": "hi"}]}
    payload.update(extra)
    return payload


# ── 1 / 2：模型级不可用 → 换模型 ─────────────────────────────────────────────
@pytest.mark.integration
class TestFallbackOnModelUnavailable:
    async def test_quota_exhausted_falls_back_to_next_model(self, gateway_client, fresh_app):
        """契约 1：4 个账号的 GLM-5.3 都回 1005 → 换 Flash 服务，客户端 200 且如实回报。

        响应头与台账都要能看出「实际服务的是 Flash、客户端原本点的是 GLM-5.3」，
        否则后台统计与客户端排障都无从判断这次请求被换过模型。
        """
        client, mock = gateway_client
        _seed_pool(fresh_app, _secrets("q"))
        mock.state.dead_models = {_GLM53}
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 200, res.text
        assert res.headers["x-zcode-model"] == _FLASH
        assert res.headers["x-zcode-model-requested"] == _GLM53
        # 上游：GLM-5.3 被 4 个账号各撞一次（每号一次），然后 Flash 一次
        assert _models_hit(mock, mark) == [_GLM53] * 4 + [_FLASH]

        usage = (await client.get("/admin/api/usage", headers=_ADMIN_AUTH)).json()
        row = usage["rows"][0]
        assert row["model"] == _FLASH
        assert row["requested_model"] == _GLM53
        assert row["http_status"] == 200 and row["outcome"] == "completed"
        assert usage["stats"]["fallback"] == 1     # 聚合里的「换过模型」计数

    async def test_model_not_allowed_3006_falls_back(self, gateway_client, fresh_app):
        """契约 2：3006「model not allowed」（HTTP 200 + body code）同样算模型不可用。"""
        client, mock = gateway_client
        _seed_pool(fresh_app, _secrets("u"))
        mock.state.unavailable_models = {_GLM53}
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 200, res.text
        assert res.headers["x-zcode-model"] == _FLASH
        assert _models_hit(mock, mark) == [_GLM53] * 4 + [_FLASH]

    async def test_model_not_allowed_3006_on_http_400_falls_back(self, gateway_client, fresh_app):
        """契约 2：3006 也可能以 HTTP 400 + body code 承载，判定不能只看状态码。"""
        client, mock = gateway_client
        _seed_pool(fresh_app, _secrets("v"))
        mock.state.unavailable_models_400 = {_GLM53}
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 200, res.text
        assert res.headers["x-zcode-model"] == _FLASH
        assert _models_hit(mock, mark) == [_GLM53] * 4 + [_FLASH]

    async def test_openai_endpoint_also_falls_back(self, gateway_client, fresh_app):
        """契约 1：OpenAI 端点与 messages 共用同一套调度，换模型后 body 里的 model 也如实。"""
        client, mock = gateway_client
        _seed_pool(fresh_app, _secrets("w"))
        mock.state.dead_models = {_GLM53}

        res = await client.post("/v1/chat/completions",
                                json={"model": "glm-5.3",
                                      "messages": [{"role": "user", "content": "hi"}]})

        assert res.status_code == 200, res.text
        assert res.headers["x-zcode-model"] == _FLASH
        assert res.headers["x-zcode-model-requested"] == _GLM53
        assert res.json()["model"] == _FLASH


# ── 1b：池子大小 vs 尝试上限（「问完了才许判死」）────────────────────────────
@pytest.mark.integration
class TestPoolSizeVersusAttemptLimit:
    async def test_pool_size_equal_to_attempt_limit_still_switches(
            self, gateway_client, fresh_app):
        """回归：可轮询账号数正好等于 MAX_ACCOUNT_ATTEMPTS 时仍必须换模型。

        真实池有 5 个账号，`MAX_ACCOUNT_ATTEMPTS` 也是 5：旧实现靠「store.select
        返回 None」判断「全池都问过了」，可 while 循环先被 attempts 上限截断，
        永远等不到那个 None → 5 个账号全回 3006/1005 也不换模型，直接 503。
        兜底因此形同虚设，这是线上实测抓到的真 bug。
        """
        client, mock = gateway_client
        from app.routes import gateway as gateway_module

        assert len(_seed_pool(fresh_app, _secrets("five", 5))) == 5
        assert gateway_module.MAX_ACCOUNT_ATTEMPTS == 5      # 前提：账号数 == 尝试上限
        mock.state.dead_models = {_GLM53}
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 200, res.text
        assert res.headers["x-zcode-model"] == _FLASH
        assert res.headers["x-zcode-model-requested"] == _GLM53
        # 5 个账号各撞一次 GLM-5.3，然后 1 次 Flash 成功
        assert _models_hit(mock, mark) == [_GLM53] * 5 + [_FLASH]

    async def test_pool_larger_than_attempt_limit_does_not_switch(
            self, gateway_client, fresh_app):
        """反面：账号数多于尝试上限时「没问完不许判死」，保守地不换模型。

        6 个账号只问了 5 个就撞上上限：剩下那个没问过的账号可能正好有这个模型的
        额度，此时判死会把流量赶去更弱的模型。宁可 503 让客户端重试。
        """
        client, mock = gateway_client
        from app import modelpool as modelpool_module

        _seed_pool(fresh_app, _secrets("six", 6))
        mock.state.dead_models = {_GLM53}
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 503, res.text
        assert res.headers.get("x-zcode-model") is None
        assert _models_hit(mock, mark) == [_GLM53] * 5       # 没碰 Flash
        assert "GLM-5.3" in res.json()["error"]["message"]
        # 没问完就不留死标记：后续请求仍会先照试这个模型
        assert modelpool_module.modelpool.state("zai", _GLM53) in ("quota_ok", "unknown")


# ── 3 / 4：不该换模型的信号 ──────────────────────────────────────────────────
@pytest.mark.integration
class TestNoFallbackForNonModelFailures:
    async def test_model_concurrency_3009_never_switches_model(
            self, gateway_client, fresh_app, monkeypatch):
        """契约 3：3009 是「模型并发租约没释放」，等一两秒就好——绝不换模型、绝不判死。

        误判成模型不可用会把一个健康模型锁死 TTL，且换过去的模型同样可能并发满。
        """
        from app import modelpool as modelpool_module
        from app import settings as settings_module

        client, mock = gateway_client
        _no_sleep(monkeypatch)
        monkeypatch.setattr(settings_module, "MODEL_BUSY_ROUNDS", 0)   # 只跑一轮，不真等
        secrets = _secrets("b")
        _seed_pool(fresh_app, secrets)
        for secret in secrets:
            _set_sequence(mock, secret, ["business_concurrency_429"])
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 503
        assert "并发" in res.json()["error"]["message"]
        assert set(_models_hit(mock, mark)) == {_GLM53}       # 只撞 GLM-5.3，没换模型
        pool = modelpool_module.modelpool
        # 状态必须仍是 quota_ok / unknown 之一：3009 只是并发租约，不是「模型不可用」
        # 的证据。判成 dead 会把这个健康模型锁死整个 TTL，且候选链会绕开它。
        assert pool.state("zai", _GLM53) in ("quota_ok", "unknown")
        assert pool.state("zai", _GLM53) != "dead"
        assert pool.reason("zai", _GLM53) == ""
        assert pool.candidates("zai", _GLM53)[0] == _GLM53    # 候选链没被污染

    @pytest.mark.parametrize("scenario", ["auth_invalid", "forbidden", "server_error",
                                          "risk_control_3012"])
    async def test_account_failures_never_switch_model(
            self, gateway_client, fresh_app, monkeypatch, scenario):
        """契约 4：401 / 403 / 5xx / 3012 风控是账号或通道问题，换模型救不了。

        这类失败只换号或直接失败；一旦被当成「模型不可用」，会把整条兜底链
        白白锁死（每个模型都判死 → 全池不可用）。
        """
        from app import modelpool as modelpool_module

        client, mock = gateway_client
        _no_sleep(monkeypatch)                       # 5xx 重试不真等
        secrets = _secrets("n" + scenario[0])
        _seed_pool(fresh_app, secrets)
        for secret in secrets:
            _set_sequence(mock, secret, [scenario])
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 503, res.text
        assert set(_models_hit(mock, mark)) == {_GLM53}
        # 账号/通道故障不得变成「模型不可用」的证据
        assert modelpool_module.modelpool.state("zai", _GLM53) in ("quota_ok", "unknown")


# ── 5 / 9：关掉兜底 ──────────────────────────────────────────────────────────
@pytest.mark.integration
class TestFallbackOptOut:
    @pytest.mark.parametrize("headers", [
        {"x-zcode-no-fallback": "1"},
        {"x-zcode-model-fallback": "off"},
    ])
    async def test_per_request_opt_out_returns_503(self, gateway_client, fresh_app, headers):
        """契约 5：单请求声明「不许换模型」→ GLM-5.3 全 1005 时如实回 503，不偷换。

        评测 / 回归对比类客户端必须拿到自己点的模型，宁可失败也不要被换。
        """
        client, mock = gateway_client
        _seed_pool(fresh_app, _secrets("o"))
        mock.state.dead_models = {_GLM53}
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body(), headers=headers)

        assert res.status_code == 503
        assert "x-zcode-model" not in res.headers
        assert set(_models_hit(mock, mark)) == {_GLM53}   # 没碰 Flash

    async def test_setting_disables_fallback(self, gateway_client, fresh_app):
        """契约 9：总开关 `model_fallback_enabled=0` → 完全不兜底，保持旧行为 503。"""
        client, mock = gateway_client
        _seed_pool(fresh_app, _secrets("s"))
        mock.state.dead_models = {_GLM53}
        fresh_app.set_setting("model_fallback_enabled", "0")
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 503
        assert set(_models_hit(mock, mark)) == {_GLM53}


# ── 6：整条链都不可用 ────────────────────────────────────────────────────────
@pytest.mark.integration
class TestAllModelsUnavailable:
    async def test_503_names_every_tried_model(self, gateway_client, fresh_app):
        """契约 6：整条兜底链都不可用 → 503，message 点名每个试过的模型（排障靠它）。"""
        client, mock = gateway_client
        _seed_pool(fresh_app, _secrets("a"))
        mock.state.dead_models = set(_CHAIN)
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 503
        assert res.json()["error"]["type"] == "no_available_account"
        message = res.json()["error"]["message"]
        assert "全池不可用" in message
        for model in _CHAIN:
            assert model in message
        assert set(_models_hit(mock, mark)) == set(_CHAIN)   # 每个模型都真的试过


# ── 7：反应式记忆 ────────────────────────────────────────────────────────────
@pytest.mark.integration
class TestDeadModelRemembered:
    async def test_second_request_skips_dead_model(self, gateway_client, fresh_app):
        """契约 7：兜底成功后该模型被记住 TTL，第二个请求直接落到可用模型。

        上游调用序列是关键证据：第二个请求**一次**都不该再打到 GLM-5.3
        （否则每个请求都要拿 N 个账号去撞同一堵墙，白烧 N 次往返）。
        """
        from app import modelpool as modelpool_module

        client, mock = gateway_client
        _seed_pool(fresh_app, _secrets("r"))
        mock.state.dead_models = {_GLM53}

        first = await client.post("/v1/messages", json=_body())
        assert first.status_code == 200, first.text
        assert first.headers["x-zcode-model"] == _FLASH
        assert modelpool_module.modelpool.state("zai", _GLM53) == "dead"

        mark = _call_mark(mock)
        second = await client.post("/v1/messages", json=_body())

        assert second.status_code == 200, second.text
        assert _models_hit(mock, mark) == [_FLASH]           # 只打了 Flash，没再撞 GLM-5.3
        assert second.headers["x-zcode-model"] == _FLASH
        # 客户端点的仍是 GLM-5.3：换了模型就必须告诉它
        assert second.headers["x-zcode-model-requested"] == _GLM53


# ── 8：主动式（额度快照） ────────────────────────────────────────────────────
@pytest.mark.integration
class TestProactiveQuotaEvidence:
    async def test_zero_remaining_skips_model_without_upstream_call(
            self, gateway_client, fresh_app):
        """契约 8：额度快照里该模型全池 remaining <= 0 → 直接跳到下一个模型。

        这是「零上游请求」的主动式判死：连 4 次 1005 的往返都不该发生。
        """
        from app import modelpool as modelpool_module

        client, mock = gateway_client
        accounts = _seed_pool(fresh_app, _secrets("p"))
        for account in accounts:
            account.quota = {
                _GLM53: {"total": 100, "used": 100, "remaining": 0, "expires_at": None},
                _FLASH: {"total": 100, "used": 0, "remaining": 100, "expires_at": None},
            }
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 200, res.text
        assert modelpool_module.modelpool.state("zai", _GLM53) == "dead"
        assert _models_hit(mock, mark) == [_FLASH]           # 零请求打到 GLM-5.3
        assert res.headers["x-zcode-model"] == _FLASH
        assert res.headers["x-zcode-model-requested"] == _GLM53

    async def test_snapshot_with_one_account_remaining_keeps_model(self, gateway_client, fresh_app):
        """契约 8 的反面：只要还有一个账号有剩余额度，就必须先照试原模型。

        额度快照是「证据」而不是「命令」——误判会绕过一个其实可用的模型。
        """
        client, mock = gateway_client
        accounts = _seed_pool(fresh_app, _secrets("t"))
        for index, account in enumerate(accounts):
            account.quota = {_GLM53: {"total": 100, "used": 0,
                                      "remaining": 0 if index else 100,
                                      "expires_at": None}}
        mark = _call_mark(mock)

        res = await client.post("/v1/messages", json=_body())

        assert res.status_code == 200, res.text
        assert _models_hit(mock, mark) == [_GLM53]
        assert res.headers["x-zcode-model"] == _GLM53


# ── 10 / 11：响应头与设置接口 ────────────────────────────────────────────────
@pytest.mark.integration
class TestHeadersAndSettings:
    async def test_success_without_fallback_reports_model(self, gateway_client, fresh_app):
        """契约 10：没换模型也带 `x-zcode-model`，且不带 `x-zcode-model-requested`。"""
        client, _mock = gateway_client
        _seed_pool(fresh_app, _secrets("h"))

        res = await client.post("/v1/messages", json=_body(_FLASH))

        assert res.status_code == 200, res.text
        assert res.headers["x-zcode-model"] == _FLASH
        assert "x-zcode-model-requested" not in res.headers

    async def test_settings_roundtrip(self, gateway_client, fresh_app):
        """契约 11：三个兜底设置项能存能读（后台改完即生效，不重启）。

        读回的是**归一化后**的链（小写别名 → 上游官方名、去重、去空）：后台输入框
        里显示的名字必须和网关真正发出去的模型名一致，否则改完保存会以为没生效。
        """
        client, _mock = gateway_client

        res = await client.put("/admin/api/settings", headers=_ADMIN_AUTH, json={
            "model_fallback_enabled": False,
            "model_fallback_chain": "glm-5.3-flash, GLM-5.3",
            "model_fallback_ttl": 120,
        })
        assert res.status_code == 200, res.text

        settings = (await client.get("/admin/api/settings", headers=_ADMIN_AUTH)).json()
        assert settings["model_fallback_enabled"] is False
        assert settings["model_fallback_chain"] == f"{_FLASH},{_GLM53}"
        assert settings["model_fallback_ttl"] == 120
        # 落库即生效：modelpool 读到的就是同一份配置
        from app.modelpool import modelpool
        assert modelpool.enabled() is False
        assert modelpool.chain("zai") == [_FLASH, _GLM53]

    async def test_models_endpoint_reports_fallback_ledger(self, gateway_client, fresh_app):
        """后台「模型兜底」卡片的数据源：配置 + 每个模型的可用性证据。"""
        from app import modelpool as modelpool_module

        client, _mock = gateway_client
        _seed_pool(fresh_app, _secrets("m"))
        modelpool_module.modelpool.mark_dead("zai", _GLM53, "额度用尽")

        data = (await client.get("/admin/api/models", headers=_ADMIN_AUTH)).json()

        assert data["enabled"] is True
        assert _GLM53 in data["chain"]
        dead = next(row for row in data["models"] if row["model"] == _GLM53)
        assert dead["state"] == "dead" and dead["reason"] == "额度用尽"
        assert dead["dead_until"] is not None
