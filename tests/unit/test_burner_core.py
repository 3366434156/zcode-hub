"""burner_core 工单队列状态机的单元测试（纯文件逻辑，不碰网络/桌面）。

夹具把模块级路径全部指到 tmp_path，并给 logs 打桩——不污染网关真实 data/。
"""

from __future__ import annotations

import json
import time

import pytest

from app import burner_core


@pytest.fixture()
def burner_env(tmp_path, monkeypatch):
    """隔离的 burner 环境：队列/状态/频道文件全部落在 tmp_path。"""
    sup = tmp_path / "_sup"
    sup.mkdir()
    monkeypatch.setattr(burner_core, "DIR", tmp_path / "burner")
    monkeypatch.setattr(burner_core, "QUEUE_FILE", tmp_path / "burner" / "queue.json")
    monkeypatch.setattr(burner_core, "STATE_FILE", tmp_path / "burner" / "state.json")
    monkeypatch.setattr(burner_core, "PROMPT_FILE", sup / "next-prompt.txt")
    monkeypatch.setattr(burner_core, "ENABLE_FLAG", sup / "continue.enabled")
    monkeypatch.setattr(burner_core, "logs",
                        type("L", (), {"info": staticmethod(lambda *a, **k: None),
                                       "ok": staticmethod(lambda *a, **k: None),
                                       "warn": staticmethod(lambda *a, **k: None),
                                       "err": staticmethod(lambda *a, **k: None)}))
    return tmp_path


def _enable(tmp_path):
    (tmp_path / "_sup" / "continue.enabled").write_text("", encoding="utf-8")


def _consume_prompt(tmp_path):
    """模拟 Stop 钩子的一次性消费：读走并归档 next-prompt.txt（归档名唯一）。"""
    f = tmp_path / "_sup" / "next-prompt.txt"
    if f.exists():
        import itertools
        for i in itertools.count():
            dst = tmp_path / "_sup" / f"next-prompt.used-test-{i}.txt"
            if not dst.exists():
                f.rename(dst)
                return


# ── 入队校验 ────────────────────────────────────────────────────────────────
def test_enqueue_requires_title_and_prompt(burner_env):
    with pytest.raises(ValueError):
        burner_core.enqueue("", "做点活")
    with pytest.raises(ValueError):
        burner_core.enqueue("标题", "  ")


def test_enqueue_and_snapshot(burner_env):
    burner_core.enqueue("标题A", "指令A")
    snap = burner_core.snapshot()
    assert snap["stats"]["queued"] == 1
    assert snap["queue"][0]["title"] == "标题A"
    assert snap["queue"][0]["status"] == "queued"


# ── 总闸 ────────────────────────────────────────────────────────────────────
def test_disabled_no_dispatch(burner_env):
    burner_core.enqueue("标题A", "指令A")
    result = burner_core.tick()
    assert result["action"] == "idle"
    assert "总闸" in result["reason"]
    assert not (burner_env / "_sup" / "next-prompt.txt").exists()


# ── 派单 ────────────────────────────────────────────────────────────────────
def test_dispatch_writes_prompt(burner_env):
    _enable(burner_env)
    order = burner_core.enqueue("标题A", "指令A")
    result = burner_core.tick()
    assert result["action"] == "dispatched"
    assert (burner_env / "_sup" / "next-prompt.txt").read_text(encoding="utf-8").strip() == "指令A"
    snap = burner_core.snapshot()
    assert snap["queue"][0]["status"] == "dispatched"
    assert snap["dispatchedId"] == order["id"]
    assert snap["promptPending"] is True


def test_wait_when_prompt_unconsumed(burner_env):
    _enable(burner_env)
    burner_core.enqueue("标题A", "指令A")
    burner_core.tick()
    burner_core.enqueue("标题B", "指令B")
    # prompt 还在（钩子未消费）→ 在途工单进入验收分支，返回 running 不打扰
    result = burner_core.tick()
    assert result["action"] == "running"
    snap = burner_core.snapshot()
    assert snap["queue"][1]["status"] == "queued"


