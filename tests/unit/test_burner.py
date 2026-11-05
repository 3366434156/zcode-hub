"""app.burner 工单队列状态机的单元测试（从 zburn-hub 合并）（纯文件逻辑，不碰网络/桌面）。

夹具把模块级路径全部指到 tmp_path，并给 logs 打桩——不污染网关真实 data/。
"""

from __future__ import annotations

import json
import time

import pytest

import app.burner as burner_core


@pytest.fixture()
def burner_env(tmp_path, monkeypatch):
    """隔离的 burner 环境：队列/状态/频道文件全部落在 tmp_path。"""
    sup = tmp_path / "_sup"
    sup.mkdir()
    monkeypatch.setattr(burner_core, "SUP_DIR", sup)
    monkeypatch.setattr(burner_core, "DIR", tmp_path / "burner")
    monkeypatch.setattr(burner_core, "QUEUE_FILE", tmp_path / "burner" / "queue.json")
    monkeypatch.setattr(burner_core, "STATE_FILE", tmp_path / "burner" / "state.json")
    monkeypatch.setattr(burner_core, "PROMPT_FILE", sup / "next-prompt.txt")
    monkeypatch.setattr(burner_core, "ENABLE_FLAG", sup / "continue.enabled")
    monkeypatch.setattr(burner_core, "SESSIONS_FILE", sup / "continue.sessions")
    monkeypatch.setattr(burner_core, "AUTO_TASK_FILE", sup / "auto-task.txt")
    monkeypatch.setattr(burner_core, "AUTO_HANDOFF_FILE", sup / "auto-handoff.md")
    monkeypatch.setattr(burner_core, "_emit", lambda kind, msg: None)
    return tmp_path


def _enable(tmp_path):
    (tmp_path / "_sup" / "continue.enabled").write_text("", encoding="utf-8")
    (tmp_path / "_sup" / "continue.sessions").write_text("sess-test\n", encoding="utf-8")


def _consume_prompt(tmp_path, slot="main"):
    """模拟 Stop 钩子：先写 turn-end，再消费指令（归档 + 写消费时刻），
    最后再写一次 turn-end 表示“任务回合”也结束了。"""
    sup = tmp_path / "_sup"
    (sup / f"turn-end.{slot}").write_text(str(time.time()), encoding="utf-8")
    time.sleep(0.02)
    name = "next-prompt.txt" if slot == "main" else f"next-prompt.{slot}.txt"
    f = sup / name
    if f.exists():
        import itertools
        for i in itertools.count():
            dst = sup / f"{f.stem}.used-test-{i}.txt"
            if not dst.exists():
                f.rename(dst)
                break
    (sup / f"prompt-consumed.{slot}").write_text(
        str(int(time.time() * 1000)), encoding="utf-8")
    time.sleep(0.02)
    (sup / f"turn-end.{slot}").write_text(str(time.time()), encoding="utf-8")


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


def test_empty_monitor_list_no_dispatch(burner_env):
    """main 炉位没绑会话时钩子不续派，队列派出去也没人消费——所以直接不派。"""
    (burner_env / "_sup" / "continue.enabled").write_text("", encoding="utf-8")
    burner_core.enqueue("标题A", "指令A")
    result = burner_core.tick()
    assert result["action"] == "idle"
    assert "没有绑定的监听会话" in result["reason"]
    assert not (burner_env / "_sup" / "next-prompt.txt").exists()


# ── 多炉位 ─────────────────────────────────────────────────────────────────
def test_slot_parsing(burner_env):
    (burner_env / "_sup" / "continue.sessions").write_text(
        "sess-a\nsess-b|s2\n# 注释\nsess-c|S3\nsess-d|\n", encoding="utf-8")
    assert burner_core.monitored_map() == {
        "sess-a": "main", "sess-b": "s2", "sess-c": "s3", "sess-d": "main"}
    assert burner_core.monitored_sessions("s2") == ["sess-b"]
    assert burner_core.monitored_sessions("main") == ["sess-a", "sess-d"]


