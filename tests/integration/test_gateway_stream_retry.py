"""流式上游中断的收口与透明重试（2026-11-02 `incomplete chunked read`）。

实测：上游偶发在 SSE 中途关闭连接。网关行为必须是：
- **尚未向客户端发出任何内容** → 重新调度上游（换连接/换号），客户端无感；
- **已经出过内容** → 不能重放（会重复 message_start/正文），补一条标准
  `event: error` 帧后收口，客户端能明确「回答不完整」而不是把半截当完整。
"""

from __future__ import annotations

import pytest

ADMIN_AUTH = {"Authorization": "Bearer zcode"}  # 默认后台密钥

# 每个用例独立凭证：mock 的场景序列按凭证前缀绑定，复用会串场
_RETRY_JWT = "hR.eyJzdWIiOiJyIn0.sig"
_NO_RETRY_JWT = "hX.eyJzdWIiOiJ4In0.sig"
_OAI_RETRY_JWT = "hQ.eyJzdWIiOiJxIn0.sig"


def _set_sequence(mock, secret: str, sequence: list[str]) -> None:
    bind = secret[:16]
    mock.state.counters[bind] = 0
    mock.state.sequences[bind] = sequence


def _calls(mock, secret: str) -> int:
    return int(mock.state.counters.get(secret[:16], 0))


def _messages_payload() -> dict:
    return {
        "model": "GLM-5.3-Flash",
        "max_tokens": 64,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }


@pytest.mark.integration
class TestStreamAbortRetry:
    async def test_abort_before_first_byte_retries_transparently(
        self, gateway_client, fresh_app
    ):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _RETRY_JWT, name="stream-retry")
        _set_sequence(mock, _RETRY_JWT, ["sse_abort", "ok"])

        res = await client.post("/v1/messages", json=_messages_payload())
        assert res.status_code == 200
        text = res.text
        assert "message_start" in text, "重试后的完整流应当透传给客户端"
        assert "event: error" not in text, "未出字的中断不应让客户端看到错误"
        assert _calls(mock, _RETRY_JWT) == 2, "上游应被调用两次（第一次中断 + 重试）"

        mon = await client.get("/admin/api/monitoring", headers=ADMIN_AUTH)
        entry = mon.json()["entries"][0]
        assert entry["outcome"] == "completed" and entry["ok"] is True

    async def test_abort_after_first_byte_is_not_retried(
        self, gateway_client, fresh_app
    ):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _NO_RETRY_JWT, name="stream-no-retry")
        _set_sequence(mock, _NO_RETRY_JWT, ["sse_abort_after", "ok"])

        res = await client.post("/v1/messages", json=_messages_payload())
        assert res.status_code == 200
        text = res.text
        assert "message_start" in text, "中断前已透传的内容保留"
        assert "event: error" in text, "已出字的中断必须显式告知客户端"
        assert _calls(mock, _NO_RETRY_JWT) == 1, "已经输出过内容不能重放"

        mon = await client.get("/admin/api/monitoring", headers=ADMIN_AUTH)
        entry = mon.json()["entries"][0]
        assert entry["outcome"] == "upstream_aborted"
        assert entry["ok"] is False

    async def test_openai_stream_retry_before_first_chunk(
        self, gateway_client, fresh_app
    ):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _OAI_RETRY_JWT, name="oai-stream-retry")
        _set_sequence(mock, _OAI_RETRY_JWT, ["sse_abort", "ok"])

        res = await client.post("/v1/chat/completions", json={
            "model": "glm-5.3",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert res.status_code == 200
        text = res.text
        assert "data: [DONE]" in text
        assert '"error"' not in text
        assert _calls(mock, _OAI_RETRY_JWT) == 2
