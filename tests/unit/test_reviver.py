"""会话唤醒器：空闲/卡死判定、额度闸与总闸（不真正启动 CLI）。"""

from __future__ import annotations

import json
import os
import time

from app import reviver


def test_idle_detection():
    now = time.time()
    # 正常收尾：assistant + finish=stop，静默 180s 即空闲
    assert reviver._idle(
        {"role": "assistant", "finish": "stop", "completed_at": now - 1000}, now)
    # 回合可能还在跑：非 assistant 结尾，静默不足 STUCK_SECONDS 不唤醒
    assert not reviver._idle(
        {"role": "user", "completed_at": now - 100}, now)
    # 非 assistant 结尾且静默超过 STUCK_SECONDS：视为卡死
    assert reviver._idle(
        {"role": "user", "completed_at": now - 1000}, now)
    # assistant 挂着 tool-calls：短静默不唤醒，长静默视为卡死
    assert not reviver._idle(
        {"role": "assistant", "finish": "tool-calls", "completed_at": now - 100}, now)
    assert reviver._idle(
        {"role": "assistant", "finish": "tool-calls", "completed_at": now - 1000}, now)
    # 刚结束的回合不唤醒
    assert not reviver._idle(
        {"role": "assistant", "finish": "stop", "completed_at": now}, now)
    # outcome 明确失败：即使最后一条是 user，也按 IDLE_SECONDS 处理
    assert reviver._idle(
        {"role": "user", "completed_at": now - 200}, now, outcome="error")


def test_pid_alive_is_read_only_and_works():
    # Windows 上旧实现 os.kill(pid, 0) 会 TerminateProcess；这里必须只读探活
    assert reviver._pid_alive(os.getpid()) is True
    assert reviver._pid_alive(0) is False
    assert reviver._pid_alive(-1) is False
    assert os.getpid() > 0


def test_client_patch_status_detects_cap(tmp_path, monkeypatch):
    target = tmp_path / "zcode.cjs"
    target.write_bytes(b"bundle ... f9a=3 ... end")
    monkeypatch.setattr(reviver, "CLIENT_FILE", target)
    monkeypatch.setattr(reviver, "_patch_cache", {"mtime": -1.0, "value": {}})

    status = reviver.client_patch_status()
    assert status["exists"] is True
    assert status["patched"] is False
    assert status["cap"] == 3

    target.write_bytes(b"bundle ... f9a=999 ... end")
    status = reviver.client_patch_status()
    assert status["patched"] is True
    assert status["cap"] == 999


def _flags(tmp_path, monkeypatch, *, continue_on=True):
    flag = tmp_path / "revive.enabled"
    flag.write_text("", encoding="utf-8")
    cont = tmp_path / "continue.enabled"
    if continue_on:
        cont.write_text("", encoding="utf-8")
    monkeypatch.setattr(reviver, "FLAG", flag)
    monkeypatch.setattr(reviver, "CONTINUE_FLAG", cont)
    monkeypatch.setattr(reviver, "STATE_FILE", tmp_path / "revive.json")
    monkeypatch.setattr(reviver, "LOG_FILE", tmp_path / "revive.log")
    monkeypatch.setattr(reviver, "BLOCK_FILE", tmp_path / "model-block.json")
    monkeypatch.setattr(reviver, "_hook_paused", lambda slot: (False, ""))
    monkeypatch.setattr(reviver, "_outside_night", lambda: False)
    monkeypatch.setattr(reviver.burner, "enabled", lambda: True)
    # 通道硬闸默认放行（专测唤醒流程）；需要时用例里再覆盖
    monkeypatch.setattr(reviver, "_lane_evidence", lambda sid: {
        "state": "ok", "channel": "zcode2api/GLM-5.3-Flash",
        "source": "test", "at": 0.0})
    reviver._gate_watch.update(on=None, at=0.0)