def test_slots_dispatch_independently(burner_env):
    """两个炉位各有绑定会话：一轮 tick 里各派各的，写各自的指令文件。"""
    (burner_env / "_sup" / "continue.enabled").write_text("", encoding="utf-8")
    (burner_env / "_sup" / "continue.sessions").write_text(
        "sess-a\nsess-b|s2\n", encoding="utf-8")
    burner_core.enqueue("主炉单", "指令A", slot="main")
    burner_core.enqueue("旁路单", "指令B", slot="s2")
    result = burner_core.tick()
    assert result["action"] == "multi" and result["n"] == 2
    assert (burner_env / "_sup" / "next-prompt.txt").read_text(
        encoding="utf-8").strip() == "指令A"
    assert (burner_env / "_sup" / "next-prompt.s2.txt").read_text(
        encoding="utf-8").strip() == "指令B"
    snap = burner_core.snapshot()
    assert snap["slots"]["main"]["dispatchedId"]
    assert snap["slots"]["s2"]["dispatchedId"]
    assert snap["dispatchedId"] == snap["slots"]["main"]["dispatchedId"], "main 兼容顶层字段"


def test_slot_without_session_waits(burner_env):
    """s2 没绑会话：它的单不派；main 正常派。"""
    (burner_env / "_sup" / "continue.enabled").write_text("", encoding="utf-8")
    (burner_env / "_sup" / "continue.sessions").write_text("sess-a\n", encoding="utf-8")
    burner_core.enqueue("主炉单", "指令A", slot="main")
    burner_core.enqueue("旁路单", "指令B", slot="s2")
    result = burner_core.tick()
    assert result["action"] == "dispatched", "只有 main 派出"
    assert (burner_env / "_sup" / "next-prompt.s2.txt").exists() is False
    snap = burner_core.snapshot()
    by_slot = {o["title"]: o["status"] for o in snap["queue"]}
    assert by_slot["主炉单"] == "dispatched"
    assert by_slot["旁路单"] == "queued"


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


def test_accept_waits_for_turn_after_prompt_consumed(burner_env):
    """指令在“继续”回合被消费 ≠ 任务回合结束：必须等下一个 Stop。"""
    _enable(burner_env)
    watch = burner_env / "out.frag"
    watch.write_text("old", encoding="utf-8")
    burner_core.enqueue("标题A", "指令A", watch=[str(watch)])
    burner_core.tick()  # 派单

    sup = burner_env / "_sup"
    # 真实钩子顺序：Stop 先写 turn-end，再归档 next-prompt 并写消费标记
    (sup / "turn-end.main").write_text(str(time.time()), encoding="utf-8")
    time.sleep(0.02)
    (sup / "next-prompt.txt").rename(sup / "next-prompt.used-race.txt")
    (sup / "prompt-consumed.main").write_text(
        str(int(time.time() * 1000)), encoding="utf-8")

    # 消费指令的那个 Stop 不能作为验收依据
    r1 = burner_core.tick()
    assert r1["action"] == "running"
    assert "上一回合消费" in r1["reason"]

    # 任务回合真正结束（新的 Stop）且 watch 有产出 → 通过
    time.sleep(0.02)
    watch.write_text("new", encoding="utf-8")
    time.sleep(0.02)
    (sup / "turn-end.main").write_text(str(time.time()), encoding="utf-8")
    r2 = burner_core.tick()
    assert r2["action"] == "accepted"
    assert r2["status"] == "done"


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


# ── 额度自动派单（执行者是 Stop 钩子，burner 只提供配置/快照）──────────────
def _enable_auto(tmp_path, task="把下一批文档补完"):
    (tmp_path / "_sup" / "continue.enabled").write_text("", encoding="utf-8")
    (tmp_path / "_sup" / "continue.sessions").write_text("sess-test\n", encoding="utf-8")
    (tmp_path / "_sup" / "auto.enabled").write_text("", encoding="utf-8")
    (tmp_path / "_sup" / "auto-task.txt").write_text(task + "\n", encoding="utf-8")


