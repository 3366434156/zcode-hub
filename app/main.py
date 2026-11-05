"""FastAPI 应用工厂 + 生命周期。"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import constants, logs, settings
from .burner import runner as burner_runner
from .captcha import captcha_manager
from .claim import claim_round
from .quota import monitor
from .reviver import audit_bindings as revive_audit_bindings
from .reviver import ensure_enabled as revive_ensure_enabled
from .reviver import runner as revive_runner
from .routes import admin_api, admin_burner, admin_logs, admin_tunnel, gateway, pages
from .version_sync import version_sync

# 修正 Windows 中文控制台可能出现的乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


class _LogstoreHandler(logging.Handler):
    """把 uvicorn 的 access / error 日志接进网关日志台账。

    uvicorn 走标准 logging，不经过 app.logs：不接的话「谁在什么时候打了什么
    请求、进程最后一条输出是什么」就只剩 launcher 窗口里有，窗口一关全丢。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from .logstore import logstore

            tag = "access" if record.name == "uvicorn.access" else "uvicorn"
            level = ("INFO" if record.levelno < logging.WARNING
                     else "WARN" if record.levelno < logging.ERROR else "ERROR")
            logstore.write(level, tag, record.getMessage(),
                           req_id=str(getattr(record, "req_id", "") or ""))
        except Exception:  # noqa: BLE001 - 日志旁路失败不影响服务
            pass


def _attach_uvicorn_logging() -> None:
    handler = _LogstoreHandler()
    # 只挂 uvicorn 与 uvicorn.access：uvicorn.error 会冒泡到 uvicorn（不另挂，
    # 否则同一条记录父子各记一次，日志文件里全是重复行）。uvicorn 默认把
    # access 的 propagate 关了，所以它必须单独挂。
    for name in ("uvicorn", "uvicorn.access"):
        logger = logging.getLogger(name)
        # 只挂一次：create_app 可能被测试反复调用
        if not any(isinstance(h, _LogstoreHandler) for h in logger.handlers):
            logger.addHandler(handler)


def _display_host() -> str:
    # 0.0.0.0 / 空地址在浏览器中不可直接访问，展示为 127.0.0.1
    host = (settings.HOST or "").strip()
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


def _backfill_fingerprints() -> list:
    """启动时给无指纹或非桌面 SKU（旧版宿主机克隆）的存量账号换发生成档案。

    换机后清 installed_at，由 lifespan 补跑按账号安装序（新 device_mid 需要
    自己的 app_launch / app_daily_active）。返回被替换的账号列表。
    """
    from .fingerprint import DeviceProfile, is_generated_sku, profile_for
    from .store import store

    replaced = []
    for account in store.list_accounts():
        fp = account.fingerprint
        needs = False
        if not isinstance(fp, dict) or not fp.get("device_mid"):
            needs = True
        else:
            try:
                profile = DeviceProfile(
                    platform=fp["platform"], arch=fp["arch"], os_version=fp["os_version"],
                    language=fp["language"], timezone=fp["timezone"], screen=fp["screen"],
                    device_mid=fp["device_mid"],
                )
            except (KeyError, TypeError, ValueError):
                needs = True
            else:
                needs = not is_generated_sku(profile)
        if needs:
            account.fingerprint = None
            account.installed_at = None
            profile_for(account)
            store._assign_fingerprint(account)
            store.update_account(account)
            replaced.append(account)
    return replaced


def _backfill_install_ids() -> int:
    """启动时给无 install_id 的存量账号补配安装身份（纯本地，无网络）并落库。"""
    import uuid as _uuid

    from .store import store

    backfilled = 0
    for account in store.list_accounts():
        if not (account.install_id or "").strip():
            account.install_id = str(_uuid.uuid4())
            store.update_account(account)
            backfilled += 1
    return backfilled


# 启动安装序的后台任务引用：事件循环对 task 只持弱引用（asyncio 官方文档），
# 不保存引用任务可能被 GC 中途丢弃且无日志 —— 与 captcha._refill_task 同一模式
_install_task: asyncio.Task | None = None