def test_gate_notice_logs_closure_and_throttles(monkeypatch):
    """总闸被外部关闭必须留痕：2026-11-04 实证删除后巡检静默 49 分钟无人知晓。"""
    lines: list[str] = []
    monkeypatch.setattr(reviver, "log", lines.append)
    reviver._gate_watch.update(on=None, at=0.0)

    monkeypatch.setattr(reviver, "_now", lambda: 1000.0)
    reviver._gate_notice("")                 # 总闸正常：复位，不打扰
    reviver._gate_notice("续派总闸未开")      # 开→关：立刻留痕
    assert any("刚刚被关闭" in line for line in lines)

    lines.clear()
    reviver._gate_notice("续派总闸未开")      # 持续关闭：10 分钟内不重复刷屏
    assert lines == []

    monkeypatch.setattr(reviver, "_now", lambda: 1700.0)
    reviver._gate_notice("续派总闸未开")      # 超过 10 分钟：低频提醒
    assert any("每 10 分钟提醒" in line for line in lines)

    lines.clear()
    reviver._gate_notice("")                 # 恢复：留痕
    assert any("总闸已恢复" in line for line in lines)
    reviver._gate_notice("续派总闸未开")      # 再关：又是「刚刚被关闭」
    assert any("刚刚被关闭" in line for line in lines)

    lines.clear()
    reviver._gate_watch.update(on=None, at=0.0)
    monkeypatch.setattr(reviver, "_now", lambda: 2000.0)
    reviver._gate_notice("续派总闸未开")      # 启动时即关闭：不冤枉「刚刚」
    assert any("网关启动时即关闭" in line for line in lines)


def test_tick_skips_when_quota_zero(tmp_path, monkeypatch):
    _flags(tmp_path, monkeypatch)
    monkeypatch.setattr(reviver.burner, "monitored_map", lambda: {"sess-a": "main"})
    monkeypatch.setattr(reviver.burner, "quota_remaining", lambda: 0)

    result = reviver.tick()
    assert result["action"] == "idle"
    assert "额度" in result["reason"]


def test_tick_skips_when_continue_disabled(tmp_path, monkeypatch):
    _flags(tmp_path, monkeypatch, continue_on=False)
    result = reviver.tick()
    assert result["action"] == "idle"
    assert "续派总闸" in result["reason"]


def test_tick_wakes_idle_session(tmp_path, monkeypatch):
    _flags(tmp_path, monkeypatch)
    monkeypatch.setattr(reviver.burner, "monitored_map", lambda: {"sess-a": "main"})
    monkeypatch.setattr(reviver.burner, "quota_remaining", lambda: 5000)
    monkeypatch.setattr(reviver.burner, "auto_enabled", lambda: True)
    monkeypatch.setattr(reviver.burner, "prompt_file",
                        lambda slot: tmp_path / "next-prompt.txt")
    task = tmp_path / "auto-task.txt"
    task.write_text("执行下一个任务\n", encoding="utf-8")
    monkeypatch.setattr(reviver.burner, "auto_task_file", lambda slot: task)
    monkeypatch.setattr(reviver.zsession, "get_session",
                        lambda sid: {"directory": "E:/pw", "title": "x",
                                     "outcome": "", "updated_at": 0})
    monkeypatch.setattr(reviver.zsession, "last_message",
                        lambda sid: {"role": "assistant", "finish": "stop",
                                     "completed_at": time.time() - 1000,
                                     "tokens": {"input": 1000}})
    calls = []
    monkeypatch.setattr(reviver, "_wake",
                        lambda slot, sid, directory, reason, state, prompt="继续":
                        calls.append((slot, sid, directory))
                        or {"action": "woke", "pid": 123})

    result = reviver.tick()
    assert result["action"] == "woke"
    assert calls == [("main", "sess-a", "E:/pw")]


