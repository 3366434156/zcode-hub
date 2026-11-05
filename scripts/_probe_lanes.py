"""车道探针：5 条独立对话（各自唯一前缀）先预热建缓存，再并行重复发。

验证稳态：每条车道粘在自己的账号上、每次命中自己那段 prefix cache 时的
真实往返时间与计费口径（input + cache_read + output）。

用法：python scripts/_probe_lanes.py [lanes] [chars] [rounds] [max_tokens]
"""
from __future__ import annotations

import concurrent.futures as cf
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:3000"
_ROOT = Path(__file__).resolve().parents[1]


def _meta(key: str) -> str:
    conn = sqlite3.connect(_ROOT / "data" / "accounts.db")
    try:
        row = conn.execute("select value from meta where key=?", (key,)).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


KEY = _meta("gateway_key")
_FILES = [
    "docs/development/05-upstream-protocols.md",
    "docs/development/06-dev-guide.md",
    "app/routes/gateway.py",
    "app/burner.py",
    "app/quota.py",
    "README.md",
]


def build_context(chars: int) -> str:
    parts: list[str] = []
    total = 0
    for f in _FILES:
        try:
            text = (_ROOT / f).read_text(encoding="utf-8")
        except OSError:
            continue
        parts.append(f"### FILE: {f}\n{text}")
        total += len(text)
        if total >= chars:
            break
    blob = "\n\n".join(parts)
    if len(blob) < chars:
        blob = (blob + "\n") * (chars // max(1, len(blob)) + 1)
    return blob[:chars]


def ask(text: str, max_tokens: int) -> dict:
    body = {"model": "GLM-5.3-Flash", "max_tokens": max_tokens, "stream": False,
            "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/v1/messages",
                       headers={"Authorization": f"Bearer {KEY}"},
                       json=body, timeout=600)
        dt = time.time() - t0
        data = r.json()
    except Exception as err:  # noqa: BLE001
        return {"code": -1, "dt": time.time() - t0, "err": str(err)[:100], "units": 0}
    u = data.get("usage") or {}
    units = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
             + u.get("cache_read_input_tokens", 0))
    return {"code": r.status_code, "dt": dt, "inp": u.get("input_tokens", 0),
            "cached": u.get("cache_read_input_tokens", 0),
            "out": u.get("output_tokens", 0), "units": units,
            **({"err": str(data)[:100]} if r.status_code != 200 else {})}


def main() -> None:
    lanes = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    chars = int(sys.argv[2]) if len(sys.argv) > 2 else 400_000
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    max_tokens = int(sys.argv[4]) if len(sys.argv) > 4 else 16
    ctx = build_context(chars)
    prompts = [f"[lane {i} / burn-probe]\n{ctx}" for i in range(lanes)]

    print(f"预热 {lanes} 条车道（串行，确保轮询分到不同账号）…")
    for i, p in enumerate(prompts):
        r = ask(p, max_tokens)
        print(f"  warm {i}: {r}")
        time.sleep(1)

    for rd in range(rounds):
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=lanes) as pool:
            rows = list(pool.map(lambda p: ask(p, max_tokens), prompts))
        wall = time.time() - t0
        units = sum(r["units"] for r in rows)
        ok = sum(1 for r in rows if r["code"] == 200)
        cached = sum(r.get("cached", 0) for r in rows)
        print(f"round {rd}: ok={ok}/{lanes} wall={wall:.2f}s units={units} "
              f"cached={cached} rate={units / wall * 3600 / 1e6:.1f}M/h "
              f"times={[round(r['dt'], 1) for r in rows]}")


if __name__ == "__main__":
    main()