def test_auto_is_injected_by_hook_not_burner(burner_env):
    """自动续派在回合结束那一刻由钩子注入，burner 不再抢跑写 next-prompt。"""
    _enable_auto(burner_env)
    result = burner_core.tick()
    assert result["action"] == "idle"
    assert "钩子" in result["reason"]
    assert not (burner_env / "_sup" / "next-prompt.txt").exists()


def test_auto_snapshot_merges_hook_state(burner_env):
    _enable_auto(burner_env)
    hook = {"auto": {"s2": {"rounds": 3, "lastTask": "任务X", "sameTaskStreak": 1,
                            "paused": True, "pausedReason": "连续同一任务",
                            "lastAt": 1790000000, "lastRemaining": 123}}}
    (burner_env / "_sup" / "hook-state.json").write_text(
        json.dumps(hook), encoding="utf-8")
    snap = burner_core.snapshot()
    a = snap["slots"]["s2"]["auto"]
    assert a["rounds"] == 3
    assert a["paused"] is True and a["pausedReason"] == "连续同一任务"
    assert a["lastTask"] == "任务X" and a["lastRemaining"] == 123


def test_auto_task_update_clears_hook_pause(burner_env):
    _enable_auto(burner_env)
    hook = {"auto": {"s2": {"rounds": 2, "paused": True,
                            "pausedReason": "x", "sameTaskStreak": 2, "nudge": 2}}}
    (burner_env / "_sup" / "hook-state.json").write_text(
        json.dumps(hook), encoding="utf-8")
    burner_core.set_auto_task("新任务", slot="s2")
    data = json.loads((burner_env / "_sup" / "hook-state.json").read_text(
        encoding="utf-8"))
    st = data["auto"]["s2"]
    assert st["paused"] is False and st["sameTaskStreak"] == 0 and st["nudge"] == 0
    assert burner_core.snapshot()["slots"]["s2"]["auto"]["task"] == "新任务"


def test_auto_reset_clears_hook_state(burner_env):
    _enable_auto(burner_env)
    hook = {"auto": {"main": {"rounds": 5, "paused": True, "pausedReason": "x",
                              "sameTaskStreak": 2, "nudge": 1}}}
    (burner_env / "_sup" / "hook-state.json").write_text(
        json.dumps(hook), encoding="utf-8")
    burner_core.reset_auto("main")
    data = json.loads((burner_env / "_sup" / "hook-state.json").read_text(
        encoding="utf-8"))
    st = data["auto"]["main"]
    assert st["rounds"] == 0 and st["paused"] is False and st["nudge"] == 0


def test_quota_remaining_excludes_unusable(monkeypatch):
    """失效/停用/耗尽/冷却/关闭的账号即使残留 quota 快照也不计入。"""
    from app.models import Account, Status
    from app.store import store

    def acc(name, status, remaining, enabled=True, cooling=0):
        item = Account(id=name, name=name, provider="zai", mode="jwt")
        item.status = status
        item.enabled = enabled
        item.quota = {"GLM-5.3-Flash": {"remaining": remaining}}
        item.cooling_until = cooling
        return item

    accounts = [
        acc("ok", Status.ACTIVE, 1000),
        acc("invalid", Status.INVALID, 9000),
        acc("disabled", Status.DISABLED, 9000),
        acc("exhausted", Status.EXHAUSTED, 9000),
        acc("cooling", Status.COOLING, 9000, cooling=time.time() + 600),
        acc("off", Status.ACTIVE, 9000, enabled=False),
    ]
    monkeypatch.setattr(store, "list_accounts", lambda: accounts)
    assert burner_core.quota_remaining() == 1000