def test_tick_staggers_wakes_across_ticks(tmp_path, monkeypatch):
    """五路同时空闲时每周期只唤醒 1 个：2026-11-04 01:26 五路同秒唤醒导致整池 3012。"""
    _flags(tmp_path, monkeypatch)
    mapping = {"sess-a": "main", "sess-b": "s2", "sess-c": "s3",
               "sess-d": "s4", "sess-e": "s5"}
    monkeypatch.setattr(reviver.burner, "monitored_map", lambda: mapping)
    monkeypatch.setattr(reviver.burner, "quota_remaining", lambda: 5000)
    monkeypatch.setattr(reviver.burner, "auto_enabled", lambda: True)
    monkeypatch.setattr(reviver.burner, "prompt_file",
                        lambda slot: tmp_path / "next-prompt.txt")
    task = tmp_path / "auto-task.txt"
    task.write_text("执行下一个任务\n", encoding="utf-8")
    monkeypatch.setattr(reviver.burner, "auto_task_file", lambda slot: task)
    monkeypatch.setattr(reviver.zsession, "get_session",
                        lambda sid: {"directory": "E:/pw", "title": "x",
                                     "outcome": "", "updated_at": 0})
    monkeypatch.setattr(reviver.zsession, "last_message",
                        lambda sid: {"role": "assistant", "finish": "stop",
                                     "completed_at": time.time() - 1000,
                                     "tokens": {"input": 1000}})
    calls = []
    monkeypatch.setattr(reviver, "_wake",
                        lambda slot, sid, directory, reason, state, prompt="继续":
                        calls.append(slot) or {"action": "woke", "pid": 123})
    monkeypatch.setattr(reviver, "WAKE_PER_TICK", 1)
    reviver._wake_rr["last"] = ""

    result = reviver.tick()
    assert calls == ["main"]                     # 一个周期只放一个
    assert [r["action"] for r in result["results"]].count("stagger") == 4

    reviver.tick()                               # 下一周期轮到下一个炉位，不重复宠 main
    assert calls == ["main", "s2"]
    for _ in range(3):
        reviver.tick()
    assert calls == ["main", "s2", "s3", "s4", "s5"]


def test_rotated_starts_after_last_wake():
    mapping = {"a": "main", "b": "s2", "c": "s3"}
    assert [s for _, s in reviver._rotated(mapping, "")] == ["main", "s2", "s3"]
    assert [s for _, s in reviver._rotated(mapping, "main")] == ["s2", "s3", "main"]
    assert [s for _, s in reviver._rotated(mapping, "s3")] == ["main", "s2", "s3"]
    assert [s for _, s in reviver._rotated(mapping, "已下线")] == ["main", "s2", "s3"]
    assert reviver._rotated({"only": "main"}, "x") == [("only", "main")]


def test_tick_rotates_when_context_too_large(tmp_path, monkeypatch):
    """上下文越过阈值必须换新会话：476k 上下文 × 5 路曾把整池打进 3012。"""
    _flags(tmp_path, monkeypatch)
    monkeypatch.setattr(reviver, "SESSIONS_FILE", tmp_path / "continue.sessions")
    monkeypatch.setattr(reviver.burner, "monitored_map", lambda: {"sess-a": "main"})
    monkeypatch.setattr(reviver.burner, "quota_remaining", lambda: 5000)
    monkeypatch.setattr(reviver.burner, "auto_enabled", lambda: True)
    monkeypatch.setattr(reviver.burner, "prompt_file",
                        lambda slot: tmp_path / "next-prompt.txt")
    monkeypatch.setattr(reviver.burner, "auto_task_file",
                        lambda slot: tmp_path / "auto-task.txt")
    monkeypatch.setattr(reviver.burner, "auto_handoff_file",
                        lambda slot: tmp_path / "auto-handoff.md")
    monkeypatch.setattr(reviver.zsession, "get_session",
                        lambda sid: {"directory": "E:/pw", "title": "x",
                                     "outcome": "", "updated_at": 0})
    monkeypatch.setattr(reviver.zsession, "last_message",
                        lambda sid: {"role": "assistant", "finish": "stop",
                                     "completed_at": time.time() - 1000,
                                     "tokens": {"input": 476000}})
    monkeypatch.setattr(reviver.zsession, "context_tokens", lambda sid: 476000)
    monkeypatch.setattr(reviver, "ROTATE_TOKENS", 120000)
    monkeypatch.setattr(reviver, "HEAVY_CONTEXT", 0)   # 本用例只测轮换，不测限并发
    rotated, woken = [], []
    monkeypatch.setattr(reviver, "_rotate_wake",
                        lambda slot, sid, directory, tokens, state: rotated.append(
                            (slot, tokens)) or {"action": "rotate", "pid": 1})
    monkeypatch.setattr(reviver, "_wake",
                        lambda *a, **kw: woken.append(a) or {"action": "woke", "pid": 1})

    result = reviver.tick()
    assert rotated == [("main", 476000)]
    assert woken == []                       # 不再 resume 老会话
    assert result["action"] == "woke"        # rotate 也算本周期动了手


