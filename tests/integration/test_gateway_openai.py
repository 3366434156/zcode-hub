"""GW-011 /v1/chat/completions（OpenAI 风格）端点集成测试。

复用 /v1/messages 同一套 mock 上游（返回 Anthropic 格式），验证双向转换：
非流式 JSON 与流式 SSE chunk。
"""

from __future__ import annotations

import json

import pytest

_GOOD_JWT = "hO.eyJzdWIiOiJvIn0.sig"


def _last_messages_body(mock) -> dict:
    """最近一次 messages 调用的请求体。

    mock.state.calls 是会话级共享记录，后台 billing 刷新（GET，空 body）
    随时可能追加在后面，所以不能直接取 calls[-1]。
    """
    for _method, path, _headers, body in reversed(mock.state.calls):
        if path.endswith("/v1/messages"):
            return json.loads(body)
    raise AssertionError("没有收到任何 messages 调用")


def _bind_of(secret: str) -> str:
    """mock 侧按凭证前缀分场景（_bind_key = cred[:16]）。"""
    return secret[:16]


def _set_sequence(mock, secret: str, sequence: list[str]) -> None:
    """给某凭证排场景队列，并把该凭证的调用计数清零。

    mock 是会话级实例：counters 跨用例累加，不清零的话场景队列会直接落在
    最后一个元素上（前面的排期永远轮不到）。
    """
    bind = _bind_of(secret)
    mock.state.counters[bind] = 0
    mock.state.sequences[bind] = sequence


def _clear_sequence(mock, secret: str) -> None:
    mock.state.sequences.pop(_bind_of(secret), None)


