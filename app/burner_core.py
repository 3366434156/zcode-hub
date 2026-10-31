"""着色器工单队列与状态机（纯逻辑层，可独立测试）。

移植自 zburn（E:\\数据\\projects\\zburn）batch.py 的队列/验收语义，派单通道从
UIA 打字改为 v0.2 Stop 钩子的文件协议（data/_sup/next-prompt.txt 一次性消费）。

状态机（每个工单）：
    queued ──派单(写 next-prompt.txt)──▶ dispatched ──验收通过──▶ done
       ▲                                    │
       └────── tries 未超限，重新排队 ◀──验收失败──

派单时机：next-prompt.txt 不存在 = 上个工单的回合已结束（钩子归档了文件），
此时才允许派下一单；文件存在 = 上一单还在执行中，只做验收不动派单。
总闸：data/_sup/continue.enabled 不存在时一切不动（与钩子同一开关）。
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

DIR = settings.DATA_DIR / "burner"
QUEUE_FILE = DIR / "queue.json"
STATE_FILE = DIR / "state.json"
PROMPT_FILE = settings.SUP_DIR / "next-prompt.txt"
ENABLE_FLAG = settings.SUP_DIR / "continue.enabled"

VERIFY_TIMEOUT = 600        # 单条验收命令上限（秒）
_MAX_NOTE = 400             # note 截断，防止验证器输出把状态文件撑爆

_lock = threading.RLock()


# ── 原子写（抄 zburn batch.py:58 的 tmp+os.replace）─────────────────────────
def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=1))


# ── 工单构造 ────────────────────────────────────────────────────────────────
def enqueue(title: str, prompt: str, watch: list[str] | None = None,
            verify_cmd: str = "", cwd: str = "", retries: int = 1) -> dict:
    """入队一个工单。id/标题/prompt 必填（校验语义抄 zburn batch.py:31-43）。"""
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
        "note": "",
        "at": _now(),
        "dispatchedAt": 0.0,
        "doneAt": 0.0,
    }
    with _lock:
        orders = _load_queue()
        orders.append(order)
        _save_queue(orders)
    logs.info("burner", f"入队 {order['id']} {title}")
    return order


def _find(orders: list[dict], order_id: str) -> dict | None:
    for o in orders:
        if o.get("id") == order_id:
            return o
    return None


def _maybe_remove_pending_prompt(order: dict) -> bool:
    """取消/重置在途工单时，若其指令还没被钩子消费（文件还在且内容相同），
    删掉它，防止已取消的活儿在回合结束仍被执行。返回是否删了。"""
    try:
        if PROMPT_FILE.exists() and PROMPT_FILE.read_text(encoding="utf-8").strip() == order.get("prompt", "").strip():
            PROMPT_FILE.unlink()
            return True
    except Exception as err:
        logs.warn("burner", f"清理待发指令失败: {err}")
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
        if state.get("dispatchedId") == order_id:
            state["dispatchedId"] = ""
            state["baseline"] = {}
            _save_state(state)
            if _maybe_remove_pending_prompt(o):
                logs.info("burner", f"已撤回未消费的待发指令（{order_id}）")
        logs.info("burner", f"取消 {order_id}")
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
        if state.get("dispatchedId") == order_id:
            state["dispatchedId"] = ""
            state["baseline"] = {}
            _save_state(state)
            if _maybe_remove_pending_prompt(o):
                logs.info("burner", f"已撤回未消费的待发指令（{order_id}）")
        logs.info("burner", f"重试 {order_id}")
        return o


def clear_done() -> int:
    with _lock:
        orders = _load_queue()
        keep = [o for o in orders if o["status"] not in ("done", "cancelled")]
        removed = len(orders) - len(keep)
        if removed:
            _save_queue(keep)
            logs.info("burner", f"清理已完成工单 {removed} 条")
        return removed


# ── 验收（抄 zburn batch.py:65-85）──────────────────────────────────────────
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


def tick(force: bool = False) -> dict:
    """状态机一步。后台轮询与"立即派单"都走这里（force=忽略节流直接尝试派单）。"""
    with _lock:
        if not enabled():
            return {"action": "idle", "reason": "总闸未开"}

        orders = _load_queue()
        state = _load_state()
        dispatched_id = state.get("dispatchedId", "")
        prompt_exists = PROMPT_FILE.exists()

        # 1) 有在途工单：先做验收
        if dispatched_id:
            o = _find(orders, dispatched_id)
            if o and o["status"] == "dispatched":
                return _accept(o, orders, state, prompt_exists)
            # 状态不一致（被人工动过）：清掉在途标记
            state["dispatchedId"] = ""
            _save_state(state)

        # 2) 无在途：如果 prompt 文件还在（上个工单的指令没被钩子消费），
        #    说明回合还没结束，不派新单
        if prompt_exists and not force:
            return {"action": "wait", "reason": "上一条指令尚未被消费"}

        # 3) 派下一单
        nxt = next((o for o in orders if o["status"] == "queued"), None)
        if not nxt:
            return {"action": "idle", "reason": "队列空"}
        return _dispatch(nxt, orders, state)


def _dispatch(order: dict, orders: list[dict], state: dict) -> dict:
    PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(PROMPT_FILE, order["prompt"] + "\n")
    order["status"] = "dispatched"
    order["dispatchedAt"] = _now()
    order["note"] = ""
    state["dispatchedId"] = order["id"]
    state["baseline"] = {w: _mtime(w) for w in order.get("watch", [])}
    _save_queue(orders)
    _save_state(state)
    logs.ok("burner", f"派单 {order['id']} {order['title']}（写入 next-prompt.txt）")
    return {"action": "dispatched", "id": order["id"], "title": order["title"]}


def _accept(order: dict, orders: list[dict], state: dict, prompt_exists: bool) -> dict:
    """在途工单验收：watch 文件有变化才跑 verify；通过=done，失败按 tries 重排。"""
    baseline = state.get("baseline", {}) or {}
    changed = any(_mtime(w) > (baseline.get(w) or 0.0) for w in order.get("watch", []))

    if prompt_exists and not changed:
        # 指令还没被消费、watch 也没动：工单仍在执行，不打扰
        return {"action": "running", "id": order["id"]}

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
        state["dispatchedId"] = ""
        state["baseline"] = {}
        logs.ok("burner", f"验收通过 {order['id']} {order['title']}")
    elif order["tries"] <= int(order.get("retries", 0)):
        order["status"] = "queued"
        order["note"] = f"第 {order['tries']} 次未过：{note[:_MAX_NOTE]}"
        state["dispatchedId"] = ""
        state["baseline"] = {}
        logs.warn("burner", f"验收未过重排 {order['id']}（{order['tries']}/{order['retries']}）")
    else:
        order["status"] = "failed"
        order["note"] = f"重试耗尽：{note[:_MAX_NOTE]}"
        order["doneAt"] = _now()
        state["dispatchedId"] = ""
        state["baseline"] = {}
        logs.err("burner", f"验收失败弃单 {order['id']}：{note[:80]}")
    _save_queue(orders)
    _save_state(state)
    return {"action": "accepted", "id": order["id"], "status": order["status"], "note": note[:120]}


# ── 只读快照（admin API 用）─────────────────────────────────────────────────
def snapshot() -> dict:
    with _lock:
        orders = _load_queue()
        state = _load_state()
        return {
            "enabled": enabled(),
            "promptPending": PROMPT_FILE.exists(),
            "dispatchedId": state.get("dispatchedId", ""),
            "queue": orders,
            "stats": {
                "queued": sum(1 for o in orders if o["status"] == "queued"),
                "dispatched": sum(1 for o in orders if o["status"] == "dispatched"),
                "done": sum(1 for o in orders if o["status"] == "done"),
                "failed": sum(1 for o in orders if o["status"] == "failed"),
                "cancelled": sum(1 for o in orders if o["status"] == "cancelled"),
            },
        }