def test_rebind_slot_swaps_only_target_line(tmp_path, monkeypatch):
    f = tmp_path / "continue.sessions"
    f.write_text("sess_old\nsess_b|s2\nsess_c|s3\n", encoding="utf-8")
    monkeypatch.setattr(reviver, "SESSIONS_FILE", f)
    monkeypatch.setattr(reviver.burner, "SLOT_MAIN", "main")

    assert reviver._rebind_slot("s2", "sess_new", "sess_b") is True
    assert f.read_text("utf-8") == "sess_old\nsess_new|s2\nsess_c|s3\n"

    assert reviver._rebind_slot("main", "sess_m2", "sess_old") is True
    assert f.read_text("utf-8").splitlines()[0] == "sess_m2"

    # 名单里没有该会话时补一行，不能悄悄丢掉这个炉位
    assert reviver._rebind_slot("s4", "sess_d", "sess_不存在") is True
    assert "sess_d|s4" in f.read_text("utf-8")


def test_heavy_context_limits_parallel_lanes(tmp_path, monkeypatch):
    """重上下文时同池并行数受限：五路各 40 万同时发，曾把整池打进 3012。"""
    _flags(tmp_path, monkeypatch)
    mapping = {"sess-a": "main", "sess-b": "s2", "sess-c": "s3"}
    monkeypatch.setattr(reviver.burner, "monitored_map", lambda: mapping)
    monkeypatch.setattr(reviver.burner, "quota_remaining", lambda: 5000)
    monkeypatch.setattr(reviver.burner, "auto_enabled", lambda: True)
    monkeypatch.setattr(reviver.burner, "prompt_file",
                        lambda slot: tmp_path / "next-prompt.txt")
    monkeypatch.setattr(reviver.burner, "auto_task_file",
                        lambda slot: tmp_path / "auto-task.txt")
    monkeypatch.setattr(reviver.zsession, "get_session",
                        lambda sid: {"directory": "E:/pw", "title": "x",
                                     "outcome": "", "updated_at": 0})
    monkeypatch.setattr(reviver.zsession, "last_message",
                        lambda sid: {"role": "assistant", "finish": "stop",
                                     "completed_at": time.time() - 1000,
                                     "tokens": {"input": 400000}})
    monkeypatch.setattr(reviver.zsession, "context_tokens", lambda sid: 400000)
    monkeypatch.setattr(reviver, "HEAVY_CONTEXT", 200000)
    monkeypatch.setattr(reviver, "HEAVY_MAX_BUSY", 1)
    monkeypatch.setattr(reviver, "ROTATE_TOKENS", 0)      # 只测限并发
    monkeypatch.setattr(reviver, "WAKE_PER_TICK", 5)
    monkeypatch.setattr(reviver, "_pid_matches", lambda pid: bool(pid))
    reviver._wake_rr["last"] = ""

    # 已经有一路在跑（重上下文）→ 其余全部错峰等待，不再加并发
    state = {"s2": {"pid": 999, "session": "sess-b", "at": 0}}
    monkeypatch.setattr(reviver, "_load_state", lambda: dict(state))
    monkeypatch.setattr(reviver, "_save_state", lambda s: None)
    calls = []
    monkeypatch.setattr(reviver, "_wake",
                        lambda slot, sid, d, r, st, prompt="继续":
                        calls.append(slot) or {"action": "woke", "pid": 1})
    reviver.tick()
    assert calls == []                       # 三路全是重上下文、已有 1 路在跑

    # 没有人在跑时可以放一路进去
    monkeypatch.setattr(reviver, "_load_state", lambda: {})
    calls.clear()
    reviver.tick()
    assert calls == ["main"]