@pytest.mark.integration
class TestUpstreamBusinessError:
    """上游「HTTP 200 + body 里的 code」不得被当成成功。"""

    async def test_200_with_code_is_a_failure(self, gateway_client, fresh_app):
        """额度用尽（code 1005）是 200 响应：旧实现会原样透传给客户端当成功。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="biz-quota")
        _set_sequence(mock, _GOOD_JWT, ["business_quota_200"])
        try:
            res = await client.post(
                "/v1/chat/completions",
                json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            _clear_sequence(mock, _GOOD_JWT)
        assert res.status_code == 503
        assert "额度" in res.json()["error"]["message"]

    async def test_concurrency_limit_rotates_without_waiting(self, gateway_client, fresh_app,
                                                             monkeypatch):
        """code 3009 是「模型并发租约没释放」：短等重试整轮，而不是原地等 60s。"""
        client, mock = gateway_client
        from app import settings as settings_module
        from app.routes import gateway as gateway_module
        from tests.conftest import seed_account

        waited: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waited.append(seconds)

        monkeypatch.setattr(gateway_module, "_sleep", fake_sleep)
        monkeypatch.setattr(settings_module, "MODEL_BUSY_WAIT", 1.0)
        monkeypatch.setattr(settings_module, "MODEL_BUSY_ROUNDS", 2)
        seed_account(fresh_app, _GOOD_JWT, name="biz-3009")
        # 第一轮 3009，第二轮就成功：证明「等 1s 再来」这一手确实能接上
        _set_sequence(mock, _GOOD_JWT, ["business_concurrency_429", "ok"])
        try:
            res = await client.post(
                "/v1/chat/completions",
                json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            _clear_sequence(mock, _GOOD_JWT)
        assert res.status_code == 200
        assert waited == [1.0]        # 只等了一轮 1s，没进入 429 的 60s 等待

    async def test_concurrency_gives_up_after_rounds(self, gateway_client, fresh_app,
                                                     monkeypatch):
        """一直 3009 时按轮数收口：不会无限等，503 里说明原因。"""
        client, mock = gateway_client
        from app import settings as settings_module
        from app.routes import gateway as gateway_module
        from tests.conftest import seed_account

        waited: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waited.append(seconds)

        monkeypatch.setattr(gateway_module, "_sleep", fake_sleep)
        monkeypatch.setattr(settings_module, "MODEL_BUSY_WAIT", 1.0)
        monkeypatch.setattr(settings_module, "MODEL_BUSY_ROUNDS", 1)
        seed_account(fresh_app, _GOOD_JWT, name="biz-3009-stuck")
        _set_sequence(mock, _GOOD_JWT, ["business_concurrency_429"])
        try:
            res = await client.post(
                "/v1/chat/completions",
                json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            _clear_sequence(mock, _GOOD_JWT)
        assert res.status_code == 503
        assert "并发" in res.json()["error"]["message"]
        assert waited == [1.0]        # 只等了一轮就收口



@pytest.mark.integration
class TestChatCompletions:
    async def test_nonstream_basic(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai")
        res = await client.post(
            "/v1/chat/completions",
            json={"model": "glm-5.3-flash",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == "GLM-5.3-Flash"
        assert data["choices"][0]["message"]["content"] == "Hello from mock upstream"
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["usage"]["prompt_tokens"] == 10
        # 最近请求明细：{ok, at, detail} 形态，悬停 tooltip 数据源
        acc = next(a for a in fresh_app.list_accounts("zai") if a.name == "oai")
        last = acc.recent_results[-1]
        assert isinstance(last, dict) and last["ok"] is True
        assert "HTTP 200" in last["detail"] and last["at"] > 0

    async def test_system_message_not_sent_as_system(self, gateway_client, fresh_app):
        """客户端 system 不再进上游 system 字段（只留官方两块，见 docs/05 §9.1）。

        上游只接受「官方前两块」组成的 system，多一块就 3012；客户端 system 被折叠进
        首条 user 消息——内容仍然进模型，形状才合规。
        """
        client, mock = gateway_client
        from app.body_transform import _CLI_MARKER
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-sys")
        res = await client.post(
            "/v1/chat/completions",
            json={"model": "GLM-5.3",
                  "messages": [{"role": "system", "content": "守则"},
                               {"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        payload = _last_messages_body(mock)
        assert [b["text"] for b in payload["system"]] == [b["text"] for b in _CLI_MARKER]
        first = payload["messages"][0]
        content = first["content"]
        text = content[0]["text"] if isinstance(content, list) else content
        assert "守则" in text

    async def test_max_tokens_clamped_to_upstream_limit(self, gateway_client, fresh_app):
        """上游 400 code 1210：max_tokens 上限 131072，网关钳制（GW-012）。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-mt")
        res = await client.post(
            "/v1/messages",
            json={"model": "GLM-5.3", "max_tokens": 999999,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        payload = _last_messages_body(mock)
        assert payload["max_tokens"] == 131072

    async def test_max_tokens_string_form_clamped(self, gateway_client, fresh_app):
        """字符串型 max_tokens 也要钳制，防止绕过类型检查打到上游 1210。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-mt-str")
        res = await client.post(
            "/v1/messages",
            json={"model": "GLM-5.3", "max_tokens": "999999",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        payload = _last_messages_body(mock)
        assert payload["max_tokens"] == 131072

    async def test_max_tokens_floor_and_passthrough(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-mt2")
        res = await client.post(
            "/v1/messages",
            json={"model": "GLM-5.3", "max_tokens": 1024,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        payload = _last_messages_body(mock)
        assert payload["max_tokens"] == 1024  # 合法值原样透传

    async def test_stream_returns_openai_chunks(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-stream")
        res = await client.post(
            "/v1/chat/completions",
            json={"model": "glm-5.3-flash", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        lines = [ln for ln in res.text.splitlines() if ln.startswith("data: ")]
        assert lines[-1] == "data: [DONE]"
        chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        assert "chunk-0" in text
        finishes = [c["choices"][0]["finish_reason"] for c in chunks if c["choices"][0]["finish_reason"]]
        assert finishes == ["stop"]

    async def test_stream_forwards_reasoning_content(self, gateway_client, fresh_app):
        """思考块必须实时下发为 reasoning_content。

        否则模型整段思考期间客户端收不到任何字节，界面只能显示「未响应」
        ——这正是本机实测的根因（1996 条 thinking_delta 被丢弃，客户端静默 39s）。
        """
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-think")
        mock.state.sse_thinking = True
        try:
            res = await client.post(
                "/v1/chat/completions",
                json={"model": "glm-5.3-flash", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            mock.state.sse_thinking = False
        assert res.status_code == 200
        lines = [ln for ln in res.text.splitlines() if ln.startswith("data: ")]
        chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
        reasoning = "".join(c["choices"][0]["delta"].get("reasoning_content") or "" for c in chunks)
        assert reasoning == "想一想"
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        assert "chunk-0" in text
        usage = next(c["usage"] for c in chunks if c["choices"][0]["finish_reason"])
        assert usage["prompt_tokens_details"]["cached_tokens"] == 1024

    async def test_stream_keepalive_during_upstream_silence(
            self, gateway_client, fresh_app, monkeypatch):
        """上游静默期补 SSE 注释帧：客户端与中间代理都能判断连接还活着。"""
        client, mock = gateway_client
        from app import settings as settings_module
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="oai-keepalive")
        monkeypatch.setattr(settings_module, "SSE_KEEPALIVE_IDLE", 0.05)
        mock.state.sse_gap = 0.2
        try:
            res = await client.post(
                "/v1/chat/completions",
                json={"model": "glm-5.3-flash", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            mock.state.sse_gap = 0
        assert res.status_code == 200
        assert ": keep-alive" in res.text       # 心跳确实发出
        assert "chunk-0" in res.text            # 且不影响正文
        assert res.text.rstrip().endswith("[DONE]")

    async def test_same_conversation_sticks_to_one_account(self, gateway_client, fresh_app):
        """同一段对话回到同一账号（上游前缀缓存才能继续命中）；换对话才轮询。"""
        client, mock = gateway_client
        from app import affinity
        from tests.conftest import seed_account

        affinity.reset()
        seed_account(fresh_app, "jwt-aaaa", name="sticky-1")
        seed_account(fresh_app, "jwt-bbbb", name="sticky-2")

        def last_cred() -> str:
            """最近一次 messages 调用用的凭证（跳过 billing 等后台请求）。"""
            for _method, path, headers, _body in reversed(mock.state.calls):
                if path.endswith("/v1/messages"):
                    return headers.get("authorization") or headers.get("x-api-key") or ""
            return ""

        same = {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "同一段对话"}]}
        await client.post("/v1/chat/completions", json=same)
        first = last_cred()
        await client.post("/v1/chat/completions", json=same)
        assert last_cred() == first

        # 全局粘号（默认开）：新对话也先回到上次成功服务的账号，而不是轮询摊开
        # ——上游按「账号 × 模型」计并发租约，摊开换不来更高并发，只会看起来跳号。
        other = {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "另一段对话"}]}
        await client.post("/v1/chat/completions", json=other)
        assert last_cred() == first

        # 关掉全局粘号才回到老的轮询语义：清掉会话粘性后，新对话换下一个账号
        affinity.reset()
        fresh_app.set_setting("account_sticky", "0")
        third = {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "第三段对话"}]}
        await client.post("/v1/chat/completions", json=third)
        assert last_cred() != first
        fresh_app.set_setting("account_sticky", "1")
        affinity.reset()

    async def test_invalid_payload_rejected(self, gateway_client, fresh_app):
        client, _ = gateway_client
        res = await client.post("/v1/chat/completions",
                                json={"messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 400
        assert res.json()["error"]["type"] == "invalid_request_error"

    async def test_no_account_returns_503(self, fresh_app):
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/v1/chat/completions",
                                    json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 503
        assert res.json()["error"]["type"] == "no_available_account"

    async def test_gateway_key_required(self, gateway_client, fresh_app):
        client, _ = gateway_client
        fresh_app.set_setting("gateway_key", "sk-gw-test")
        try:
            res = await client.post("/v1/chat/completions",
                                    json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
            assert res.status_code == 401
            res = await client.post("/v1/chat/completions",
                                    headers={"x-api-key": "sk-gw-test"},
                                    json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
            assert res.status_code == 503  # 鉴权通过，进入调度（无账号）
        finally:
            fresh_app.set_setting("gateway_key", "")

