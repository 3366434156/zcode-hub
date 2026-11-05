"""一次性探针：验证大上下文请求经网关的可行性、计时与 usage 口径。

用法：python scripts/_probe_burn.py [chars] [max_tokens]
"""
from __future__ import annotations

import json
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


def build_context(chars: int) -> str:
    parts: list[str] = []
    files = [
        "docs/development/05-upstream-protocols.md",
        "docs/development/06-dev-guide.md",
        "app/routes/gateway.py",
        "app/burner.py",
        "app/quota.py",
        "README.md",
    ]
    total = 0
    for f in files:
        try:
            text = open(f, encoding="utf-8").read()
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


def main() -> None:
    chars = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    ctx = build_context(chars)
    body = {
        "model": "GLM-5.3-Flash",
        "max_tokens": max_tokens,
        "stream": False,
        "messages": [{"role": "user", "content": [{"type": "text", "text": ctx}]}],
    }
    t0 = time.time()
    with httpx.Client(timeout=300) as c:
        r = c.post(f"{BASE}/v1/messages",
                   headers={"Authorization": f"Bearer {KEY}"},
                   json=body)
    dt = time.time() - t0
    print("status", r.status_code, "elapsed", round(dt, 2), "s", "chars", len(ctx))
    try:
        data = r.json()
    except ValueError:
        print(r.text[:2000])
        return
    print(json.dumps(data, ensure_ascii=False)[:2500])


if __name__ == "__main__":
    main()