def _run_install_sequence_on_start() -> None:
    """启动后台执行一次安装序（官方客户端每次启动都拉 configs + 发 app_launch，
    日活去重在上游按 device_mid+日期）。失败只留痕，绝不影响启动。"""
    global _install_task

    from . import install

    async def _run() -> None:
        try:
            await install.run_install_sequence()
        except Exception as err:  # noqa: BLE001 —— 后台任务异常无人接收，必须自兜
            logs.err("install", f"安装序意外异常: {err}")

    _install_task = asyncio.create_task(_run())


@asynccontextmanager
async def lifespan(app: FastAPI):
    replaced = _backfill_fingerprints()
    if replaced:
        logs.ok("fingerprint", f"存量账号补配独立设备指纹 ×{len(replaced)}")
        for acc in replaced:
            admin_api._schedule_install(acc)
    installed = _backfill_install_ids()
    if installed:
        logs.ok("install", f"存量账号补配安装身份 ×{installed}")
    monitor.start()
    captcha_manager.start()   # 验证码预解池后台补充
    version_sync.start()      # 客户端版本巡检（官方最新 vs 钉版；跟进需显式开启）
    claim_round.start()       # 套餐自动领取轮（间隔 0 = 关闭）
    burner_runner.start()     # 任务工单队列（续派页；总闸关闭时只空转）
    revive_ensure_enabled()   # 会话唤醒器：兜底客户端停止续派/回合异常
    # 启动审计：绑定到非网关模型的历史会话一律摘除并封锁（2026-11-05 事故）
    try:
        audit = revive_audit_bindings()
        if any(item.get("action") == "blocked" for item in audit.get("results", [])):
            logs.warn("revive", f"启动通道审计：已封锁 {sum(1 for i in audit['results'] if i.get('action') == 'blocked')} 个炉位")
    except Exception as err:  # noqa: BLE001 - 审计失败不能拖垮网关启动
        logs.warn("revive", f"启动通道审计异常: {err}")
    revive_runner.start()
    _run_install_sequence_on_start()
    from .tunnel import tunnel

    tunnel.autostart()        # 公网隧道（ZCODE_TUNNEL=1 或后台开过）
    base = f"http://{_display_host()}:{settings.PORT}"
    logs.banner([
        f"{logs._B}{logs._MAG}zcode-hub{logs._R} {logs._DIM}v{settings.APP_VERSION} · Python{logs._R}",
        f"{logs._DIM}后台管理{logs._R}  {logs._C}{base}/admin/login{logs._R}",
        f"{logs._DIM}对话端点{logs._R}  {logs._C}{base}/v1/messages{logs._R}",
        f"{logs._DIM}客户端版本{logs._R}  {logs._C}{constants.CLIENT_APP_VERSION}{logs._R}"
        f"{logs._DIM}（钉版；官方最新见 /admin/api/status）{logs._R}",
    ])
    try:
        yield
    finally:
        tunnel.stop(persist=False)
        revive_runner.stop()
        burner_runner.stop()
        await monitor.stop()
        await version_sync.stop()
        await claim_round.stop()
        await captcha_manager.close()


def create_app() -> FastAPI:
    _attach_uvicorn_logging()
    app = FastAPI(title="zcode-hub", version=settings.APP_VERSION, lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=str(settings.FRONTEND_DIR)), name="static")

    app.include_router(pages.router)
    app.include_router(admin_api.router)
    # 续派工单（zburn 合并进来的队列/派单/验收）
    app.include_router(admin_burner.router)
    # 网关日志单独一个 router：它的数据源（logstore）与请求台账完全独立，
    # 拆开可以让「日志」随时增删而不碰 admin_api 这个已经很大的文件
    app.include_router(admin_logs.router, prefix="/admin/api")
    app.include_router(admin_tunnel.router)
    app.include_router(gateway.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        """看门狗探针：无鉴权、不碰上游、不读账号库，只证明进程还能响应。"""
        return {"ok": True, "version": settings.APP_VERSION, "ts": time.time()}

    return app


app = create_app()
