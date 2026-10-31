"""客户端版本巡检（默认只体检，不自动跟进）。

为什么不再「自动跟进」
----------------------
上游按客户端版本门槛投放活动套餐（docs/development/05 §5 两次实证：钉 3.14.4 时
billing/preview 恒空），所以历史上确实出现过「官方一发版、活动就对整池不可见」。
但 2026-10-30 23:00 实测证明**盲目跟版本更危险**：同账号、同配额窗口、同 CLI 形态下
`app_version=3.16.3` → HTTP 200；`app_version=3.17.1` → HTTP 405
`{"code":3012,"msg":"unusual activity"}`（整池 5 个账号全中，全部进风控冷却）。
官方 CLI 的请求形状（UA 后缀 / 签名）随版本变了，版本号不是唯一变量。

因此本模块的职责是**体检**：
- 启动读 `data/app_version.json`（上次确认过的官方版本，纯记录，不自动生效）；
- 后台每 `VERSION_SYNC_INTERVAL`（默认 6h）拉一次官方发行清单，和当前钉版比对；
- 版本不一致时把它写进日志与 `/admin/api/status`（`latest` vs `effective`），
  让「活动突然不可见」有一个可查的口径，而不是靠人肉翻文档。

要自动跟进（不推荐，必须自己确认 messages 形状没变）：
`ZCODE_APP_VERSION_FOLLOW=1` → 发现更新版本时经 `constants.set_client_version()`
全量改写（UA / 版本头 / configs 查询串）。只升不降，改写前请先跑一次真实对话，
确认不是 3012；`docs/development/05 §5` 有完整的验证步骤。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid

import httpx

from . import constants, logs, settings

_VERSION_LINE = re.compile(r"^version:\s*([0-9]+(?:\.[0-9]+)*)\s*$", re.MULTILINE)
_PERSIST_NAME = "app_version.json"


def parse_manifest_version(text: str) -> str | None:
    """从 electron 发行清单 YAML 里取 version 字段（只认顶格 version: 行）。

    不引入 YAML 依赖：清单是 electron-updater 的固定形状，`version:` 顶格出现
    一次即可。取不到/非法 → None（调用方保持当前版本）。
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_LINE.search(text)
    return match.group(1) if match else None


def _version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.strip().split("."))
    except (AttributeError, ValueError):
        return (0,)


def is_newer(candidate: str, current: str) -> bool:
    """candidate 是否严格新于 current（纯数字点分版本；不同段数按缺省 0 补齐）。"""
    a = _version_tuple(candidate)
    b = _version_tuple(current)
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return a > b


def _persist_path():
    return settings.DATA_DIR / _PERSIST_NAME


def load_saved_version() -> str | None:
    """读上次巡检记录的官方版本（文件缺失/损坏 → None，不报错）。"""
    try:
        payload = json.loads(_persist_path().read_text("utf-8"))
    except (OSError, ValueError):
        return None
    version = str((payload or {}).get("version") or "").strip()
    return version or None


def _save_version(version: str) -> None:
    import time

    try:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _persist_path().write_text(
            json.dumps({"version": version, "saved_at": int(time.time())}, ensure_ascii=False),
            "utf-8",
        )
    except OSError as err:  # noqa: BLE001 - 落盘失败只影响下次巡检的展示
        logs.warn("version", f"版本记录落盘失败（仅本次运行内可见）: {err}")


async def fetch_latest_version(timeout: float = 15) -> str | None:
    """拉官方发行清单拿最新版本；失败返回 None（不抛）。

    注意清单是**按设备灰度投放**的：同一时刻不同 device_mid 可能拿到 3.16.3 或
    3.17.1（2026-10-30 实测连续 3 次里出现过 1 次旧值）。所以这里用本机稳定的
    device_mid（官方客户端同款口径），并且 `_tick` 只接受「更高」的版本，避免灰度
    抖动让告警反复横跳。
    """
    try:
        from .quota import device_mid

        mid = device_mid()
    except Exception:  # noqa: BLE001 - 拿不到就临时生成（仅影响灰度分组）
        mid = str(uuid.uuid4())
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.get(
                settings.ZCODE_RELEASES_URL,
                params={"platform": "windows-x86_64", "device_mid": mid},
                headers={"Accept": "application/x-yaml,text/yaml,text/plain,*/*",
                         "User-Agent": f"ZCode/{constants.CLIENT_APP_VERSION}"},
            )
    except httpx.HTTPError as err:
        logs.warn("version", f"官方发行清单拉取失败: {err}")
        return None
    if res.status_code >= 400:
        logs.warn("version", f"官方发行清单 HTTP {res.status_code}")
        return None
    version = parse_manifest_version(res.text)
    if not version:
        logs.warn("version", "官方发行清单里没有可解析的 version 字段")
    return version


class VersionSync:
    """后台版本巡检轮（启动读记录 + 周期拉清单比对；跟进需显式开启）。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.current: str = constants.CLIENT_APP_VERSION
        self.latest: str | None = load_saved_version()

    # ── 跟进（仅 ZCODE_APP_VERSION_FOLLOW=1 时调用）──────────────────────────
    def adopt(self, version: str | None) -> bool:
        """把版本切到 version（仅严格更新时生效）。返回是否发生了切换。

        ⚠️ 调用方必须先确认新版本的 messages 形状可用（3.17.1 实测 3012）；
        默认配置下巡检不会走到这里。
        """
        v = (version or "").strip()
        if not v or not is_newer(v, self.current):
            return False
        previous = self.current
        constants.set_client_version(v)
        self.current = v
        _save_version(v)
        logs.ok("version", f"客户端版本跟进到 {v}（原 {previous}）")
        return True

    # ── 轮次 ─────────────────────────────────────────────────────────────────
    async def _tick(self) -> None:
        version = await fetch_latest_version()
        if not version:
            return
        # 官方清单按设备灰度，可能回落：只接受「更高」的版本，避免告警反复横跳
        if self.latest and not is_newer(version, self.latest):
            return
        self.latest = version
        _save_version(version)
        if settings.APP_VERSION_FOLLOW and self.adopt(version):
            return
        if version == self.current:
            logs.info("version", f"客户端版本与官方最新一致（{version}）")
            return
        hint = ("已开启自动跟进，但新版本不高于当前钉版" if settings.APP_VERSION_FOLLOW
                else "自动跟进已关闭（官方 3.17.x 起 messages 形状变化，实测 3012）")
        logs.warn(
            "version",
            f"官方客户端已到 {version}，当前钉 {self.current}；{hint}。"
            f"若活动套餐突然不可见，先按 docs/development/05 §5 验证新版本形状",
        )

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as err:  # noqa: BLE001 - 后台轮必须自兜
                logs.warn("version", f"版本巡检轮异常: {err}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.VERSION_SYNC_INTERVAL)
                return
            except TimeoutError:
                continue

    def start(self) -> None:
        """启动后台巡检轮（幂等）。只读清单，不改版本（除非显式开启跟进）。"""
        saved = load_saved_version()
        if saved:
            self.latest = saved
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except TimeoutError:
            pass
        except Exception as err:  # noqa: BLE001 - 停服不冒泡
            logs.warn("version", f"版本巡检轮退出异常: {err}")
        self._task = None


version_sync = VersionSync()
