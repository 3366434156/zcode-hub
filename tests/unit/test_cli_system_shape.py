"""CLI 形态的 system 收敛（2026-10-31 上游规则变化）。

实测：上游只接受「官方前两块」组成的 system；客户端自带第 3+ 块时整条请求被判
405/3012（同一 body 砍成两块 → 200，134 个工具 / thinking / metadata 全部保留无影响）。
被剥掉的客户端块默认折叠进首条 user 消息，指令不丢。
"""

from __future__ import annotations

import copy

from app import body_transform, settings
from app.body_transform import _CLI_MARKER, apply_cli_system


def _official_blocks(n: int = 2) -> list[dict]:
    return [copy.deepcopy(b) for b in _CLI_MARKER[:n]]


def _client_body(extra_blocks: int = 3) -> dict:
    system = _official_blocks(2) + [
        {"type": "text", "text": f"客户端追加指令 {i}"} for i in range(extra_blocks)
    ]
    return {
        "model": "GLM-5.3-Flash",
        "max_tokens": 1024,
        "stream": True,
        "system": system,
        "messages": [{"role": "user", "content": "hi"}],
    }


def test_strict_keeps_only_official_two_blocks():
    body = _client_body(3)
    assert apply_cli_system(body, strict=True) is True
    assert len(body["system"]) == 2
    assert [b["text"] for b in body["system"]] == [b["text"] for b in _official_blocks(2)]


def test_strict_folds_dropped_blocks_into_first_user_message(monkeypatch):
    monkeypatch.setattr(settings, "CLI_SYSTEM_FOLD_TO_USER", True)
    body = _client_body(2)
    apply_cli_system(body, strict=True)
    content = body["messages"][0]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert "客户端追加指令 0" in content[0]["text"]
    assert content[0]["text"].startswith("客户端追加指令 0")   # 官方块不再重复折叠
    assert content[1]["text"] == "hi"


def test_fold_disabled_drops_extra_blocks(monkeypatch):
    monkeypatch.setattr(settings, "CLI_SYSTEM_FOLD_TO_USER", False)
    body = _client_body(2)
    apply_cli_system(body, strict=True)
    assert len(body["system"]) == 2
    assert body["messages"][0]["content"] == "hi"     # 原样不动


def test_strict_is_idempotent():
    body = _client_body(2)
    apply_cli_system(body, strict=True)
    snapshot = copy.deepcopy(body)
    assert apply_cli_system(body, strict=True) is False
    assert body == snapshot


def test_strict_from_settings_default_on(monkeypatch):
    monkeypatch.setattr(settings, "CLI_SYSTEM_STRICT", True)
    body = _client_body(3)
    apply_cli_system(body)                     # 不传 strict → 读设置
    assert len(body["system"]) == 2


def test_legacy_prepend_when_strict_off():
    body = _client_body(0)                     # 客户端没有任何 system
    body["system"] = [{"type": "text", "text": "客户端自己的 system"}]
    apply_cli_system(body, strict=False)
    assert len(body["system"]) == 3            # 官方两块 + 客户端块
    assert body["system"][-1]["text"] == "客户端自己的 system"


def test_no_system_still_gets_official_two():
    body = {"model": "GLM-5.3-Flash", "messages": [{"role": "user", "content": "hi"}]}
    assert apply_cli_system(body, strict=True) is True
    assert len(body["system"]) == 2
    assert body["messages"][0]["content"] == "hi"


def test_fold_handles_block_content_list_and_no_user_message():
    body = _client_body(1)
    body["messages"] = [{"role": "assistant", "content": "先说话"}]
    apply_cli_system(body, strict=True)        # 没有 user 消息：只收敛 system，不炸
    assert len(body["system"]) == 2
    assert body["messages"][0]["content"] == "先说话"


def test_transform_body_cli_end_to_end(monkeypatch):
    monkeypatch.setattr(settings, "CLI_SYSTEM_STRICT", True)
    monkeypatch.setattr(settings, "CLI_SYSTEM_FOLD_TO_USER", True)
    body = _client_body(3)
    out = body_transform.transform_body_cli(body)
    assert len(out["system"]) == 2
    assert isinstance(out["messages"][0]["content"], list)