def test_tick_respects_slot_stop(tmp_path, monkeypatch):
    _flags(tmp_path, monkeypatch)
    task = tmp_path / "auto-task.txt"
    task.write_text("STOP\n", encoding="utf-8")
    monkeypatch.setattr(reviver.burner, "monitored_map", lambda: {"sess-a": "main"})
    monkeypatch.setattr(reviver.burner, "quota_remaining", lambda: 5000)
    monkeypatch.setattr(reviver.burner, "auto_enabled", lambda: True)
    monkeypatch.setattr(reviver.burner, "prompt_file",
                        lambda slot: tmp_path / "next-prompt.txt")
    monkeypatch.setattr(reviver.burner, "auto_task_file", lambda slot: task)
    monkeypatch.setattr(reviver, "_wake",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("STOP 炉位不应唤醒")))

    result = reviver.tick()
    assert result["action"] == "idle"
    assert result["results"][0]["reason"] == "该炉位已写 STOP"


def test_tick_skips_outside_night(tmp_path, monkeypatch):
    _flags(tmp_path, monkeypatch)
    monkeypatch.setattr(reviver, "_outside_night", lambda: True)
    task = tmp_path / "auto-task.txt"
    task.write_text("继续\n", encoding="utf-8")
    monkeypatch.setattr(reviver.burner, "monitored_map", lambda: {"sess-a": "main"})
    monkeypatch.setattr(reviver.burner, "quota_remaining", lambda: 5000)
    monkeypatch.setattr(reviver.burner, "auto_enabled", lambda: True)
    monkeypatch.setattr(reviver.burner, "prompt_file",
                        lambda slot: tmp_path / "next-prompt.txt")
    monkeypatch.setattr(reviver.burner, "auto_task_file", lambda slot: task)
    monkeypatch.setattr(reviver, "_wake",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("白天不应唤醒")))

    result = reviver.tick()
    assert result["action"] == "idle"
    assert result["results"][0]["reason"] == "白天停派（09:00–23:00）"


def test_tick_kills_hung_wake_process(tmp_path, monkeypatch):
    _flags(tmp_path, monkeypatch)
    (tmp_path / "revive.json").write_text(json.dumps(
        {"main": {"at": time.time() - 100, "pid": 4321}}), encoding="utf-8")
    task = tmp_path / "auto-task.txt"
    task.write_text("继续\n", encoding="utf-8")
    monkeypatch.setattr(reviver.burner, "monitored_map", lambda: {"sess-a": "main"})
    monkeypatch.setattr(reviver.burner, "quota_remaining", lambda: 5000)
    monkeypatch.setattr(reviver.burner, "auto_enabled", lambda: True)
    monkeypatch.setattr(reviver.burner, "prompt_file",
                        lambda slot: tmp_path / "next-prompt.txt")
    monkeypatch.setattr(reviver.burner, "auto_task_file", lambda slot: task)
    monkeypatch.setattr(reviver.zsession, "get_session", lambda sid: {
        "directory": "E:/pw", "outcome": "", "updated_at": time.time() - 7200})
    monkeypatch.setattr(reviver.zsession, "last_message", lambda sid: {
        "role": "assistant", "finish": "tool-calls",
        "completed_at": time.time() - 7200, "tokens": {"input": 1000}})
    monkeypatch.setattr(reviver, "_pid_matches", lambda pid: pid == 4321)
    killed = []
    monkeypatch.setattr(reviver, "_terminate",
                        lambda pid: killed.append(pid) or True)

    result = reviver.tick()
    assert killed == [4321]
    assert result["results"][0]["action"] == "killed"
    assert reviver._load_state()["main"]["pid"] == 0


# ── 模型通道硬闸（2026-11-05 事故：轮换会话继承 GUI 模型 hy4-preview，5h 烧 ¥462）──

def test_channel_allow_list_only_gateway():
    assert reviver._allowed_channel("zcode2api/GLM-5.3-Flash")
    assert reviver._allowed_channel("zcode2api/GLM-5.3")
    assert not reviver._allowed_channel("wb-proxy-cn/hy4-preview")
    assert not reviver._allowed_channel("wb-proxy-cn/deepseek-v4.1-flash")
    assert not reviver._allowed_channel("zai/glm-4.6")
    assert not reviver._allowed_channel("")


