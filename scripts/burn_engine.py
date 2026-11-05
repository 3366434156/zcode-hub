"""烧录引擎：按账号钉号 + 大上下文前缀缓存放大，把临期额度按可控速率烧完。

为什么有效（2026-11-03 实测）：
  - 上游 ``used_units`` = input_tokens + cache_read_input_tokens + output_tokens，
    缓存命中部分 **全额计入** 额度（执落：4,481,688 = 646,194+3,790,016+45,478）。
  - 13.5 万 token 前缀的请求，缓存命中后单次 1.6–11s，单账号可并发 5 路；
    5 账号稳态实测 210–292M tokens/h。
  - 全局粘号会把新对话吸到同一个账号（实测 5 条车道全落「执落2」），所以每条
    车道必须用 ``x-zcode-account`` 显式钉号。

安全阀：
  - ``data/burn/STOP`` 出现即停；
  - 默认 08:50 硬截止（额度 09:00 过期）；
  - 单账号连续失败指数退避，全局连续失败熔断；
  - 账号被判额度耗尽（1005 / exceed quota limit）后移出车道池；
  - pid 文件防重复启动。

用法：
  python scripts/burn_engine.py --lanes 5 --chars 400000
  python scripts/burn_engine.py --selftest
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

_ROOT = Path(__file__).resolve().parents[1]
DATA = _ROOT / "data"
BURN_DIR = DATA / "burn"
STATUS_FILE = BURN_DIR / "status.json"
LEDGER_FILE = BURN_DIR / "ledger.csv"
LOG_FILE = BURN_DIR / "engine.log"
PID_FILE = BURN_DIR / "engine.pid"
STOP_FILE = BURN_DIR / "STOP"


def _pid_alive(pid: int) -> bool:
    """只读探活。Windows 的 os.kill(pid, 0) 会 TerminateProcess 杀掉进程。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


BASE = "http://127.0.0.1:3000"
MODEL = "GLM-5.3-Flash"

_CORPUS_FILES = [
    "docs/development/05-upstream-protocols.md",
    "docs/development/06-dev-guide.md",
    "docs/development/04-gateway.md",
    "app/routes/gateway.py",
    "app/burner.py",
    "app/quota.py",
    "app/store.py",
    "README.md",
]

_TAIL_QUESTIONS = [
    "用一句话概括上面材料里最关键的结论。",
    "从上面材料里挑一个风险点，用一句话说明。",
    "用一句话说明这段材料涉及的协议端点。",
    "用一句话总结额度窗口的过期规则。",
    "上面材料里有没有并发相关的约束？一句话回答。",
    "用一句话说明缓存命中在计费里的口径。",
]


