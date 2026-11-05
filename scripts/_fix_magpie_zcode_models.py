"""把 magpie 的 zcode agent 模型指向本地网关（zcode2api）。

背景（2026-11-04 23:00）：magpie 的 pickedModels.zcode 被重置成 `auto/*` 自动路由，
auto 把请求分给了官方 provider（已欠费）→ 上游 403 insufficient balance，五炉位
全部空转，本地网关 1500M 额度一分没用。这里把 `zcode2api/*` 放到列表最前并摘掉
auto/*，让 CLI 的请求回到本地网关。改前自动备份，可随时还原。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

SETTINGS = Path.home() / ".config" / "magpie" / "settings.json"
GATEWAY_MODELS = ("zcode2api/GLM-5.3-Flash", "zcode2api/GLM-5.3")


def main() -> None:
    raw = SETTINGS.read_text("utf-8")
    data = json.loads(raw)
    backup = SETTINGS.with_name(
        SETTINGS.name + time.strftime(".bak-zcodehub-%Y%m%d-%H%M%S"))
    shutil.copy2(SETTINGS, backup)

    picked = list((data.get("pickedModels") or {}).get("zcode") or [])
    before = list(picked)

    for model in GATEWAY_MODELS:
        while model in picked:
            picked.remove(model)
        picked.insert(0, model)

    dropped = [m for m in picked if m.startswith("auto/")]
    picked = [m for m in picked if not m.startswith("auto/")]
    data.setdefault("pickedModels", {})["zcode"] = picked

    SETTINGS.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print("backup:", backup.name)
    print("before[:3]:", before[:3])
    print("after[:4]:", picked[:4])
    print("dropped auto/*:", len(dropped), "| remaining:", len(picked))


if __name__ == "__main__":
    main()