def test_channel_name_accepts_db_and_usage_shapes():
    # 会话库：provider_id=magpie + 已带前缀的 model_id
    assert reviver._channel_name("magpie", "zcode2api/GLM-5.3-Flash") == \
        "zcode2api/GLM-5.3-Flash"
    # magpie usage：provider + 裸模型名
    assert reviver._channel_name("zcode2api", "GLM-5.3-Flash") == \
        "zcode2api/GLM-5.3-Flash"
    assert reviver._channel_name("wb-proxy-cn", "hy4-preview") == \
        "wb-proxy-cn/hy4-preview"


def test_lane_evidence_reads_session_db(monkeypatch):
    monkeypatch.setattr(reviver.zsession, "last_model", lambda sid: {
        "provider": "wb-proxy-cn", "model": "hy4-preview", "at": 1.0})
    ev = reviver._lane_evidence("sess_b8c68df3")
    assert ev["state"] == "wrong"
    assert ev["channel"] == "wb-proxy-cn/hy4-preview"

    monkeypatch.setattr(reviver.zsession, "last_model", lambda sid: {
        "provider": "magpie", "model": "zcode2api/GLM-5.3-Flash", "at": 2.0})
    ev = reviver._lane_evidence("sess_b8c68df3")
    assert ev["state"] == "ok"

    # 三条证据都没有 → unknown（不是放行）
    monkeypatch.setattr(reviver.zsession, "last_model", lambda sid: None)
    monkeypatch.setattr(reviver, "_usage_rows", lambda limit=300: [])
    monkeypatch.setattr(reviver, "_route_for", lambda sid: {})
    assert reviver._lane_evidence("sess_none")["state"] == "unknown"


def _guard_env(tmp_path, monkeypatch, evidence, *, age=300, session="sess-a"):
    _flags(tmp_path, monkeypatch)
    blocks = tmp_path / "model-block.json"
    sessions = tmp_path / "continue.sessions"
    sessions.write_text("sess-a\nsess-b|s2\n", encoding="utf-8")
    monkeypatch.setattr(reviver, "BLOCK_FILE", blocks)
    monkeypatch.setattr(reviver, "SESSIONS_FILE", sessions)
    monkeypatch.setattr(reviver.burner, "SLOT_MAIN", "main")
    monkeypatch.setattr(reviver.burner, "monitored_map",
                        lambda: {"sess-a": "main", "sess-b": "s2"})
    monkeypatch.setattr(reviver, "_pid_matches", lambda pid: pid == 123)
    state = {"main": {"at": time.time() - age, "pid": 123, "session": session,
                      "reason": "空闲超时", "dir": "E:/pw"}}
    monkeypatch.setattr(reviver, "_load_state", lambda: dict(state))
    saved = {}
    monkeypatch.setattr(reviver, "_save_state", lambda s: saved.update(s))
    monkeypatch.setattr(reviver, "_lane_evidence", lambda sid: evidence)
    killed = []
    monkeypatch.setattr(reviver, "_terminate",
                        lambda pid: killed.append(pid) or True)
    return blocks, sessions, killed, saved


def test_guard_blocks_wrong_channel_immediately(tmp_path, monkeypatch):
    """只要实际通道不是网关：杀进程 + 摘监听 + 全池封锁，不看原因。"""
    blocks, sessions, killed, saved = _guard_env(
        tmp_path, monkeypatch,
        {"state": "wrong", "channel": "wb-proxy-cn/hy4-preview",
         "source": "session-db"})

    result = reviver.guard_tick()
    assert killed == [123]
    assert result["results"][0]["action"] == "blocked"
    data = json.loads(blocks.read_text(encoding="utf-8"))
    assert data["locked"] is True
    assert data["slots"]["main"]["channel"] == "wb-proxy-cn/hy4-preview"
    assert "sess-a" not in sessions.read_text(encoding="utf-8")
    assert "sess-b|s2" in sessions.read_text(encoding="utf-8")
    assert saved["main"]["pid"] == 0

    # 封锁后唤醒巡检也不再启动任何会话
    monkeypatch.setattr(reviver.burner, "quota_remaining", lambda: 5000)
    assert reviver.tick()["action"] == "idle"
    assert "封锁" in reviver.tick()["reason"]


