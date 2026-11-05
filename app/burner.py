"""任务工单队列与续派状态机（从 zburn-hub 合并进来的纯逻辑层）。

工单通过 Stop 钩子的文件协议派发：写 `_sup/next-prompt.txt`，钩子在一个回合
结束后一次性消费并归档；本模块按 watch 文件变化 + verify 命令做验收。

**多炉位（slot）**：一个会话绑一个炉位（continue.sessions 写 `sess_xxx|s2`，
不带 `|` 即 main），每个炉位有独立的待发指令（main 用 next-prompt.txt，其余
`next-prompt.<slot>.txt`）、独立在途状态与独立自动派单（auto-task/auto-handoff
按炉位分文件）。5 会话 = 5 炉位并行，互不抢单。

状态机（每个工单）：
    queued ──派单(写 next-prompt.<slot>.txt)──▶ dispatched ──验收通过──▶ done
       ▲                                    │
       └────── tries 未超限，重新排队 ◀──验收失败──

派单时机：该炉位的指令文件不存在 = 上个工单的回合已结束（钩子归档了文件），
此时才允许派下一单；文件存在 = 上一单还在执行中，只做验收不动派单。
总闸：_sup/continue.enabled 不存在时一切不动（与 Stop 钩子同一开关）。
监听名单：炉位没有绑定会话时不派单——钩子只续派名单里的会话（已派出的
在途工单仍正常验收）。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import logs, settings

# Windows 下跑验收命令不弹黑框
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 路径做成模块级常量：单测直接 monkeypatch 到 tmp_path，不碰真实 data/
SUP_DIR: Path = Path(settings.SUP_DIR)
DIR: Path = Path(settings.DATA_DIR) / "burner"
QUEUE_FILE: Path = DIR / "queue.json"
STATE_FILE: Path = DIR / "state.json"
PROMPT_FILE: Path = SUP_DIR / "next-prompt.txt"
ENABLE_FLAG: Path = SUP_DIR / "continue.enabled"
SESSIONS_FILE: Path = SUP_DIR / "continue.sessions"

# 额度自动派单：剩余额度高于阈值时，自动生成「先总结、再执行下一项」的工单。
# 下一项任务由 agent 每轮写回 auto-task.txt；写 STOP / 留空即暂停。
AUTO_TASK_FILE: Path = SUP_DIR / "auto-task.txt"
AUTO_HANDOFF_FILE: Path = SUP_DIR / "auto-handoff.md"
AUTO_DEFAULT_MIN = 1_000      # 剩余额度低于此值就停（单位与上游 quota 一致）
AUTO_DEFAULT_ROUNDS = 100_000           # 一次开启最多自动派多少轮（防失控）
AUTO_DEFAULT_SAME = 3              # 连续多少轮拿到同一任务就暂停（防原地打转）

VERIFY_TIMEOUT = 600        # 单条验收命令上限（秒）
_MAX_NOTE = 400             # note 截断，防止验证器输出把状态文件撑爆
TICK_INTERVAL = 10.0        # 后台轮询间隔（秒）

SLOT_MAIN = "main"


def normalize_slot(slot: str | None) -> str:
    """炉位名：main / s2…s5 / 自定义短词；非法字符一律落回 main。"""
    raw = str(slot or SLOT_MAIN).strip().lower()
    if not raw or raw == SLOT_MAIN:
        return SLOT_MAIN
    cleaned = "".join(c if (c.isalnum() and c.isascii()) or c in "-_" else ""
                      for c in raw)[:12]
    return cleaned or SLOT_MAIN


def prompt_file(slot: str = SLOT_MAIN) -> Path:
    return PROMPT_FILE if slot == SLOT_MAIN else SUP_DIR / f"next-prompt.{slot}.txt"


def auto_task_file(slot: str = SLOT_MAIN) -> Path:
    return AUTO_TASK_FILE if slot == SLOT_MAIN else SUP_DIR / f"auto-task.{slot}.txt"


def auto_handoff_file(slot: str = SLOT_MAIN) -> Path:
    return AUTO_HANDOFF_FILE if slot == SLOT_MAIN else SUP_DIR / f"auto-handoff.{slot}.md"


def turn_end_file(slot: str = SLOT_MAIN) -> Path:
    """Stop 钩子每结束一个回合就刷新它；自动验收靠它判断「回合真的结束了」。"""
    return SUP_DIR / f"turn-end.{slot}"


TURN_WAIT_MAX = 4 * 3600       # 注入后等回合结束的上限；超时按验收失败处理
TURN_WAIT_LEGACY = 120         # 没有 turn-end 标记（旧钩子/未启用）时给的宽限期


_lock = threading.RLock()


# ── 原子写 ──────────────────────────────────────────────────────────────────
def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _emit(kind: str, msg: str) -> None:
    """统一日志出口：走网关日志台账（面板「网关日志」可查）。"""
    fn = {"info": logs.info, "ok": logs.ok, "warn": logs.warn, "err": logs.err}.get(
        kind, logs.info)
    fn("burner", msg)


def _now() -> float:
    return time.time()


# ── 队列读写 ────────────────────────────────────────────────────────────────
def _load_queue() -> list[dict]:
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_queue(orders: list[dict]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(QUEUE_FILE, json.dumps(orders, ensure_ascii=False, indent=1))


def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except Exception:
        data = {}
    # 迁移：v2.17.0 之前的单炉位状态（dispatchedId/baseline/auto 在顶层）→ slots.main
    if "slots" not in data:
        legacy = {k: data.pop(k) for k in ("dispatchedId", "baseline", "auto")
                  if k in data}
        data["slots"] = {SLOT_MAIN: legacy} if legacy else {}
    return data


def _slot_state(state: dict, slot: str) -> dict:
    """取某炉位的在途状态（dispatchedId/baseline/auto），没有就建默认。"""
    slots = state.setdefault("slots", {})
    st = slots.get(slot)
    if not isinstance(st, dict):
        st = {}
        slots[slot] = st
    st.setdefault("dispatchedId", "")
    st.setdefault("baseline", {})
    if not isinstance(st.get("auto"), dict):
        st["auto"] = {}
    return st


def _save_state(state: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=1))


# ── 工单构造 ────────────────────────────────────────────────────────────────
def enqueue(title: str, prompt: str, watch: list[str] | None = None,
            verify_cmd: str = "", cwd: str = "", retries: int = 1,
            auto: bool = False, slot: str = SLOT_MAIN) -> dict:
    """入队一个工单。id/标题/prompt 必填。auto=True 表示额度自动派单生成。"""
    title = (title or "").strip()
    prompt = (prompt or "").strip()
    if not title:
        raise ValueError("工单标题不能为空")
    if not prompt:
        raise ValueError("工单指令不能为空")
    order = {
        "id": "WO-" + uuid.uuid4().hex[:8],
        "title": title,
        "prompt": prompt,
        "watch": [str(Path(w)) for w in (watch or []) if str(w).strip()],
        "verify_cmd": (verify_cmd or "").strip(),
        "cwd": (cwd or "").strip(),
        "retries": max(0, int(retries or 0)),
        "tries": 0,
        "status": "queued",
        "auto": bool(auto),
        "slot": normalize_slot(slot),
        "note": "",
        "at": _now(),
        "dispatchedAt": 0.0,
        "doneAt": 0.0,
    }
    with _lock:
        orders = _load_queue()
        orders.append(order)
        _save_queue(orders)
    _emit("info", f"入队 {order['id']} {title}")
    return order


def _find(orders: list[dict], order_id: str) -> dict | None:
    for o in orders:
        if o.get("id") == order_id:
            return o
    return None


def _maybe_remove_pending_prompt(order: dict) -> bool:
    """取消/重置在途工单时，若其指令还没被钩子消费（文件还在且内容相同），
    删掉它，防止已取消的活儿在回合结束仍被执行。返回是否删了。"""
    pfile = prompt_file(normalize_slot(order.get("slot")))
    try:
        if pfile.exists() and pfile.read_text(
                encoding="utf-8").strip() == order.get("prompt", "").strip():
            pfile.unlink()
            return True
    except Exception as err:
        _emit("warn", f"清理待发指令失败: {err}")
    return False


def cancel(order_id: str) -> dict:
    with _lock:
        orders = _load_queue()
        o = _find(orders, order_id)
        if not o:
            raise ValueError(f"工单不存在: {order_id}")
        if o["status"] in ("done", "cancelled"):
            return o
        o["status"] = "cancelled"
        o["note"] = "人工取消"
        o["doneAt"] = _now()
        _save_queue(orders)
        state = _load_state()
        st = _slot_state(state, normalize_slot(o.get("slot")))
        if st.get("dispatchedId") == order_id:
            st["dispatchedId"] = ""
            st["baseline"] = {}
            _save_state(state)
            if _maybe_remove_pending_prompt(o):
                _emit("info", f"已撤回未消费的待发指令（{order_id}）")
        _emit("info", f"取消 {order_id}")
        return o


def retry(order_id: str) -> dict:
    """把 done/failed/cancelled 的工单重置回 queued（tries 清零）。"""
    with _lock:
        orders = _load_queue()
        o = _find(orders, order_id)
        if not o:
            raise ValueError(f"工单不存在: {order_id}")
        o["status"] = "queued"
        o["tries"] = 0
        o["note"] = "人工重试"
        o["doneAt"] = 0.0
        _save_queue(orders)
        state = _load_state()
        st = _slot_state(state, normalize_slot(o.get("slot")))
        if st.get("dispatchedId") == order_id:
            st["dispatchedId"] = ""
            st["baseline"] = {}
            _save_state(state)
            if _maybe_remove_pending_prompt(o):
                _emit("info", f"已撤回未消费的待发指令（{order_id}）")
        _emit("info", f"重试 {order_id}")
        return o


def clear_done() -> int:
    with _lock:
        orders = _load_queue()
        keep = [o for o in orders if o["status"] not in ("done", "cancelled")]
        removed = len(orders) - len(keep)
        if removed:
            _save_queue(keep)
            _emit("info", f"清理已完成工单 {removed} 条")
        return removed


# ── 验收 ────────────────────────────────────────────────────────────────────
def _run_verify(cmd: str, cwd: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd or None, capture_output=True,
            text=True, errors="replace", timeout=VERIFY_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-_MAX_NOTE:]
        return proc.returncode == 0, tail.strip() or f"exit={proc.returncode}"
    except subprocess.TimeoutExpired:
        return False, f"验收超时（>{VERIFY_TIMEOUT}s）"
    except Exception as err:  # 命令本身起不来
        return False, f"验收命令异常: {err}"


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# ── 状态机主步 ──────────────────────────────────────────────────────────────
def enabled() -> bool:
    return ENABLE_FLAG.exists()


def monitored_map() -> dict[str, str]:
    """监听名单 → {session_id: slot}。行格式：`sess_xxx`（main）或 `sess_xxx|s2`。"""
    out: dict[str, str] = {}
    try:
        lines = SESSIONS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        sid, _, slot = ln.partition("|")
        sid = sid.strip()
        if sid:
            out.setdefault(sid, normalize_slot(slot))
    return out


def monitored_sessions(slot: str = SLOT_MAIN) -> list[str]:
    """某个炉位绑定的会话 id 列表。"""
    return [sid for sid, s in monitored_map().items() if s == slot]


def active_slots(state: dict, orders: list[dict]) -> list[str]:
    """本轮 tick 需要推进的炉位 = 状态里有在途的 ∪ 队列里有的 ∪ 名单里绑了的。"""
    slots = {SLOT_MAIN}
    slots.update(state.get("slots", {}).keys())
    for o in orders:
        if o.get("status") in ("queued", "dispatched"):
            slots.add(normalize_slot(o.get("slot")))
    for slot in monitored_map().values():
        slots.add(slot)
    if SLOT_MAIN in slots:
        return [SLOT_MAIN] + sorted(s for s in slots if s != SLOT_MAIN)
    return sorted(slots)


# ── 额度自动派单 ────────────────────────────────────────────────────────────
def auto_enabled() -> bool:
    return (SUP_DIR / "auto.enabled").exists()


def _auto_config() -> dict:
    """自动派单参数（burner/auto.json）；缺字段用默认值，坏文件不阻断队列。"""
    defaults = {"minRemaining": AUTO_DEFAULT_MIN, "maxRounds": AUTO_DEFAULT_ROUNDS,
                "maxSameTask": AUTO_DEFAULT_SAME}
    try:
        raw = json.loads((DIR / "auto.json").read_text(encoding="utf-8"))
    except Exception:
        return defaults
    if not isinstance(raw, dict):
        return defaults
    for key in defaults:
        try:
            defaults[key] = max(0, int(raw.get(key, defaults[key])))
        except (TypeError, ValueError):
            pass
    return defaults


def set_auto_config(min_remaining: int | None = None, max_rounds: int | None = None,
                    max_same_task: int | None = None) -> dict:
    cfg = _auto_config()
    if min_remaining is not None:
        cfg["minRemaining"] = max(0, int(min_remaining))
    if max_rounds is not None:
        cfg["maxRounds"] = max(1, int(max_rounds))
    if max_same_task is not None:
        cfg["maxSameTask"] = max(1, int(max_same_task))
    _atomic_write(DIR / "auto.json", json.dumps(cfg, ensure_ascii=False, indent=1))
    return cfg


def _auto_task(slot: str = SLOT_MAIN) -> str:
    try:
        return auto_task_file(slot).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_auto_state(state: dict, slot: str = SLOT_MAIN) -> dict:
    return _slot_state(state, slot)["auto"]


def quota_remaining() -> int:
    """所有**当前可用**账号的剩余额度合计（单位与上游 quota 一致）。

    只统计 enabled 且状态可服务的账号：失效/停用/已耗尽/冷却中的账号即使
    残留旧 quota 快照也不计入，避免 5 路一直去撞一个已经不能出字的号。
    """
    from .models import Status
    from .store import store

    total = 0
    for account in store.list_accounts():
        if not account.enabled:
            continue
        if account.status in (Status.INVALID, Status.DISABLED, Status.EXHAUSTED):
            continue
        if account.is_cooling():
            continue
        for quota in (account.quota or {}).values():
            try:
                total += int((quota or {}).get("remaining") or 0)
            except (TypeError, ValueError):
                continue
    return total


def _auto_prompt(task: str, slot: str = SLOT_MAIN) -> str:
    return (
        "先做交接总结：把上一阶段已完成的工作、当前状态、遗留问题写入 "
        f"{auto_handoff_file(slot)}（覆盖写，最多 20 行；不要贴代码、不要写客套话）。\n"
        "然后执行下面这项任务：\n"
        f"{task}\n\n"
        "完成后：\n"
        f"1. 把下一项任务写进 {auto_task_file(slot)}（只写任务本身，不要解释）；\n"
        "2. 如果当前轮次计划的任务清单已全部完成，不要写 STOP：先按同样格式起草下一轮计划"
        "（同目录按轮次递增命名：轨道N-第2轮.md、第3轮…），再把下一轮第 1 项写回 "
        f"{auto_task_file(slot)}；只有确实没有任何可做项时才写 STOP。\n"
        "3. 总控文件绝不能碰：data/_sup 下的 continue.enabled、continue.sessions、"
        "hook-state.json、next-prompt*.txt 一律不创建/不修改/不删除"
        "（2026-11-04 实证：总闸被删后全线停摆 49 分钟）；"
        f"你只能写本炉位的 {auto_task_file(slot)} 与 {auto_handoff_file(slot)}。\n"
        "本任务由你本人完成，不要指派子代理（subagent），也不要 fork 新会话。\n"
        "本回合到此停下：不要提问、不要等待确认。"
    )


def _auto_dispatch(orders: list[dict], state: dict, slot: str,
                   prompt_exists: bool,
                   remaining: int | None = None) -> dict | None:
    """某炉位的额度自动派单一步：返回结果 dict；None = 不归它管。

    只在「该炉位没有 queued/dispatched、没有待发指令」时接管，用户手排的
    工单永远优先。任务池为空 / STOP / 额度不足 / 轮次用尽 / 连续同一任务
    都会暂停并给出原因。
    """
    if not auto_enabled():
        return None
    if prompt_exists:
        return {"action": "wait", "reason": "待发指令尚未被消费，自动派单等待"}
    if not monitored_sessions(slot):
        return {"action": "idle", "reason": f"炉位 {slot} 没有绑定的监听会话"}
    auto = _read_auto_state(state, slot)
    if auto.get("paused"):
        return {"action": "idle",
                "reason": f"自动派单已暂停：{auto.get('pausedReason') or '需要手动复位'}"}

    task = _auto_task(slot)
    if not task:
        return {"action": "idle", "reason": "auto-task 为空：没有下一步任务"}
    if task.strip().upper() == "STOP":
        return {"action": "idle", "reason": "agent 已写 STOP：自动派单结束"}

    cfg = _auto_config()
    rounds = int(auto.get("rounds") or 0)
    if rounds >= cfg["maxRounds"]:
        return {"action": "idle",
                "reason": f"自动派单已达上限 {cfg['maxRounds']} 轮（可在页面复位）"}

    last_task = str(auto.get("lastTask") or "").strip()
    streak = int(auto.get("sameTaskStreak") or 0)
    if last_task and task == last_task:
        streak += 1
    else:
        streak = 0
    if streak >= cfg["maxSameTask"]:
        auto["paused"] = True
        auto["pausedReason"] = f"连续 {streak + 1} 轮拿到同一任务，疑似原地打转"
        _save_state(state)
        return {"action": "idle", "reason": auto["pausedReason"]}

    if remaining is None:
        remaining = quota_remaining()
    if remaining <= cfg["minRemaining"]:
        return {"action": "idle",
                "reason": f"剩余额度 {remaining} <= 阈值 {cfg['minRemaining']}，自动派单停止"}

    title = f"[{slot}] " + " ".join(task.splitlines()[:1])[:40]
    order = enqueue(title, _auto_prompt(task, slot),
                    watch=[str(auto_handoff_file(slot))],
                    retries=0, auto=True, slot=slot)
    state_auto = _read_auto_state(state, slot)
    state_auto.update({
        "rounds": rounds + 1,
        "lastTask": task,
        "sameTaskStreak": streak,
        "lastAt": _now(),
        "lastRemaining": remaining,
        "paused": False,
        "pausedReason": "",
    })
    _save_state(state)
    _emit("info", f"自动派单[{slot}] 第 {rounds + 1} 轮：{title}（剩余额度 {remaining}）")
    orders = _load_queue()
    created = _find(orders, order["id"])
    if not created:
        return {"action": "error", "reason": "自动派单入队后找不到工单"}
    return _dispatch(created, orders, state)


def tick(force: bool = False) -> dict:
    """状态机一步：推进**所有炉位**（main 优先，其余按名排序）。

    返回聚合结果：action = 多炉位里有实质动作的个数（multi>1），单炉位时
    就是该炉位的动作；`results` 带每个炉位的明细，后台轮询与「立即派单」
    都走这里（force=忽略"指令未消费"直接尝试派单）。
    """
    with _lock:
        if not enabled():
            return {"action": "idle", "reason": "总闸未开"}

        orders = _load_queue()
        state = _load_state()
        results: list[dict] = []

        for slot in active_slots(state, orders):
            st = _slot_state(state, slot)
            pfile = prompt_file(slot)
            prompt_exists = pfile.exists()

            # 1) 有在途工单：先做验收
            dispatched_id = st.get("dispatchedId", "")
            if dispatched_id:
                o = _find(orders, dispatched_id)
                if o and o["status"] == "dispatched":
                    results.append(_accept(o, orders, state, slot, prompt_exists))
                    continue
                # 状态不一致（被人工动过）：清掉在途标记
                st["dispatchedId"] = ""
                _save_state(state)

            # 2) 无在途：prompt 还在（上个工单的指令没被钩子消费）→ 不派新单
            if prompt_exists and not force:
                results.append({"slot": slot, "action": "wait",
                                "reason": "上一条指令尚未被消费"})
                continue

            # 3) 派下一单：用户手排的 queued 工单优先；没有就交给额度自动派单
            nxt = next((o for o in orders if o["status"] == "queued"
                        and normalize_slot(o.get("slot")) == slot), None)
            if nxt:
                if not monitored_sessions(slot):
                    results.append({"slot": slot, "action": "idle",
                                    "reason": f"炉位 {slot} 没有绑定的监听会话，未派单"})
                    continue
                results.append(_dispatch(nxt, orders, state))
                continue
            # 自动续派由 Stop 钩子在回合结束那一刻直接注入（无空档、不会
            # 出现"后台派了单但会话已停"的竞态）；这里只负责手排队列。
            results.append({"slot": slot, "action": "idle", "reason": "队列空（自动续派由钩子负责）"})

        meaningful = [r for r in results
                      if r.get("action") not in ("idle",)]
        if len(meaningful) > 1:
            return {"action": "multi", "n": len(meaningful), "results": results}
        if len(meaningful) == 1:
            return {**meaningful[0], "results": results}
        first = results[0] if results else {}
        return {"action": "idle",
                "reason": first.get("reason", "队列空"),
                "slot": first.get("slot", SLOT_MAIN),
                "results": results}


def _dispatch(order: dict, orders: list[dict], state: dict) -> dict:
    slot = normalize_slot(order.get("slot"))
    pfile = prompt_file(slot)
    pfile.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(pfile, order["prompt"] + "\n")
    order["status"] = "dispatched"
    order["dispatchedAt"] = _now()
    order["note"] = ""
    st = _slot_state(state, slot)
    st["dispatchedId"] = order["id"]
    st["dispatchedAt"] = _now()
    st["baseline"] = {w: _mtime(w) for w in order.get("watch", [])}
    _save_queue(orders)
    _save_state(state)
    _emit("ok", f"派单[{slot}] {order['id']} {order['title']}（写入 {pfile.name}）")
    return {"slot": slot, "action": "dispatched",
            "id": order["id"], "title": order["title"]}


def prompt_consumed_at(slot: str = SLOT_MAIN) -> float:
    """该炉位最近一次指令被钩子消费的时刻（钩子写的 `prompt-consumed.<slot>`）。

    钩子在 Stop 事件里先写 turn-end、再消费 next-prompt；如果指令是在唤醒器
    的“继续”回合被消费的，这个 Stop 的 turn-end 只能证明“指令已取走”，
    不能证明“任务已执行”。验收必须等到这之后又出现一个 Stop。
    """
    path = SUP_DIR / f"prompt-consumed.{slot}"
    try:
        return float((path.read_text(encoding="utf-8") or "").strip()) / 1000.0
    except (OSError, ValueError):
        return 0.0


def _accept(order: dict, orders: list[dict], state: dict, slot: str,
            prompt_exists: bool) -> dict:
    """在途工单验收：watch 文件有变化才跑 verify；通过=done，失败按 tries 重排。"""
    st = _slot_state(state, slot)
    baseline = st.get("baseline", {}) or {}
    changed = any(_mtime(w) > (baseline.get(w) or 0.0) for w in order.get("watch", []))

    if prompt_exists and not changed:
        # 指令还没被消费、watch 也没动：工单仍在执行，不打扰
        return {"slot": slot, "action": "running", "id": order["id"]}

    # 指令被消费 ≠ 任务已执行：注入发生在 Stop 之后的下一个回合开始时。
    # 用归档文件的 mtime 作为消费时刻——必须等这之后又出现一个 Stop 才验收，
    # 否则会把“唤醒器发的继续回合”误判成任务回合（刚消费就判失败）。
    dispatched_at = float(st.get("dispatchedAt") or order.get("dispatchedAt") or 0.0)
    consumed_at = max(prompt_consumed_at(slot), dispatched_at)
    turn_end = _mtime(str(turn_end_file(slot)))
    waited = _now() - dispatched_at if dispatched_at else TURN_WAIT_MAX + 1
    if turn_end > 0 and turn_end <= consumed_at and waited < TURN_WAIT_MAX:
        return {"slot": slot, "action": "running", "id": order["id"],
                "reason": "等待执行回合结束（指令已在上一回合消费）"}
    if turn_end <= 0 and waited < TURN_WAIT_LEGACY:
        # 旧钩子没有 turn-end 标记：先给 2 分钟宽限，避免刚注入就误判失败
        return {"slot": slot, "action": "running", "id": order["id"],
                "reason": "等待回合结束（无 turn-end 标记，宽限中）"}

    if not changed:
        # 回合已结束但 watch 无产出：记一次失败
        passed, note = False, "回合结束但 watch 文件无变化"
    elif order.get("verify_cmd"):
        passed, note = _run_verify(order["verify_cmd"], order.get("cwd", ""))
    else:
        passed, note = True, "watch 有产出（无验收命令）"

    order["tries"] = int(order.get("tries", 0)) + 1
    if passed:
        order["status"] = "done"
        order["note"] = note
        order["doneAt"] = _now()
        st["dispatchedId"] = ""
        st["baseline"] = {}
        _emit("ok", f"验收通过[{slot}] {order['id']} {order['title']}")
    elif order["tries"] <= int(order.get("retries", 0)):
        order["status"] = "queued"
        order["note"] = f"第 {order['tries']} 次未过：{note[:_MAX_NOTE]}"
        st["dispatchedId"] = ""
        st["baseline"] = {}
        _emit("warn", f"验收未过重排[{slot}] {order['id']}（{order['tries']}/{order['retries']}）")
    else:
        order["status"] = "failed"
        order["note"] = f"重试耗尽：{note[:_MAX_NOTE]}"
        order["doneAt"] = _now()
        st["dispatchedId"] = ""
        st["baseline"] = {}
        _emit("err", f"验收失败弃单[{slot}] {order['id']}：{note[:80]}")

    # 自动派单的工单失败 → 暂停该炉位的自动模式，避免每 10 秒重派死循环
    if order.get("auto") and not passed:
        auto = _read_auto_state(state, slot)
        auto["paused"] = True
        auto["pausedReason"] = f"自动派单验收失败：{note[:120]}"
        _emit("warn", f"自动派单[{slot}]已暂停：{auto['pausedReason']}")
    _save_queue(orders)
    _save_state(state)
    return {"slot": slot, "action": "accepted", "id": order["id"],
            "status": order["status"], "note": note[:120]}


# ── 只读快照（admin API 用）─────────────────────────────────────────────────
def _load_hook_state() -> dict:
    try:
        data = json.loads((SUP_DIR / "hook-state.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _update_hook_auto(slot: str, **fields) -> None:
    """把复位/暂停状态同步到钩子的 hook-state.json（钩子才是自动续派的执行者）。"""
    path = SUP_DIR / "hook-state.json"
    data = _load_hook_state()
    auto = data.get("auto")
    if not isinstance(auto, dict):
        auto = {}
        data["auto"] = auto
    st = auto.get(slot)
    if not isinstance(st, dict):
        st = {}
        auto[slot] = st
    st.update(fields)
    try:
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    except OSError:
        pass


def auto_snapshot(state: dict | None = None, slot: str = SLOT_MAIN) -> dict:
    if state is None:
        state = _load_state()
    auto = _read_auto_state(state, slot)
    hook_auto = ((_load_hook_state().get("auto") or {}).get(slot) or {})
    cfg = _auto_config()
    task = _auto_task(slot)
    try:
        handoff_mtime = auto_handoff_file(slot).stat().st_mtime
    except OSError:
        handoff_mtime = 0.0
    rounds = max(int(auto.get("rounds") or 0), int(hook_auto.get("rounds") or 0))
    streak = max(int(auto.get("sameTaskStreak") or 0),
                 int(hook_auto.get("sameTaskStreak") or 0))
    paused = bool(auto.get("paused")) or bool(hook_auto.get("paused"))
    paused_reason = (auto.get("pausedReason") or hook_auto.get("pausedReason") or "")
    last_task = hook_auto.get("lastTask") or auto.get("lastTask") or ""
    last_at = max(float(auto.get("lastAt") or 0), float(hook_auto.get("lastAt") or 0))
    last_remaining = hook_auto.get("lastRemaining") or auto.get("lastRemaining") or 0
    return {
        "slot": slot,
        "enabled": auto_enabled(),
        "minRemaining": cfg["minRemaining"],
        "maxRounds": cfg["maxRounds"],
        "maxSameTask": cfg["maxSameTask"],
        "task": task,
        "rounds": rounds,
        "sameTaskStreak": streak,
        "nudge": int(hook_auto.get("nudge") or 0),
        "paused": paused,
        "pausedReason": str(paused_reason),
        "lastAt": last_at,
        "lastRemaining": last_remaining,
        "lastTask": str(last_task),
        "handoffMtime": handoff_mtime,
        "monitored": len(monitored_sessions(slot)),
        "remaining": quota_remaining() if auto_enabled() else 0,
    }


def set_auto(enabled: bool) -> dict:
    flag = SUP_DIR / "auto.enabled"
    if enabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("", encoding="utf-8")
        state = _load_state()
        for slot, st in state.get("slots", {}).items():
            auto = st.get("auto") if isinstance(st.get("auto"), dict) else {}
            auto["paused"] = False
            auto["pausedReason"] = ""
            st["auto"] = auto
            _update_hook_auto(slot, paused=False, pausedReason="")
        _save_state(state)
    elif flag.exists():
        flag.unlink()
    _emit("info", f"额度自动派单{'开启' if enabled else '关闭'}")
    return auto_snapshot()


def set_auto_task(text: str, slot: str = SLOT_MAIN) -> dict:
    text = (text or "").strip()
    tfile = auto_task_file(slot)
    if text:
        tfile.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(tfile, text + "\n")
    elif tfile.exists():
        tfile.unlink()
    state = _load_state()
    auto = _read_auto_state(state, slot)
    auto["paused"] = False
    auto["pausedReason"] = ""
    auto["sameTaskStreak"] = 0
    _save_state(state)
    _update_hook_auto(slot, paused=False, pausedReason="", sameTaskStreak=0, nudge=0)
    _emit("info", f"自动派单[{slot}]任务已更新（{len(text)} 字）" if text
          else f"自动派单[{slot}]任务已清空")
    return auto_snapshot(state, slot)


def reset_auto(slot: str | None = None) -> dict:
    state = _load_state()
    slots = [slot] if slot else list(state.get("slots", {}).keys()) or [SLOT_MAIN]
    for s in slots:
        _slot_state(state, s)["auto"] = {
            "rounds": 0, "sameTaskStreak": 0, "paused": False,
            "pausedReason": "", "lastTask": ""}
        _update_hook_auto(s, rounds=0, sameTaskStreak=0, nudge=0, paused=False,
                          pausedReason="", lastTask="")
    _save_state(state)
    _emit("info", f"额度自动派单计数已复位（{', '.join(slots)}）")
    return auto_snapshot(state, slots[0])


def snapshot() -> dict:
    with _lock:
        orders = _load_queue()
        state = _load_state()
        cfg = _auto_config()
        hook_auto = _load_hook_state().get("auto") or {}
        slots_out: dict[str, dict] = {}
        for slot in sorted({*(state.get("slots", {}).keys()),
                             *(normalize_slot(o.get("slot")) for o in orders),
                             *monitored_map().values(), *hook_auto.keys(), SLOT_MAIN}):
            st = state.get("slots", {}).get(slot, {})
            auto = auto_snapshot(state, slot)
            auto["minRemaining"] = cfg["minRemaining"]
            auto["maxRounds"] = cfg["maxRounds"]
            auto["maxSameTask"] = cfg["maxSameTask"]
            auto["remaining"] = quota_remaining() if auto_enabled() else 0
            slots_out[slot] = {
                "slot": slot,
                "dispatchedId": st.get("dispatchedId", ""),
                "promptPending": prompt_file(slot).exists(),
                "monitored": monitored_sessions(slot),
                "queued": sum(1 for o in orders if o["status"] == "queued"
                              and normalize_slot(o.get("slot")) == slot),
                "dispatched": sum(1 for o in orders if o["status"] == "dispatched"
                                  and normalize_slot(o.get("slot")) == slot),
                "auto": auto,
            }
        main = slots_out.get(SLOT_MAIN, {})
        return {
            "enabled": enabled(),
            "promptPending": bool(main.get("promptPending")),
            "dispatchedId": main.get("dispatchedId", ""),
            "queue": orders,
            "slots": slots_out,
            "auto": main.get("auto", {}),   # 兼容：main 炉位的自动派单视图
            "stats": {
                "queued": sum(1 for o in orders if o["status"] == "queued"),
                "dispatched": sum(1 for o in orders if o["status"] == "dispatched"),
                "done": sum(1 for o in orders if o["status"] == "done"),
                "failed": sum(1 for o in orders if o["status"] == "failed"),
                "cancelled": sum(1 for o in orders if o["status"] == "cancelled"),
            },
        }


# ── 后台轮询（线程：tick 里有阻塞文件 IO / verify 子进程，不能占事件循环）────
class BurnerRunner:
    """周期性推进状态机（派单/验收）。与 zburn-hub 同语义：10s 一步。"""

    def __init__(self, interval: float = TICK_INTERVAL) -> None:
        self._interval = interval
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._desired = False
        self._stop_event = threading.Event()
        self._last_result: dict = {}

    def status(self) -> dict:
        with self._lock:
            alive = bool(self._thread and self._thread.is_alive())
            return {"running": bool(self._desired and alive),
                    "lastTick": dict(self._last_result or {})}

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._desired = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="burner-runner",
                                            daemon=True)
            self._thread.start()
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._desired = False
            self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        return self.status()

    def _loop(self) -> None:
        while self._desired:
            try:
                self._last_result = tick()
            except Exception as err:  # noqa: BLE001 - 后台轮询必须自兜
                self._last_result = {"action": "error", "reason": str(err)}
                _emit("err", f"工单轮询异常: {err}")
            self._stop_event.wait(self._interval)


runner = BurnerRunner()