def _meta(key: str) -> str:
    conn = sqlite3.connect(DATA / "accounts.db")
    try:
        row = conn.execute("select value from meta where key=?", (key,)).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def build_corpus(chars: int) -> str:
    parts: list[str] = []
    total = 0
    for rel in _CORPUS_FILES:
        try:
            text = (_ROOT / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        parts.append(f"### FILE: {rel}\n{text}")
        total += len(text)
        if total >= chars:
            break
    blob = "\n\n".join(parts)
    if len(blob) < chars:
        blob = (blob + "\n") * (chars // max(1, len(blob)) + 1)
    return blob[:chars]


@dataclass
class Lane:
    idx: int
    account_id: str
    account_name: str
    prefix: str
    ok: int = 0
    fail: int = 0
    units: int = 0
    last_error: str = ""
    backoff_until: float = 0.0
    consecutive: int = 0
    dead: bool = False

    @property
    def stats(self) -> dict:
        return {"lane": self.idx, "account": self.account_name,
                "accountId": self.account_id, "ok": self.ok, "fail": self.fail,
                "units": self.units, "lastError": self.last_error,
                "dead": self.dead}


@dataclass
class Campaign:
    lanes: list[Lane]
    started: float = field(default_factory=time.time)
    requests: int = 0
    ok: int = 0
    fail: int = 0
    units: int = 0
    errors_total: int = 0
    last_error: str = ""
    stop_reason: str = ""
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _units_window: list[tuple[float, int]] = field(default_factory=list)
    _t0: float = field(default_factory=time.time)

    def rate_5m(self) -> float:
        now = time.time()
        self._units_window = [(t, u) for t, u in self._units_window if now - t < 300]
        if not self._units_window:
            return 0.0
        span = max(1.0, now - self._units_window[0][0])
        return sum(u for _, u in self._units_window) / span * 3600

    def overall_rate(self) -> float:
        span = max(1.0, time.time() - self._t0)
        return self.units / span * 3600

    async def record(self, lane: Lane, units: int, err: str, elapsed: float) -> None:
        async with self._lock:
            self.requests += 1
            if err:
                self.fail += 1
                self.errors_total += 1
                self.last_error = err
                lane.fail += 1
                lane.last_error = err
                lane.consecutive += 1
                lane.backoff_until = time.time() + min(60.0, 2.0 ** min(lane.consecutive, 5))
            else:
                self.ok += 1
                self.units += units
                lane.ok += 1
                lane.units += units
                lane.consecutive = 0
                lane.last_error = ""
                self._units_window.append((time.time(), units))
            with open(LEDGER_FILE, "a", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow([
                    int(time.time()), lane.idx, lane.account_name, lane.account_id,
                    "ERR" if err else "OK", units, round(elapsed, 3), err[:200],
                ])

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "running": not self.stop_reason,
            "startedAt": int(self.started),
            "updatedAt": int(now),
            "elapsed": int(now - self.started),
            "requests": self.requests,
            "ok": self.ok,
            "fail": self.fail,
            "units": self.units,
            "rate5m": round(self.rate_5m()),
            "rateOverall": round(self.overall_rate()),
            "lastError": self.last_error,
            "stopReason": self.stop_reason,
            "lanes": [lane.stats for lane in self.lanes],
        }

    def write_status(self) -> None:
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, STATUS_FILE)


async def warm_lane(client: httpx.AsyncClient, lane: Lane, key: str,
                    max_tokens: int, campaign: Campaign) -> None:
    """预热一次：把前缀写进该账号的上游缓存（失败交给主循环重试）。"""
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "stream": False,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": lane.prefix}]},
            {"role": "user", "content": [{"type": "text", "text": _TAIL_QUESTIONS[0]}]},
        ],
    }
    t0 = time.time()
    try:
        r = await client.post(f"{BASE}/v1/messages",
                              headers={"Authorization": f"Bearer {key}",
                                       "x-zcode-account": quote(lane.account_id)},
                              json=body)
    except Exception as err:  # noqa: BLE001
        await campaign.record(lane, 0, f"warm {type(err).__name__}: {err}", time.time() - t0)
        return
    await _consume(lane, r, campaign, t0, warm=True)


async def _consume(lane: Lane, r: httpx.Response, campaign: Campaign,
                   t0: float, warm: bool = False) -> None:
    elapsed = time.time() - t0
    if r.status_code != 200:
        text = r.text[:200]
        err = f"HTTP {r.status_code}: {text}"
        if "1005" in text or "exceed quota" in text.lower():
            lane.dead = True
            err = "额度耗尽: " + text
        await campaign.record(lane, 0, err, elapsed)
        return
    try:
        data = r.json()
    except ValueError:
        await campaign.record(lane, 0, "响应不是 JSON", elapsed)
        return
    u = data.get("usage") or {}
    units = (int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
             + int(u.get("cache_read_input_tokens") or 0))
    await campaign.record(lane, units, "", elapsed)


async def lane_loop(client: httpx.AsyncClient, lane: Lane, key: str,
                    max_tokens: int, campaign: Campaign, gap: float,
                    limit: int = 0) -> None:
    sent = 0
    while not campaign.stop_reason:
        if limit and sent >= limit:
            return
        if STOP_FILE.exists():
            campaign.stop_reason = "STOP 文件"
            return
        if lane.dead:
            return
        now = time.time()
        if lane.backoff_until > now:
            await asyncio.sleep(min(2.0, lane.backoff_until - now))
            continue
        body = {
            "model": MODEL,
            "max_tokens": max_tokens,
            "stream": False,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": lane.prefix}]},
                {"role": "user", "content": [{"type": "text",
                                              "text": random.choice(_TAIL_QUESTIONS)}]},
            ],
        }
        t0 = time.time()
        try:
            r = await client.post(f"{BASE}/v1/messages",
                                  headers={"Authorization": f"Bearer {key}",
                                           "x-zcode-account": quote(lane.account_id)},
                                  json=body)
        except Exception as err:  # noqa: BLE001
            await campaign.record(lane, 0, f"{type(err).__name__}: {err}", time.time() - t0)
            await asyncio.sleep(1)
            continue
        await _consume(lane, r, campaign, t0)
        sent += 1
        if gap > 0:
            await asyncio.sleep(gap)


