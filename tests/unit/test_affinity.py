"""会话粘性（affinity）单元测试。

覆盖：指纹稳定性（多轮增长不影响）、有效期、账号池变化后的回退、
以及 store.select 的「粘性命中不推进轮询指针」语义。
"""

from __future__ import annotations

import time

import pytest

from app import affinity, settings
from app.store import Store


@pytest.fixture(autouse=True)
def _clean():
    """每个用例前后清空粘性表，避免用例间互相污染。"""
    affinity.reset()
    yield
    affinity.reset()


class TestKeyOf:
    def test_same_conversation_same_key(self):
        first = {"system": "守则", "messages": [{"role": "user", "content": "你好"}]}
        # 多轮之后 messages 变长，但 system 与第一条 user 不变
        later = {"system": "守则", "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在"},
            {"role": "user", "content": "继续"},
        ]}
        assert affinity.key_of(first) == affinity.key_of(later)
        assert len(affinity.key_of(first)) == 16

    def test_different_conversations_differ(self):
        a = {"messages": [{"role": "user", "content": "甲"}]}
        b = {"messages": [{"role": "user", "content": "乙"}]}
        assert affinity.key_of(a) != affinity.key_of(b)

    def test_system_prompt_matters(self):
        one = {"system": "A", "messages": [{"role": "user", "content": "同"}]}
        two = {"system": "B", "messages": [{"role": "user", "content": "同"}]}
        assert affinity.key_of(one) != affinity.key_of(two)

    def test_system_as_blocks(self):
        """system 以分块数组形式下发时同样参与指纹。"""
        body = {"system": [{"type": "text", "text": "守则"}],
                "messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]}
        assert affinity.key_of(body) == affinity.key_of(
            {"system": "守则", "messages": [{"role": "user", "content": "你好"}]})

    def test_empty_body_has_no_key(self):
        assert affinity.key_of({"messages": []}) == ""
        assert affinity.key_of({}) == ""


class TestSticky:
    def test_prefer_remembers_last_answerer(self):
        affinity.remember("zai", "k1", "acc-1")
        assert affinity.prefer("zai", "k1") == "acc-1"
        assert affinity.prefer("zai", "k2") is None  # 另一段对话没有记忆

    def test_expired_record_ignored(self, monkeypatch):
        affinity.remember("zai", "k1", "acc-1")
        monkeypatch.setattr(settings, "AFFINITY_TTL", 0)
        time.sleep(0.01)
        assert affinity.prefer("zai", "k1") is None

    def test_empty_key_is_noop(self):
        affinity.remember("zai", "", "acc-1")
        assert affinity.prefer("zai", "") is None


class TestSelectPrefer:
    @staticmethod
    def _store(fresh_app) -> Store:
        """用隔离 data 目录的 store（fresh_app），绝不碰真实 accounts.db。"""
        store = fresh_app
        store.add_account("zai", "a1", "jwt-1")
        store.add_account("zai", "a2", "jwt-2")
        return store

    def test_prefer_wins_over_round_robin(self, fresh_app):
        store = self._store(fresh_app)
        target = store.list_accounts("zai")[1]
        assert store.select("zai", prefer=target.id).id == target.id
        # 连续多次命中同一账号：粘性不该被轮询指针顶掉
        assert store.select("zai", prefer=target.id).id == target.id

    def test_prefer_does_not_advance_rotation(self, fresh_app):
        """粘性命中不推进指针：轮询节奏保持原样。"""
        store = self._store(fresh_app)
        accounts = store.list_accounts("zai")
        target = accounts[1]
        store.select("zai", prefer=target.id)
        store.select("zai", prefer=target.id)
        assert store.select("zai").id == accounts[0].id

    def test_prefer_ignored_when_account_unavailable(self, fresh_app):
        store = self._store(fresh_app)
        target = store.list_accounts("zai")[1]
        store.set_enabled("zai", target.id, False)  # 粘住的账号被禁用
        picked = store.select("zai", prefer=target.id)
        assert picked is not None and picked.id != target.id

    def test_prefer_ignored_when_skipped(self, fresh_app):
        store = self._store(fresh_app)
        target = store.list_accounts("zai")[1]
        picked = store.select("zai", skip_ids={target.id}, prefer=target.id)
        assert picked is not None and picked.id != target.id