def test_guard_waits_grace_then_kills_unknown(tmp_path, monkeypatch):
    """查不到通道不是放行：宽限期内等，超时即停机。"""
    _guard_env(tmp_path, monkeypatch,
               {"state": "unknown", "channel": "", "source": "no-evidence"},
               age=10)
    assert reviver.guard_tick()["results"][0]["action"] == "pending"

    blocks, _, killed, saved = _guard_env(
        tmp_path, monkeypatch,
        {"state": "unknown", "channel": "", "source": "no-evidence"},
        age=300)
    reviver.guard_tick()
    assert killed == [123]
    data = json.loads(blocks.read_text(encoding="utf-8"))
    assert data.get("locked") is not True          # 第一次只是终止+冷却
    assert data["unknown"]["main"]["n"] == 1
    assert saved["main"]["pid"] == 0


def test_unblock_clears_lock_without_rebinding(tmp_path, monkeypatch):
    blocks, sessions, _, _ = _guard_env(
        tmp_path, monkeypatch,
        {"state": "wrong", "channel": "wb-proxy-cn/hy4-preview",
         "source": "session-db"})
    reviver.guard_tick()
    assert "sess-a" not in sessions.read_text(encoding="utf-8")

    reviver.unblock()
    data = json.loads(blocks.read_text(encoding="utf-8"))
    assert data["locked"] is False
    assert data["slots"] == {}
    # 已知走错通道的会话不能自动还回监听名单
    assert "sess-a" not in sessions.read_text(encoding="utf-8")


def test_guard_blocks_gui_run_lane_without_pid(tmp_path, monkeypatch):
    """用户自己在 GUI 里跑的监听会话，最近上过非网关通道也要摘监听。"""
    blocks, sessions, killed, _ = _guard_env(
        tmp_path, monkeypatch,
        {"state": "wrong", "channel": "wb-proxy-cn/hy4-preview",
         "source": "session-db", "at": time.time() - 60})
    monkeypatch.setattr(reviver, "_load_state", lambda: {})   # 不是唤醒器起的

    result = reviver.guard_tick()
    assert killed == []                     # 没有进程可杀，但监听必须摘
    assert any(x["action"] == "blocked" for x in result["results"])
    data = json.loads(blocks.read_text(encoding="utf-8"))
    assert data["locked"] is True
    assert "sess-a" not in sessions.read_text(encoding="utf-8")


def test_audit_blocks_wrong_binding_at_startup(tmp_path, monkeypatch):
    """启动审计不看总闸：绑定到 hy4 历史的会话必须立刻摘掉。"""
    _flags(tmp_path, monkeypatch)
    sessions = tmp_path / "continue.sessions"
    sessions.write_text("sess-a\nsess-b|s2\n", encoding="utf-8")
    monkeypatch.setattr(reviver, "SESSIONS_FILE", sessions)
    monkeypatch.setattr(reviver, "BLOCK_FILE", tmp_path / "model-block.json")
    monkeypatch.setattr(reviver.burner, "SLOT_MAIN", "main")
    monkeypatch.setattr(reviver.burner, "monitored_map",
                        lambda: {"sess-a": "main", "sess-b": "s2"})
    monkeypatch.setattr(reviver, "_load_state", lambda: {})
    monkeypatch.setattr(reviver, "_save_state", lambda s: None)
    monkeypatch.setattr(reviver, "_lane_evidence", lambda sid: {
        "state": "wrong" if sid == "sess-a" else "ok",
        "channel": "wb-proxy-cn/hy4-preview", "source": "session-db"})

    out = reviver.audit_bindings()
    assert [item["action"] for item in out["results"]] == ["blocked", "ok"]
    text = sessions.read_text(encoding="utf-8")
    assert "sess-a" not in text
    assert "sess-b|s2" in text