# ── 验收 ────────────────────────────────────────────────────────────────────
def test_accept_done_via_watch(burner_env):
    _enable(burner_env)
    watch = burner_env / "out.frag"
    watch.write_text("old", encoding="utf-8")
    burner_core.enqueue("标题A", "指令A", watch=[str(watch)])
    burner_core.tick()  # 派单，记录 watch 基线
    # 模拟：钩子消费指令 + agent 改了 watch 文件
    _consume_prompt(burner_env)
    time.sleep(0.02)
    watch.write_text("new", encoding="utf-8")
    result = burner_core.tick()
    assert result["action"] == "accepted"
    assert result["status"] == "done"
    snap = burner_core.snapshot()
    assert snap["queue"][0]["status"] == "done"
    assert snap["dispatchedId"] == ""


def test_accept_verify_failure_requeues_then_exhausts(burner_env):
    _enable(burner_env)
    watch = burner_env / "out.frag"
    watch.write_text("v1", encoding="utf-8")
    burner_core.enqueue("标题A", "指令A", watch=[str(watch)],
                        verify_cmd="exit /b 1", retries=1)  # Windows: 必失败
    burner_core.tick()  # 派单
    _consume_prompt(burner_env)
    time.sleep(0.02)
    watch.write_text("v2", encoding="utf-8")
    r1 = burner_core.tick()  # verify 失败，tries=1 <= retries=1 → 重排 queued
    assert r1["status"] == "queued"
    r2 = burner_core.tick()  # 重新派单
    assert r2["action"] == "dispatched"
    _consume_prompt(burner_env)
    time.sleep(0.02)
    watch.write_text("v3", encoding="utf-8")
    r3 = burner_core.tick()  # 再失败，tries=2 > retries=1 → failed
    assert r3["status"] == "failed"
    stored = {o["id"]: o for o in burner_core.snapshot()["queue"]}[r3["id"]]
    assert "重试耗尽" in stored["note"]


def test_accept_no_watch_change_fails(burner_env):
    _enable(burner_env)
    watch = burner_env / "out.frag"
    watch.write_text("x", encoding="utf-8")
    burner_core.enqueue("标题A", "指令A", watch=[str(watch)], retries=0)
    burner_core.tick()  # 派单
    _consume_prompt(burner_env)  # 回合结束但 watch 没动
    result = burner_core.tick()
    assert result["action"] == "accepted"
    assert result["status"] == "failed"
    assert "无变化" in result["note"]


def test_no_verify_cmd_watch_change_passes(burner_env):
    _enable(burner_env)
    watch = burner_env / "out.frag"
    watch.write_text("a", encoding="utf-8")
    burner_core.enqueue("标题A", "指令A", watch=[str(watch)])
    burner_core.tick()
    _consume_prompt(burner_env)
    time.sleep(0.02)
    watch.write_text("b", encoding="utf-8")
    result = burner_core.tick()
    assert result["status"] == "done"


# ── 管理操作 ────────────────────────────────────────────────────────────────
def test_cancel_and_retry_and_clear(burner_env):
    _enable(burner_env)
    a = burner_core.enqueue("A", "指令A")
    b = burner_core.enqueue("B", "指令B")
    burner_core.cancel(a["id"])
    snap = burner_core.snapshot()
    by_id = {o["id"]: o for o in snap["queue"]}
    assert by_id[a["id"]]["status"] == "cancelled"
    burner_core.tick()  # 应派 B（A 已取消）
    assert burner_core.snapshot()["dispatchedId"] == b["id"]
    burner_core.retry(b["id"])
    assert burner_core.snapshot()["dispatchedId"] == ""
    assert burner_core.clear_done() == 1  # 只清掉 cancelled 的 A
    assert len(burner_core.snapshot()["queue"]) == 1


def test_state_survives_reload(burner_env, monkeypatch):
    # 换一组文件路径模拟"进程重启后从磁盘恢复"
    _enable(burner_env)
    burner_core.enqueue("A", "指令A")
    burner_core.tick()
    prompt_before = (burner_env / "_sup" / "next-prompt.txt").exists()
    # 重载模块级常量指向同一路径（模拟新进程）
    monkeypatch.setattr(burner_core, "QUEUE_FILE",
                        burner_core.QUEUE_FILE)  # 路径未变；核心是 load_queue 从盘恢复
    snap = burner_core.snapshot()
    assert snap["queue"][0]["status"] == "dispatched"
    assert snap["dispatchedId"] == snap["queue"][0]["id"]
    assert prompt_before
