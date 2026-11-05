"""会话唤醒器：兜底「客户端停止续派 / 回合异常」导致的停摆。

背景：ZCode 客户端对 Stop 钩子有硬上限（每轮最多 3 次续派；已用
`docs/development/zcode-stop-hook-cap.md` 的补丁改为 999，但客户端重启才生效）。
即使上限解除，上游 5xx、CLI/桌面进程异常也会让会话停在“完成”，而 Stop 钩子
只在回合结束时才触发——没人唤醒就永远不会再跑。

本模块用 ZCode 自带的 headless CLI 做唤醒：
    node <zcode.cjs> --resume <sessionId> --prompt 继续 --cwd <目录> --mode yolo

`--resume` 会沿用会话原来的模型（实测 zcode2api/GLM-5.3-Flash），回合结束仍会
触发同一个 Stop 钩子，于是自动续派继续。只在会话确实空闲、额度>0、且不在
冷却期时才唤醒；唤醒进程还活着时绝不重复启动。

2026-11-03 加固（夜间连续运行前）：
1. Windows 上 `os.kill(pid, 0)` 不是探活，而是 TerminateProcess(pid, 0)：
   巡检会把上一分钟刚唤醒的 CLI 直接杀掉。改用 OpenProcess/GetExitCodeProcess。
2. 卡死判定不再只看「最后一条是 assistant」：回合在 user/工具结果后中断、
   或 assistant 一直挂着 tool-calls，静默超过 STUCK_SECONDS 也视为卡死并唤醒。
3. 尊重全局总闸 `continue.enabled` 与炉位 STOP；补巡检心跳、上下文用量提醒；
   启动时检查客户端 `f9a` 补丁是否还在（ZCode 自动更新会覆盖）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import burner, logs, settings, zsession

SUP_DIR: Path = Path(settings.SUP_DIR)
DIR: Path = Path(settings.DATA_DIR) / "burner"
STATE_FILE: Path = DIR / "revive.json"
LOG_FILE: Path = SUP_DIR / "revive.log"
CLI_LOG: Path = SUP_DIR / "revive-cli.log"
CLI_LOG_MAX = 20 * 1024 * 1024
FLAG: Path = SUP_DIR / "revive.enabled"
CONTINUE_FLAG: Path = SUP_DIR / "continue.enabled"
SESSIONS_FILE: Path = SUP_DIR / "continue.sessions"
# 通道硬闸的封锁名单：命中后写这里（含原会话 id，便于解封后恢复监听）。
BLOCK_FILE: Path = SUP_DIR / "model-block.json"
CLI: Path = Path(os.getenv("ZCODE_CLI", r"E:/exe/Zcode/resources/glm/zcode.cjs"))
CLIENT_FILE: Path = Path(os.getenv("ZCODE_CLIENT_RUNTIME", str(CLI)))
NODE: str = os.getenv("ZCODE_NODE") or shutil.which("node") or "node"
INTERVAL = float(os.getenv("ZCODE_REVIVE_INTERVAL", "60"))
IDLE_SECONDS = float(os.getenv("ZCODE_REVIVE_IDLE", "30"))
# 非正常收尾（最后一条是 user/工具结果、或 assistant 停在 tool-calls）的卡死阈值。
# 取值远大于正常工具耗时，避免把长构建/长思考误判成卡死。
STUCK_SECONDS = float(os.getenv("ZCODE_REVIVE_STUCK", "900"))
COOLDOWN = float(os.getenv("ZCODE_REVIVE_COOLDOWN", "600"))
HEARTBEAT_SECONDS = float(os.getenv("ZCODE_REVIVE_HEARTBEAT", "600"))
CONTEXT_WARN = int(os.getenv("ZCODE_REVIVE_CONTEXT_WARN", "600000"))
# 唤醒进程还活着但会话超过 HUNG_SECONDS 没有任何新消息/更新：判为卡死，终止后重唤醒。
HUNG_SECONDS = float(os.getenv("ZCODE_REVIVE_HUNG", "600"))
# 错峰唤醒：每个巡检周期最多唤醒几个炉位。
# 2026-11-04 01:26 实证：五个炉位在同一秒被唤醒 → 五个 128k 大上下文请求同时打到
# 上游 → 整池 5 个账号 5 秒内全部 3012/405（风控），冷却 15 分钟。此后改为每周期
# 最多唤醒 1 个（配合 60s 巡检间隔 = 炉位间至少 60s 错峰），恢复/解冻时刻不再成尖峰。
WAKE_PER_TICK = int(os.getenv("ZCODE_REVIVE_WAKE_PER_TICK", "1"))
# 上下文轮换阈值（tokens）。只在逼近模型硬上限时才换新会话（用户口径：50 万以内
# 靠客户端 auto-compact 自己压，不轻易丢连续性）。实测单请求 700k 曾成功，所以
# 触发风控的不是单请求大小，而是「5 路同时各发 40 万」的合计吞吐（见下面 HEAVY_*）。
# 0 = 关闭轮换。
# 2026-11-05 起默认 0：轮换用 `--prompt` 建新会话，不继承旧会话的模型绑定，
# 而是继承 magpie GUI 的「当前模型」——GUI 当时是 hy4-preview，一夜烧掉 462 元。
ROTATE_TOKENS = int(os.getenv("ZCODE_REVIVE_ROTATE_TOKENS", "0"))
# 重上下文限并发（2026-11-04 01:26 实证）：上下文 32~48 万时五路并行 = 每分钟向
# 上游重发约 900 万 token，整池 3012/405 冷却 15→30 分钟（strikes 满 4 次即禁用）。
# 因此单路上下文 >= HEAVY_CONTEXT 时，同池最多 HEAVY_MAX_BUSY 路并行其余错峰等待；
# 轻上下文（< 20 万）仍然五路全开——每分钟约 200~300 万 token，是实测健康区间。
HEAVY_CONTEXT = int(os.getenv("ZCODE_REVIVE_HEAVY_CONTEXT", "200000"))
HEAVY_MAX_BUSY = int(os.getenv("ZCODE_REVIVE_HEAVY_MAX_BUSY", "5"))

# ── 模型通道硬闸（2026-11-05 04:59 事故后新增）─────────────────────────────
# 事故：轮换出的 6 个新会话继承了 magpie GUI 的当前模型 hy4-preview，5 小时
# 2,457 次请求 / 642M tokens / 11.1K 积分（¥462），而网关 6.5 亿额度空放到期。
# 用户口径：**没检测到网关模型就必须停止，不管什么**。因此这里不看「配了什么」，
# 只看会话库里「实际用了什么」：provider+model 不在白名单 → 立刻杀进程、摘监听、
# 全池封锁，等人工确认后再解。
MODEL_ALLOW = tuple(
    m.strip() for m in os.getenv(
        "ZCODE_REVIVE_MODEL_ALLOW",
        "zcode2api/GLM-5.3-Flash,zcode2api/GLM-5.3",
    ).split(",") if m.strip())
# 闸门巡检间隔（秒）。独立于 60s 唤醒巡检：误用通道每分钟约 ¥1.5，10s 一查。
MODEL_GUARD_INTERVAL = float(os.getenv("ZCODE_REVIVE_MODEL_GUARD_INTERVAL", "5"))
# 新起的进程在多久内必须留下「网关模型」证据；超时没有任何证据也按违规处理。
MODEL_GRACE = float(os.getenv("ZCODE_REVIVE_MODEL_GRACE", "90"))
# 「查不到通道」连续几次后升级为封锁（避免 magpie 短暂卡顿时把炉位永久封死）。
MODEL_UNKNOWN_RETRIES = int(os.getenv("ZCODE_REVIVE_MODEL_UNKNOWN_RETRIES", "3"))
MAGPIE_BASE = os.getenv("ZCODE_MAGPIE_URL", "http://127.0.0.1:3425")
# 非唤醒器启动的（例如用户在 GUI 里手动开的）监听会话，最近这么久内跑过非网关
# 通道 → 同样摘监听封锁，避免 Stop 钩子接着往付费通道续派。
MODEL_STALE_WINDOW = float(os.getenv("ZCODE_REVIVE_MODEL_STALE", "900"))

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_lock = threading.RLock()
_last_beat: dict = {"at": 0.0, "text": ""}
_ctx_warn_at: dict[str, float] = {}
_patch_cache: dict = {"mtime": -1.0, "value": {}}
_wake_rr: dict = {"last": ""}   # 错峰唤醒的轮转游标（上次唤醒的炉位）


def _now() -> float:
    return time.time()


def log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    try:
        SUP_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    logs.info("revive", msg)


# 总闸状态观测：2026-11-04 实证，continue.enabled 被会话/外部删除后巡检全程静默
# （tick 早退不打日志），49 分钟里用户看到的只有「一点反应都没有」。开→关的
# 瞬间必须立刻留痕，持续关闭每 10 分钟低频提醒；恢复后自动复位。
_gate_watch: dict = {"on": None, "at": 0.0}


def _gate_notice(off_reason: str) -> None:
    """off_reason 为空 = 总闸正常（顺带复位观测）；否则记录关闭状态。"""
    now = _now()
    if not off_reason:
        if _gate_watch["on"] is False:
            log("[巡检] 总闸已恢复，唤醒/自动派单继续")
        _gate_watch.update(on=True, at=0.0)
        return
    if _gate_watch["on"] is True:
        _gate_watch.update(on=False, at=now)
        log(f"[巡检] {off_reason} —— 刚刚被关闭且无页面/接口日志"
            f"（疑似会话越权或外部删除，请核查 data/_sup）；唤醒与自动派单全部暂停")
        return
    first = _gate_watch["on"] is None
    last = float(_gate_watch["at"] or 0.0)
    _gate_watch.update(on=False, at=now)
    if first or now - last >= 600:
        log(f"[巡检] {off_reason}"
            f"{'（网关启动时即关闭）' if first else '（持续关闭，每 10 分钟提醒）'}；"
            f"唤醒与自动派单全部暂停，请在监督页重新开启")


def enabled() -> bool:
    return FLAG.exists()


def ensure_enabled() -> None:
    """默认开启（自动派单开启时）；用户可用页面/文件关掉。"""
    try:
        SUP_DIR.mkdir(parents=True, exist_ok=True)
        if not FLAG.exists() and burner.auto_enabled():
            FLAG.write_text("", encoding="utf-8")
    except OSError:
        pass
    patch = client_patch_status()
    if patch.get("exists") and not patch.get("patched"):
        log(f"警告：ZCode 运行时续派上限未打补丁（cap={patch.get('cap')}，"
            f"{patch.get('path')}）；桌面端回合可能仍在 3 次后停住")


def set_enabled(want: bool) -> bool:
    try:
        SUP_DIR.mkdir(parents=True, exist_ok=True)
        if want and not FLAG.exists():
            FLAG.write_text("", encoding="utf-8")
        elif not want and FLAG.exists():
            FLAG.unlink()
    except OSError as err:
        log(f"切换唤醒器失败: {err}")
    log(f"唤醒器{'开启' if FLAG.exists() else '关闭'}")
    return FLAG.exists()


def client_patch_status() -> dict:
    """检查 zcode.cjs 的续派上限是否仍是 999（ZCode 更新会把它覆盖回 3）。

    文件 15 MB，按 mtime 缓存；只在文件变化时重新扫一遍。
    """
    try:
        st = CLIENT_FILE.stat()
    except OSError:
        return {"path": str(CLIENT_FILE), "exists": False, "patched": False,
                "cap": None, "mtime": 0}
    if st.st_mtime == _patch_cache["mtime"] and _patch_cache["value"]:
        return dict(_patch_cache["value"])
    try:
        data = CLIENT_FILE.read_bytes()
        match = re.search(rb"f9a=(\d+)", data)
        cap = int(match.group(1)) if match else None
        value = {
            "path": str(CLIENT_FILE),
            "exists": True,
            "patched": bool(cap and cap > 3),
            "cap": cap,
            "mtime": st.st_mtime,
            "checkedAt": _now(),
        }
    except OSError as err:
        value = {"path": str(CLIENT_FILE), "exists": True, "patched": False,
                 "cap": None, "error": str(err), "mtime": st.st_mtime}
    _patch_cache.update(mtime=st.st_mtime, value=value)
    return dict(value)


def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _pid_alive(pid: int) -> bool:
    """进程是否存活（只读探活，绝不发信号）。

    Windows 的 `os.kill(pid, 0)` 不是探活：CPython 会调用
    TerminateProcess(handle, 0)，直接把进程杀掉。唤醒器曾因此每分钟杀掉
    自己刚启动的 CLI。这里改用 OpenProcess + GetExitCodeProcess。
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_win(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_alive_win(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_image_win(pid: int) -> str:
    """进程可执行文件全路径（只读）。用于确认 pid 还是我们唤醒的 node。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def _pid_matches(pid: int) -> bool:
    """pid 是否仍是我们唤醒的 node CLI。

    Windows 的 PID 会被复用；只看存活会把复用了同一 PID 的无关进程当成
    “CLI 还在跑”，该炉位就永远不唤醒。这里额外核对映像名。
    """
    if not _pid_alive(pid):
        return False
    if os.name != "nt":
        return True
    image = _pid_image_win(pid)
    return Path(image).name.lower() in ("node.exe", "node") if image else False


def _terminate(pid: int) -> bool:
    """终止卡死的唤醒进程（只在已确认 pid 是我们的 node 时调用）。

    Windows 用 taskkill /T 连子进程一起收掉（node 下面可能挂着构建/脚本）；
    其它平台走 SIGTERM。
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=_CREATE_NO_WINDOW, timeout=10, check=False)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except OSError:
        return False


# ── 模型通道硬闸 ────────────────────────────────────────────────────────────
_guard_lock = threading.Lock()
_usage_cache: dict = {"at": 0.0, "rows": []}


def _http_json(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - magpie 没起/超时都按「无证据」处理
        return None


def _norm_sid(session_id: str) -> str:
    text = str(session_id or "").strip()
    return text[5:] if text.startswith("sess_") else text


def _channel_name(provider: str, model: str) -> str:
    """拼出「provider/model」形式的通道名。

    会话库：provider_id=magpie, model_id=zcode2api/GLM-5.3-Flash（已带前缀）。
    magpie usage：provider=zcode2api, model=GLM-5.3-Flash（要拼）。
    """
    prov = str(provider or "").strip()
    name = str(model or "").strip()
    if not name:
        return ""
    if "/" in name:
        return name
    return f"{prov}/{name}" if prov else name


def _allowed_channel(name: str) -> bool:
    """白名单精确匹配；白名单项以 / 结尾时按前缀匹配。"""
    text = str(name or "").strip()
    if not text:
        return False
    return any(text == item or (item.endswith("/") and text.startswith(item))
               for item in MODEL_ALLOW)


def _usage_rows(limit: int = 300) -> list:
    now = _now()
    if now - float(_usage_cache["at"]) < 5 and _usage_cache["rows"]:
        return _usage_cache["rows"]
    data = _http_json(f"{MAGPIE_BASE}/v1/magpie/usage/requests?limit={int(limit)}")
    rows = data.get("rows") if isinstance(data, dict) else None
    rows = rows if isinstance(rows, list) else []
    _usage_cache.update(at=now, rows=rows)
    return rows


def _route_for(session_id: str) -> dict:
    data = _http_json(f"{MAGPIE_BASE}/v1/magpie/route?session="
                      f"{urllib.parse.quote(_norm_sid(session_id))}")
    route = (data or {}).get("route") if isinstance(data, dict) else None
    return route if isinstance(route, dict) else {}


def _evidence_from_db(session_id: str) -> dict:
    try:
        row = zsession.last_model(session_id)
    except Exception:  # noqa: BLE001 - 会话库读失败 → 继续找其它证据
        row = None
    if not row:
        return {}
    channel = _channel_name(row.get("provider"), row.get("model"))
    return {"state": "ok" if _allowed_channel(channel) else "wrong",
            "channel": channel, "source": "session-db",
            "provider": row.get("provider") or "", "model": row.get("model") or "",
            "at": float(row.get("at") or 0)}


def _evidence_from_usage(session_id: str) -> dict:
    nid = _norm_sid(session_id)
    mine = []
    for row in _usage_rows():
        if not isinstance(row, dict):
            continue
        if (_norm_sid(row.get("session")) == nid
                or _norm_sid(row.get("native_session")) == nid):
            mine.append(row)
    if not mine:
        return {}
    row = max(mine, key=lambda r: str(r.get("t") or ""))
    provider = str(row.get("session_provider") or row.get("provider") or "")
    model = str(row.get("served") or row.get("model") or "")
    asked = str(row.get("req") or "")
    channel = _channel_name(provider, model) if model else asked
    return {"state": "ok" if _allowed_channel(channel) else "wrong",
            "channel": channel, "source": "magpie-usage",
            "provider": provider, "model": model,
            "at": _iso_epoch(row.get("t"))}


def _iso_epoch(text) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _evidence_from_route(session_id: str) -> dict:
    route = _route_for(session_id)
    if not route:
        return {}
    asked = str(route.get("asked") or route.get("model") or "")
    provider = str(route.get("provider") or "")
    channel = asked if "/" in asked else _channel_name(provider, asked)
    if not channel:
        return {}
    return {"state": "ok" if _allowed_channel(channel) else "wrong",
            "channel": channel, "source": "magpie-route",
            "provider": provider, "model": asked,
            "at": _iso_epoch(route.get("time"))}


def _lane_evidence(session_id: str) -> dict:
    """某会话实际走的通道：会话库优先（完整），其次 magpie usage，最后 route。

    三条都查不到 → unknown。unknown 不是「放行」：调用方按宽限期后必须停机。
    """
    if not session_id:
        return {"state": "unknown", "channel": "", "source": "no-session"}
    for probe in (_evidence_from_db, _evidence_from_usage, _evidence_from_route):
        found = probe(session_id)
        if found:
            return found
    return {"state": "unknown", "channel": "", "source": "no-evidence"}


def _load_blocks() -> dict:
    try:
        data = json.loads(BLOCK_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_blocks(data: dict) -> None:
    try:
        SUP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = BLOCK_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, BLOCK_FILE)
    except OSError as err:
        log(f"通道硬闸：写封锁文件失败（{err}）；本轮停机仍然生效")


def _slot_blocked(slot: str) -> bool:
    blocks = _load_blocks()
    return bool(blocks.get("locked")) or slot in (blocks.get("slots") or {})


def _drop_session(session_id: str) -> bool:
    """把会话从监听名单摘掉：钩子/唤醒器都不会再续它（硬停）。"""
    if not session_id:
        return False
    try:
        lines = SESSIONS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    out = [ln for ln in lines
           if ln.strip().split("|")[0].strip() != str(session_id).strip()]
    if len(out) == len(lines):
        return False
    try:
        SESSIONS_FILE.write_text("\n".join(out) + ("\n" if out else ""),
                                 encoding="utf-8")
    except OSError:
        return False
    return True


def _block_lane(slot: str, session_id: str, state: dict, evidence: dict,
                why: str) -> None:
    """终止进程 + 摘监听 + 全池封锁。只有人工解除才恢复。"""
    prev = state.get(slot) or {}
    pid = int(prev.get("pid") or 0)
    killed = _terminate(pid) if _pid_matches(pid) else False
    channel = str(evidence.get("channel") or "")
    blocks = _load_blocks()
    blocks.setdefault("slots", {})[slot] = {
        "at": _now(),
        "session": session_id or str(prev.get("session") or ""),
        "channel": channel,
        "source": str(evidence.get("source") or ""),
        "reason": why,
        "killed": killed,
    }
    blocks["locked"] = True
    blocks["lockedAt"] = _now()
    blocks["reason"] = f"{why}（{slot}：{channel or '未检测到通道'}）"
    _save_blocks(blocks)
    dropped = _drop_session(session_id) if session_id else False
    state[slot] = {"at": 0, "pid": 0, "session": session_id or "",
                   "reason": "model-block", "dir": prev.get("dir") or ""}
    _save_state(state)
    log(f"[{slot}] 通道硬闸触发：实际 {channel or '未检测到'}"
        f"（证据 {evidence.get('source') or '-'}），白名单 {'/'.join(MODEL_ALLOW)}；"
        f"{'已终止进程' if killed else '进程已不在'}、"
        f"{'已摘除监听' if dropped else '监听未变更'}；全池封锁，等人工解除")


def guard_tick() -> dict:
    """通道硬闸一步：在跑的炉位只要不是网关模型（或查不到），立刻停机。

    生效条件：**任一**续派通道开着就查——Stop 钩子（continue.enabled）能在
    唤醒器关闭时独立续派，所以不能只看 revive.enabled。
    """
    with _guard_lock:
        if not CONTINUE_FLAG.exists() and not enabled():
            return {"action": "idle", "reason": "续派与唤醒器都未开启"}
        blocks = _load_blocks()
        if blocks.get("locked"):
            return {"action": "locked",
                    "reason": str(blocks.get("reason") or "通道硬闸已封锁")}
        mapping = burner.monitored_map()
        if not mapping:
            return {"action": "idle", "reason": "监听名单为空"}
        state = _load_state()
        now = _now()
        results = []
        for sid, slot in mapping.items():
            prev = state.get(slot) or {}
            pid = int(prev.get("pid") or 0)
            if not _pid_matches(pid):
                # 不是唤醒器起的（例如用户自己在 GUI 里跑的）：只要最近还在非网关
                # 通道上，就摘监听封锁——Stop 钩子不能接着往付费通道续派。
                evidence = _lane_evidence(sid)
                if (evidence["state"] == "wrong"
                        and now - float(evidence.get("at") or 0) <= MODEL_STALE_WINDOW):
                    _block_lane(slot, sid, state, evidence, "最近在非网关通道上运行")
                    results.append({"slot": slot, "action": "blocked",
                                    "channel": evidence.get("channel") or "",
                                    "reason": "最近在非网关通道上运行"})
                continue
            active = str(prev.get("session") or "")
            if not active and not str(prev.get("reason") or "").startswith("上下文轮换"):
                active = sid
            evidence = _lane_evidence(active)
            if evidence["state"] == "wrong":
                _block_lane(slot, active or sid, state, evidence, "模型通道不是网关")
                results.append({"slot": slot, "action": "blocked",
                                "channel": evidence.get("channel") or "",
                                "reason": "模型通道不是网关"})
                continue
            if evidence["state"] == "unknown":
                age = now - float(prev.get("at") or now)
                if age < MODEL_GRACE:
                    results.append({"slot": slot, "action": "pending",
                                    "reason": f"等待通道证据 {int(age)}s/{int(MODEL_GRACE)}s"})
                    continue
                killed = _terminate(pid)
                blocks = _load_blocks()
                counter = blocks.setdefault("unknown", {}).setdefault(slot, {"n": 0})
                counter["n"] = int(counter.get("n") or 0) + 1
                counter["at"] = now
                _save_blocks(blocks)
                state[slot] = {"at": now, "pid": 0, "session": active or sid,
                               "reason": "model-unknown-killed",
                               "dir": prev.get("dir") or ""}
                _save_state(state)
                log(f"[{slot}] 通道硬闸：进程跑了 {int(age)}s 仍拿不到"
                    f"「{MODEL_ALLOW[0] if MODEL_ALLOW else '网关模型'}」证据"
                    f"（{evidence.get('source')}），{'已终止' if killed else '终止失败'}；"
                    f"冷却后重试（第 {counter['n']} 次）")
                results.append({"slot": slot, "action": "killed",
                                "reason": "无法确认网关通道"})
                if counter["n"] >= MODEL_UNKNOWN_RETRIES:
                    _block_lane(slot, active or sid, state, evidence,
                                "连续无法确认网关通道")
                continue
            results.append({"slot": slot, "action": "model-ok",
                            "channel": evidence.get("channel") or ""})
        return {"action": "ok" if results else "idle", "results": results}


def audit_bindings() -> dict:
    """启动/人工审计：绑定会话只要历史通道不是网关，一律摘除并封锁。

    与 guard_tick 的区别：不看总闸、不看进程、不看时间窗——只要这个会话曾经
    走过非网关通道，就绝不允许再被唤醒（唤醒 = 继续烧付费额度）。空会话
    （没有任何 model_usage）不判违规，交给唤醒后的宽限期兜底。
    """
    mapping = burner.monitored_map()
    if not mapping:
        return {"action": "idle", "reason": "监听名单为空"}
    state = _load_state()
    results = []
    for sid, slot in mapping.items():
        evidence = _lane_evidence(sid)
        if evidence["state"] != "wrong":
            results.append({"slot": slot, "action": evidence["state"],
                            "channel": evidence.get("channel") or ""})
            continue
        _block_lane(slot, sid, state, evidence, "启动审计：绑定会话不是网关通道")
        results.append({"slot": slot, "action": "blocked",
                        "channel": evidence.get("channel") or "",
                        "reason": "启动审计：绑定会话不是网关通道"})
    return {"action": "ok", "results": results}


def unblock(slot: str = "") -> dict:
    """解除通道封锁：只清封锁，不自动把旧会话塞回监听名单。

    被封锁的会话本身模型绑定就是错的（例如 hy4-preview），还回去等于再烧一次；
    请在页面上重新勾选网关模型的新会话，再点解除。
    """
    blocks = _load_blocks()
    slots = blocks.get("slots") or {}
    if slot:
        slots.pop(slot, None)
    else:
        slots.clear()
        blocks["unknown"] = {}
    if not slots:
        blocks["locked"] = False
        blocks.pop("reason", None)
    blocks["slots"] = slots
    _save_blocks(blocks)
    log(f"通道硬闸：已解除{'（' + slot + '）' if slot else '（全部）'}；"
        f"监听名单未改动，请确认 magpie 当前模型是 {'/'.join(MODEL_ALLOW)} 再开跑")
    return status()


def _last_activity(sess: dict | None, info: dict | None) -> float:
    values = [float((sess or {}).get("updated_at") or 0)]
    if info:
        values += [float(info.get("completed_at") or 0),
                   float(info.get("created_at") or 0),
                   float(info.get("updated_at") or 0)]
    return max(values)


def _idle(info: dict, now: float | None = None, outcome: str = "",
          updated_at: float = 0.0) -> bool:
    """会话是否已经停下、可以唤醒。

    - 最后一条 assistant 且 finish 不是 tool-calls：正常收尾，静默 IDLE_SECONDS。
    - 最后一条是 user/工具结果，或 assistant 仍标着 tool-calls：可能回合还在跑，
      静默 STUCK_SECONDS 才视为卡死（长构建/长思考不误杀）。
    - 会话 outcome 明确失败：回合已经结束，按 IDLE_SECONDS 处理。
    """
    if not info:
        return False
    now = now or _now()
    ts = max(float(info.get("completed_at") or 0),
             float(info.get("created_at") or 0),
             float(updated_at or 0))
    if ts <= 0:
        return False
    age = now - ts
    finish = str(info.get("finish") or "").lower()
    role = str(info.get("role") or "")
    failed = str(outcome or "").lower() in (
        "error", "failed", "failure", "aborted", "timeout")
    if role == "assistant" and finish not in ("tool-calls", "tool_calls"):
        return age >= IDLE_SECONDS
    if failed:
        return age >= IDLE_SECONDS
    return age >= STUCK_SECONDS


def _auto_task_stop(slot: str) -> bool:
    try:
        text = burner.auto_task_file(slot).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return text.upper() == "STOP"


def _hook_paused(slot: str) -> tuple[bool, str]:
    """钩子侧暂停标记：暂停时唤醒也没用，跳过并让页面显示原因。"""
    try:
        data = json.loads((SUP_DIR / "hook-state.json").read_text(encoding="utf-8"))
        state = ((data.get("auto") or {}).get(slot) or {})
        return bool(state.get("paused")), str(state.get("pausedReason") or "")
    except Exception:
        return False, ""


def _outside_night() -> bool:
    """用户口径：只烧到早上 9 点；09:00–23:00 自动唤醒暂停。"""
    return 9 <= time.localtime().tm_hour < 23


def _summary(results: list[dict], remaining: int) -> str:
    parts = []
    for item in results:
        action = item.get("action")
        text = f"{item.get('slot')}:{action}"
        if action == "woke":
            text += f" pid={item.get('pid')}"
        elif action == "busy":
            text += f" pid={item.get('pid')}"
        elif action == "active":
            text += f" {item.get('tokens')}tok"
        elif action in ("idle", "cool", "error", "stagger"):
            reason = (item.get("reason") or "")[:48]
            if reason:
                text += f"({reason})"
        parts.append(text)
    return f"额度 {remaining}；" + "；".join(parts)


def _maybe_beat(text: str) -> None:
    """巡检心跳：状态变化或每 HEARTBEAT_SECONDS 记一行，夜间可回查。"""
    now = _now()
    if now - _last_beat["at"] < 60:
        return
    if text == _last_beat["text"] and now - _last_beat["at"] < HEARTBEAT_SECONDS:
        return
    log(f"[巡检] {text}")
    _last_beat.update(at=now, text=text)


def _seed_prompt(slot: str, lead: str) -> str:
    """靠交接文件恢复进度的首轮指令（换会话 / 空会话补种共用）。"""
    handoff = burner.auto_handoff_file(slot)
    task = burner.auto_task_file(slot)
    return (
        f"{lead}\n\n"
        f"先读这两个文件恢复上下文：\n"
        f"1. {handoff} —— 上一阶段交接（已完成 / 遗留 / 下一步）\n"
        f"2. {task} —— 本轮任务\n\n"
        f"然后执行该任务：严格按计划的「执行纪律」办，一轮只做一个任务。\n"
        f"完成后按两条收口：\n"
        f"- 把下一项任务写进 {task}（只写任务本身，不要解释）；\n"
        f"- 把交接写进 {handoff}（覆盖写，最多 20 行，不贴代码）。\n"
        f"如果当前轮次计划已全部完成，不要写 STOP：先起草下一轮计划，"
        f"再把下一轮第 1 项写回任务文件。\n"
        f"data/_sup 下除本炉位这两个文件外一律不动"
        f"（continue.enabled / continue.sessions / hook-state.json / next-prompt* 都不碰）。\n"
        f"不要指派子代理，不要 fork 新会话。"
    )


def _resume_prompt(slot: str) -> str:
    """唤醒续跑指令：待发任务存在就带上，避免唤醒器接管时丢任务。

    钩子注入被节流闸门跳过时 CLI 会退出、任务留在 auto-task 文件里；
    这时唤醒器是唯一接手方，prompt 里必须点名任务，否则 agent 只会"继续"
    而不知道下一项是什么。
    """
    try:
        task = burner.auto_task_file(slot).read_text(encoding="utf-8").strip()
    except OSError:
        task = ""
    if not task or task.upper() == "STOP":
        return "继续"
    return ("继续。上一轮已收尾，当前待办任务如下（执行完按「执行纪律」写回"
            f"下一项任务到 auto-task 文件）：\n{task}")


def _rotate_prompt(slot: str, tokens: int) -> str:
    """换新会话的首轮指令（上下文已达轮换阈值）。"""
    return _seed_prompt(
        slot,
        f"接手炉位 {slot}（上一会话上下文已达 {tokens // 1000}k tokens，"
        f"单请求体积过大触发上游限流，按规范换新会话续跑）。",
    )


def _seed_empty_prompt(slot: str) -> str:
    """空会话补种：换会话时首轮请求可能被上游拒（503/风控），会话建了但没内容。"""
    return _seed_prompt(
        slot,
        f"接手炉位 {slot}（新会话已建但首轮没跑成，上下文为空，"
        f"必须靠交接文件恢复进度）。",
    )


def _rebind_slot(slot: str, new_sid: str, old_sid: str) -> bool:
    """把 continue.sessions 里该炉位的会话换成 new_sid（其余行原样保留）。"""
    try:
        lines = SESSIONS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as err:
        log(f"[{slot}] 换绑失败：读不到监听名单（{err}）")
        return False
    want = burner.normalize_slot(slot)
    out, done = [], False
    for ln in lines:
        raw = ln.strip()
        if not raw or raw.startswith("#"):
            out.append(ln)
            continue
        sid, _, sl = raw.partition("|")
        if sid.strip() != old_sid:
            out.append(ln)
            continue
        out.append(new_sid if want == burner.SLOT_MAIN else f"{new_sid}|{want}")
        done = True
    if not done:
        out.append(new_sid if want == burner.SLOT_MAIN else f"{new_sid}|{want}")
    try:
        SESSIONS_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError as err:
        log(f"[{slot}] 换绑失败：写不回监听名单（{err}）")
        return False
    return True


_claim_lock = threading.Lock()


def _claim_session(slot: str, directory: str, since: float, old_sid: str) -> None:
    """后台等新会话出现在会话库里，然后换绑监听名单。

    CLI 的 session id 由客户端生成、启动后才落库，所以只能起进程后再认领；
    hook 要等回合结束才触发，这个窗口足够宽。

    ⚠️ 五个炉位同目录：认领必须先查「这个 id 是否已被别的炉位占用」，否则两个
    炉位会抢同一个新会话（check + rebind 全程持锁）。
    """
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(5)
        try:
            sid = zsession.latest_session_in(directory, since)
        except Exception:  # noqa: BLE001 - 会话库偶发锁，继续等
            continue
        if not sid or sid == old_sid:
            continue
        with _claim_lock:
            if sid in burner.monitored_map():      # 已被别的炉位认领
                continue
            if _rebind_slot(slot, sid, old_sid):
                log(f"[{slot}] 已换到新会话 {sid}（旧 {old_sid} 上下文/请求体积过大）")
                state = _load_state()
                cur = state.get(slot) or {}
                if cur.get("session") in ("", old_sid):
                    cur["session"] = sid
                    state[slot] = cur
                    _save_state(state)
                return
    log(f"[{slot}] 换会话后 240s 内没在会话库认领到新会话；本轮跑完请人工确认绑定")


def _rotate_wake(slot: str, sid: str, directory: str, tokens: int,
                 state: dict) -> dict:
    """上下文过大 → 起一个全新会话续跑（不带 --resume，上下文从零开始）。

    ⚠️ 新会话的模型绑定来自 magpie GUI 的「当前模型」，不是旧会话的通道；
    因此状态里 session 先留空，通道硬闸在认领到新会话前按「未确认」处理。
    """
    if _slot_blocked(slot):
        log(f"[{slot}] 通道硬闸封锁中，拒绝换会话")
        return {"action": "error", "reason": "通道硬闸封锁中"}
    if not CLI.exists():
        log(f"[{slot}] 换会话失败：CLI 不存在 {CLI}")
        return {"action": "error", "reason": f"CLI 不存在: {CLI}"}
    if not directory or not Path(directory).is_dir():
        log(f"[{slot}] 换会话失败：目录不存在 {directory}")
        return {"action": "error", "reason": f"目录不存在: {directory}"}
    cmd = [NODE, str(CLI), "--prompt", _rotate_prompt(slot, tokens),
           "--cwd", directory, "--mode", "yolo", "--surface", "terminal"]
    started = time.time()
    logf = None
    try:
        SUP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if CLI_LOG.exists() and CLI_LOG.stat().st_size > CLI_LOG_MAX:
                CLI_LOG.replace(SUP_DIR / "revive-cli.log.1")
        except OSError:
            pass
        logf = CLI_LOG.open("ab")
        log(f"[{slot}] 上下文 {tokens // 1000}k 已达轮换阈值 "
            f"{ROTATE_TOKENS // 1000}k，起**新会话**续跑（旧 {sid} 停用）")
        proc = subprocess.Popen(
            cmd, cwd=directory, stdin=subprocess.DEVNULL,
            stdout=logf, stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as err:  # noqa: BLE001
        log(f"[{slot}] 换会话失败: {err}")
        return {"action": "error", "reason": str(err)}
    finally:
        if logf is not None:
            logf.close()
    state[slot] = {"at": _now(), "pid": proc.pid, "session": "",
                   "reason": "上下文轮换（新会话认领中）", "dir": directory}
    _save_state(state)
    threading.Thread(target=_claim_session,
                     args=(slot, directory, started - 5, sid),
                     daemon=True, name=f"claim-{slot}").start()
    return {"action": "rotate", "slot": slot, "pid": proc.pid, "tokens": tokens}


def _wake(slot: str, sid: str, directory: str, reason: str, state: dict,
          prompt: str = "继续") -> dict:
    if _slot_blocked(slot):
        log(f"[{slot}] 通道硬闸封锁中，拒绝唤醒")
        return {"action": "error", "reason": "通道硬闸封锁中"}
    if not CLI.exists():
        log(f"[{slot}] 唤醒失败：CLI 不存在 {CLI}")
        return {"action": "error", "reason": f"CLI 不存在: {CLI}"}
    if directory and not Path(directory).is_dir():
        log(f"[{slot}] 唤醒失败：目录不存在 {directory}")
        return {"action": "error", "reason": f"目录不存在: {directory}"}
    cmd = [NODE, str(CLI), "--resume", sid, "--prompt", prompt,
           "--cwd", directory or str(Path.cwd()), "--mode", "yolo",
           "--surface", "terminal"]
    logf = None
    try:
        SUP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if CLI_LOG.exists() and CLI_LOG.stat().st_size > CLI_LOG_MAX:
                CLI_LOG.replace(SUP_DIR / "revive-cli.log.1")
        except OSError:
            pass
        logf = CLI_LOG.open("ab")
        log(f"[{slot}] 启动唤醒 CLI：--resume {sid} --cwd {directory}（{reason}）")
        proc = subprocess.Popen(
            cmd, cwd=directory or None, stdin=subprocess.DEVNULL,
            stdout=logf, stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as err:  # noqa: BLE001
        log(f"[{slot}] 唤醒失败: {err}")
        return {"action": "error", "reason": str(err)}
    finally:
        if logf is not None:
            logf.close()
    state[slot] = {"at": _now(), "pid": proc.pid, "session": sid,
                   "reason": reason, "dir": directory}
    _save_state(state)
    log(f"[{slot}] 已唤醒 {sid}（{reason}，pid={proc.pid}）")
    return {"action": "woke", "slot": slot, "pid": proc.pid, "reason": reason}


def _rotated(mapping: dict, last: str) -> list:
    """从上次唤醒的炉位之后开始轮转，保证错峰唤醒时不会永远只宠 main。"""
    items = list(mapping.items())
    if len(items) < 2:
        return items
    slots = [slot for _, slot in items]
    try:
        start = (slots.index(last) + 1) % len(items)
    except ValueError:
        start = 0
    return items[start:] + items[:start]


def tick() -> dict:
    """唤醒巡检一步：空闲且额度>0 的监听会话 → headless CLI 续一轮。"""
    with _lock:
        if not enabled():
            return {"action": "idle", "reason": "唤醒器未开启"}
        if not CONTINUE_FLAG.exists():
            _gate_notice("续派总闸未开（continue.enabled 不存在）")
            return {"action": "idle",
                    "reason": "续派总闸未开（continue.enabled 不存在）"}
        if not burner.enabled():
            _gate_notice("总闸未开（continue.enabled 在而 auto.enabled 关闭）")
            return {"action": "idle", "reason": "总闸未开"}
        _gate_notice("")
        blocks = _load_blocks()
        if blocks.get("locked"):
            reason = str(blocks.get("reason") or "通道硬闸已封锁")
            _maybe_beat(f"通道硬闸封锁中：{reason}（不会再启动任何会话）")
            return {"action": "idle", "reason": f"通道硬闸封锁中：{reason}"}
        mapping = burner.monitored_map()
        if not mapping:
            return {"action": "idle", "reason": "监听名单为空"}

        remaining = burner.quota_remaining()
        if remaining <= 0:
            _maybe_beat("可用额度 0；等待额度窗口/账号恢复")
            return {"action": "idle", "reason": "可用额度为 0，不唤醒"}

        state = _load_state()
        auto_on = burner.auto_enabled()
        now = _now()
        results = []
        budget = WAKE_PER_TICK
        # 预扫：谁在跑 + 各自上下文多大。重上下文限并发必须全局视角，
        # 不能边循环边判断（否则先唤醒的几路都以为自己是最先的）。
        pre: dict[str, dict] = {}
        for sid_i, slot_i in mapping.items():
            prev_i = state.get(slot_i) or {}
            try:
                ctx_i = zsession.context_tokens(sid_i)
            except Exception:  # noqa: BLE001 - 会话库读失败按轻上下文处理
                ctx_i = 0
            pre[slot_i] = {"alive": _pid_matches(int(prev_i.get("pid") or 0)),
                           "ctx": ctx_i}
        # 重上下文并发只数「同样重」的 busy 路：轻上下文长跑（钩子 block 连续）
        # 不占重上下文名额——2026-11-05 01:1x 实证：s3/s4 轻上下文长跑使 busy_n
        # 恒 >= 1，把唯一的重上下文路（main 248k）永久挡在 stagger 里。
        busy_heavy = sum(1 for v in pre.values()
                         if v["alive"] and v["ctx"] >= HEAVY_CONTEXT)
        for sid, slot in _rotated(mapping, str(_wake_rr.get("last") or "")):
            prompt_pending = burner.prompt_file(slot).exists()
            prev = state.get(slot) or {}
            pid = int(prev.get("pid") or 0)
            if slot in (blocks.get("slots") or {}):
                results.append({"slot": slot, "action": "blocked",
                                "reason": "通道硬闸已封锁该炉位"})
                continue
            sess = zsession.get_session(sid)
            info = zsession.last_message(sid) if sess else None
            tokens = int(((info or {}).get("tokens") or {}).get("input") or 0)

            if _pid_matches(pid):
                last = _last_activity(sess, info)
                if last > 0 and now - last >= HUNG_SECONDS:
                    killed = _terminate(pid)
                    state[slot] = {"at": 0, "pid": 0, "session": sid,
                                   "reason": "hung-killed",
                                   "dir": prev.get("dir") or ""}
                    _save_state(state)
                    log(f"[{slot}] 唤醒进程 pid={pid} 存活但 {int(now - last)}s 无进展，"
                        f"{'已终止' if killed else '终止失败'}；下一轮重唤醒")
                    results.append({"slot": slot, "action": "killed", "pid": pid,
                                    "reason": "进程卡死", "tokens": tokens})
                    continue
                results.append({"slot": slot, "action": "busy", "pid": pid,
                                "tokens": tokens})
                continue
            left = COOLDOWN - (now - float(prev.get("at") or 0))
            if left > 0:
                results.append({"slot": slot, "action": "cool",
                                "reason": "冷却中", "left": int(left)})
                continue
            if not auto_on and not prompt_pending:
                results.append({"slot": slot, "action": "idle",
                                "reason": "自动派单未开且无待发指令"})
                continue
            if auto_on and not prompt_pending and _auto_task_stop(slot):
                results.append({"slot": slot, "action": "idle",
                                "reason": "该炉位已写 STOP"})
                continue
            if auto_on and not prompt_pending:
                paused, why = _hook_paused(slot)
                if paused:
                    results.append({"slot": slot, "action": "idle",
                                    "reason": f"自动派单已暂停：{why[:40]}"})
                    continue
                if _outside_night():
                    results.append({"slot": slot, "action": "idle",
                                    "reason": "白天停派（09:00–23:00）"})
                    continue
            if not sess:
                results.append({"slot": slot, "action": "idle",
                                "reason": "会话不存在"})
                continue
            # 唤醒前先验通道：会话历史已经证明不是网关模型 → 一个请求都不许发。
            evidence = _lane_evidence(sid)
            if evidence["state"] == "wrong":
                _block_lane(slot, sid, state, evidence, "唤醒前检查：模型通道不是网关")
                results.append({"slot": slot, "action": "blocked",
                                "channel": evidence.get("channel") or "",
                                "reason": "唤醒前检查：模型通道不是网关"})
                continue
            if tokens >= CONTEXT_WARN and now - _ctx_warn_at.get(slot, 0) >= 1800:
                _ctx_warn_at[slot] = now
                log(f"[{slot}] 上下文 {tokens} tokens 达到提醒阈值 "
                    f"{CONTEXT_WARN}；若上游报 context 超限，需要切换新一轮会话")
            if not _idle(info, now, sess.get("outcome") or "",
                         float(sess.get("updated_at") or 0)):
                results.append({"slot": slot, "action": "active",
                                "tokens": tokens, "session": sid})
                continue
            if budget <= 0:
                # 削峰：本周期名额已用完，留给下一个巡检周期（60s 后），
                # 避免多路在同一秒向上游发大请求（2026-11-04 01:26 整池 3012）
                results.append({"slot": slot, "action": "stagger",
                                "reason": "错峰排队，下一周期唤醒"})
                continue
            ctx = pre.get(slot, {}).get("ctx") or zsession.context_tokens(sid)
            heavy = HEAVY_CONTEXT > 0 and ctx >= HEAVY_CONTEXT
            if heavy and busy_heavy >= HEAVY_MAX_BUSY:
                results.append({"slot": slot, "action": "stagger",
                                "reason": f"重上下文 {ctx // 1000}k，"
                                          f"同池已有 {busy_heavy} 路重在跑，错峰等待"})
                continue
            if ROTATE_TOKENS > 0 and ctx >= ROTATE_TOKENS:
                woke = _rotate_wake(slot, sid, sess.get("directory") or "",
                                    ctx, state)
                budget -= 1
                if heavy:
                    busy_heavy += 1
                _wake_rr["last"] = slot
                results.append(woke)
                continue
            woke = _wake(slot, sid, sess.get("directory") or "", "空闲超时", state,
                         prompt=_resume_prompt(slot) if info else _seed_empty_prompt(slot))
            budget -= 1
            if heavy:
                busy_heavy += 1
            _wake_rr["last"] = slot
            woke["tokens"] = tokens
            results.append(woke)

        _maybe_beat(_summary(results, remaining))
        woke = [item for item in results
                if item.get("action") in ("woke", "rotate")]
        if woke:
            return {"action": "woke", "n": len(woke), "results": results,
                    "remaining": remaining}
        return {"action": "idle", "reason": "没有需要唤醒的会话",
                "results": results, "remaining": remaining}


def status() -> dict:
    state = _load_state()
    out = {"enabled": enabled(), "running": False, "lastTick": {},
           "interval": INTERVAL, "idleSeconds": IDLE_SECONDS,
           "stuckSeconds": STUCK_SECONDS, "cooldown": COOLDOWN,
           "hungSeconds": HUNG_SECONDS, "contextWarn": CONTEXT_WARN,
           "clientPatch": client_patch_status(), "slots": {},
           "modelGuard": {"allow": list(MODEL_ALLOW),
                          "interval": MODEL_GUARD_INTERVAL,
                          "grace": MODEL_GRACE,
                          "blocks": _load_blocks()}}
    with runner._lock:
        out["running"] = bool(runner._desired and runner._thread
                              and runner._thread.is_alive())
        out["lastTick"] = dict(runner._last_result or {})
        out["lastGuard"] = dict(runner._last_guard or {})
    for slot, st in state.items():
        out["slots"][slot] = {
            "at": st.get("at") or 0,
            "pid": st.get("pid") or 0,
            "alive": _pid_matches(int(st.get("pid") or 0)),
            "session": st.get("session") or "",
        }
    return out


class ReviveRunner:
    def __init__(self, interval: float = INTERVAL) -> None:
        self._interval = interval
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._guard_thread: threading.Thread | None = None
        self._desired = False
        self._stop = threading.Event()
        self._last_result: dict = {}
        self._last_guard: dict = {}

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return status()
            self._desired = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="revive-runner",
                                            daemon=True)
            self._thread.start()
            self._guard_thread = threading.Thread(target=self._guard_loop,
                                                  name="model-guard", daemon=True)
            self._guard_thread.start()
        return status()

    def stop(self) -> dict:
        with self._lock:
            self._desired = False
            self._stop.set()
        for thread in (self._thread, self._guard_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=3)
        return status()

    def _loop(self) -> None:
        while self._desired:
            try:
                self._last_result = tick()
            except Exception as err:  # noqa: BLE001
                self._last_result = {"action": "error", "reason": str(err)}
                log(f"唤醒巡检异常: {err}")
            self._stop.wait(self._interval)

    def _guard_loop(self) -> None:
        """通道硬闸独立线程：10s 一查，不跟 60s 唤醒巡检抢锁。"""
        while self._desired:
            try:
                self._last_guard = guard_tick()
            except Exception as err:  # noqa: BLE001
                self._last_guard = {"action": "error", "reason": str(err)}
                log(f"通道硬闸巡检异常: {err}")
            if self._stop.wait(MODEL_GUARD_INTERVAL):
                return


runner = ReviveRunner()