async def _load_accounts(admin_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/admin/api/accounts",
                        headers={"Authorization": f"Bearer {admin_key}"})
        r.raise_for_status()
    data = r.json()
    rows = data.get("accounts") if isinstance(data, dict) else data
    out: list[dict] = []
    for a in rows or []:
        if a.get("provider") != "zai":
            continue
        if not a.get("enabled"):
            continue
        if a.get("status") not in ("active",):
            continue
        quota = (a.get("quota") or {}).get(MODEL) or {}
        remaining = quota.get("remaining")
        if remaining is not None and remaining <= 0:
            continue
        out.append({"id": a.get("id"), "name": a.get("name"),
                    "remaining": remaining})
    out.sort(key=lambda x: (-(x["remaining"] or 0), x["name"] or ""))
    return out


def _deadline_ts() -> float:
    """当天 08:50 的本地时间戳；已过则用当前+8h 兜底（防误配）。"""
    now = time.localtime()
    ts = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 8, 50, 0, 0, 0, -1))
    return ts if ts > time.time() else time.time() + 8 * 3600


async def selftest() -> None:
    key = _meta("gateway_key")
    accounts = await _load_accounts(_meta("admin_key") or "zcode")
    print("accounts:", [(a["name"], a["remaining"]) for a in accounts])
    if not accounts:
        print("没有可用账号")
        return
    corpus = build_corpus(400_000)
    acc = accounts[0]
    lane = Lane(0, acc["id"], acc["name"], f"[selftest {time.time_ns()}]\n{corpus}")
    campaign = Campaign([lane])
    async with httpx.AsyncClient(timeout=600) as client:
        await warm_lane(client, lane, key, 16, campaign)
        await lane_loop(client, lane, key, 16, campaign, 0, limit=3)
    print(json.dumps(campaign.snapshot(), ensure_ascii=False, indent=1))


async def main() -> None:
    global STATUS_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--lanes", type=int, default=0)
    ap.add_argument("--chars", type=int, default=400_000)
    ap.add_argument("--max-tokens", type=int, default=16)
    ap.add_argument("--gap", type=float, default=0.0)
    ap.add_argument("--duration", type=int, default=0, help="秒；0=跑到 08:50")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    BURN_DIR.mkdir(parents=True, exist_ok=True)
    if args.selftest:
        await selftest()
        return

    if PID_FILE.exists():
        try:
            old = int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            old = 0
        if _pid_alive(old):
            print(f"已有引擎在跑 pid={old}，退出")
            return

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    if STOP_FILE.exists():
        STOP_FILE.unlink()

    key = _meta("gateway_key")
    admin_key = _meta("admin_key") or "zcode"
    accounts = await _load_accounts(admin_key)
    if not accounts:
        log("没有可用账号，退出")
        return
    lanes_n = args.lanes or len(accounts)
    corpus = build_corpus(args.chars)
    run_id = f"{int(time.time())}"
    lanes: list[Lane] = []
    for i in range(lanes_n):
        acc = accounts[i % len(accounts)]
        lanes.append(Lane(i, acc["id"], acc["name"],
                          f"[burn run {run_id} lane {i}]\n{corpus}"))
    campaign = Campaign(lanes)
    deadline = (time.time() + args.duration) if args.duration else _deadline_ts()
    log(f"启动：{lanes_n} 车道 / {args.chars} chars / 账号池 {len(accounts)} / "
        f"截止 {time.strftime('%m-%d %H:%M', time.localtime(deadline))}")

    async def watchdog() -> None:
        while not campaign.stop_reason:
            if time.time() >= deadline:
                campaign.stop_reason = "到达硬截止时间"
                return
            if STOP_FILE.exists():
                campaign.stop_reason = "STOP 文件"
                return
            await asyncio.sleep(2)

    async with httpx.AsyncClient(timeout=600) as client:
        log("预热各车道前缀缓存…")
        for lane in lanes:
            await warm_lane(client, lane, key, args.max_tokens, campaign)
            log(f"  lane {lane.idx} → {lane.account_name} warm "
                f"units={lane.units} ok={lane.ok} fail={lane.fail} {lane.last_error}")
        tasks = [asyncio.create_task(watchdog())]
        tasks += [asyncio.create_task(lane_loop(client, lane, key, args.max_tokens,
                                                campaign, args.gap)) for lane in lanes]
        while not campaign.stop_reason:
            campaign.write_status()
            await asyncio.sleep(5)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    campaign.write_status()
    log(f"停止：{campaign.stop_reason} requests={campaign.requests} ok={campaign.ok} "
        f"fail={campaign.fail} units={campaign.units} "
        f"overall={campaign.overall_rate()/1e6:.1f}M/h")
    try:
        PID_FILE.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
