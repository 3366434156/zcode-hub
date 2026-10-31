"""pytest 共享夹具。

设计要点（docs/testing/01 §可测性约束）：
- Mock 上游以真实 uvicorn 端口拉起（session 级），网关内部自建的 httpx 客户端
  直接走真实 TCP —— 与生产行为同形，SSE 流式也真实经过网络栈。
- 底座的 store 是模块级单例，gateway/admin/quota/auth_admin 各自绑定了名字，
  fresh_app 在所有绑定点重绑到同一个新实例，并隔离 data 目录。
- captcha_manager 被替换为桩（避免打到真实 zcode.z.ai）。
"""

from __future__ import annotations

import importlib
import os
import threading
import time

import pytest
import pytest_asyncio
import uvicorn
from httpx import ASGITransport, AsyncClient

from app import settings
from tests.mock_upstream import server as mock_server_module

# 开发机常驻系统代理（如 Clash 监听 127.0.0.1:7897）时，httpx 默认 trust_env=True
# 会把发往 127.0.0.1 的请求也交给代理（httpx 不读 macOS 例外列表），断连场景会被
# 代理转译成 502。测试进程内全局屏蔽代理，保证网关→Mock 走真实 TCP。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

# 底座中绑定了 store 名字的全部模块 —— 新增导入点时必须同步加入
_STORE_BINDING_MODULES = (
    "app.store",
    "app.captcha",
    "app.routes.gateway",
    "app.routes.admin_api",
    "app.quota",
    "app.auth_admin",
)

# 绑定了请求台账单例（usagelog）的模块 —— 同上，新增导入点必须同步
_USAGE_BINDING_MODULES = (
    "app.usagelog",
    "app.reqlog",
    "app.routes.admin_api",
)


@pytest.fixture(autouse=True)
def _reset_model_pool():
    """每个用例前后清空模型兜底台账（进程内单例，跨用例会互相污染）。

    `app/modelpool.py` 的 `modelpool` 是模块级单例：反应式判死标记（1005/3006）
    与「实测可用」的信任票都挂在进程内存里，不随 `fresh_app` 换 store 而重置
    （它每次调用才 `from .store import store` 取当前 store，所以清内部状态就够）。

    不清的后果是真实的：某个用例把整条兜底链判死后，后续用例请求的模型会被
    静默换成别的模型（如 Flash 被判死 → 请求 Flash 却拿到 GLM-5.3），失败点
    离真正的肇事用例很远。这里统一在进出各清一次，用例之间互不影响。
    """
    from app.modelpool import modelpool

    modelpool.clear()
    yield
    modelpool.clear()


@pytest.fixture(autouse=True)
def _restore_client_version():
    """客户端版本是模块级可变状态（version_sync 运行期改写）：用例之间必须复位。

    不复位的后果：某个用例把版本跟进到 9.9.9 后，后续断言 constants 的用例
    （test_constants）会莫名失败，且失败点离肇事用例很远。
    """
    from app import constants

    original = constants.CLIENT_APP_VERSION
    yield
    constants.set_client_version(original)


@pytest.fixture(scope="session")
def mock_server():
    """真实端口上的 Mock 上游。返回 (app, port)。

    用模块级 `mock_server_module.app`（含 _wrap_asgi 断连包装）而非裸 build_app()：
    connect_fail_first 的真断连判定发生在 FastAPI 之外的包装层。
    """
    app = mock_server_module.app
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started, "mock upstream failed to start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield app, port
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def fresh_app(tmp_path, monkeypatch):
    """独立 app 实例：隔离 data 目录、重绑所有 store 绑定点。"""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ZCODE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(settings, "DB_PATH", data_dir / "accounts.db")
    monkeypatch.setattr(settings, "USAGE_DB_PATH", data_dir / "usage.db")
    monkeypatch.setattr(settings, "COOLING_SECONDS", 300)

    from app import store as store_module
    from app.store import Store

    fresh = Store()
    for mod_name in _STORE_BINDING_MODULES:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "store"):
            monkeypatch.setattr(mod, "store", fresh)
    assert store_module.store is fresh
    from app import auth_admin as auth_admin_module
    if hasattr(auth_admin_module, "reset_failures"):
        auth_admin_module.reset_failures()

    # 请求台账同样是模块级单例（导入时就绑定了真实 data/usage.db）：
    # 换到临时文件，否则测试会把记录写进用户真实台账
    from app import usagelog as usagelog_module
    fresh_usage = usagelog_module.UsageLog(data_dir / "usage.db")
    for mod_name in _USAGE_BINDING_MODULES:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "usagelog"):
            monkeypatch.setattr(mod, "usagelog", fresh_usage)
    yield fresh
    fresh_usage.close()
    return


class _StubCaptcha:
    """验证码桩：永不打真网。"""

    def __init__(self) -> None:
        self.invalidated = 0

    async def get_verify_param(self, port: int | None = None) -> tuple[str, str | None]:
        return "mock-verify-param", None

    def invalidate(self) -> None:
        self.invalidated += 1


@pytest.fixture
def stub_captcha() -> _StubCaptcha:
    """验证码桩实例。gateway_client 依赖注入同一实例，测试可直接断言其调用。"""
    return _StubCaptcha()


@pytest_asyncio.fixture
async def gateway_client(fresh_app, mock_server, monkeypatch, stub_captcha):
    """挂好 Mock 上游的网关 ASGI 客户端。返回 (client, mock_app)。"""
    mock_app, port = mock_server
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(settings, "UPSTREAM", {
        "zai": f"{base}/api/v1/zcode-plan/anthropic/v1/messages",
        "zai_fallback": f"{base}/api/anthropic/v1/messages",
        "bigmodel": f"{base}/api/anthropic/v1/messages",
    })
    # 后台额度刷新也指向 Mock（billing/current|balance|usage）
    monkeypatch.setattr(settings, "ZCODE_BILLING_BASE", f"{base}/api/v1/zcode-plan")
    # 激活事件上报也指向 Mock
    monkeypatch.setattr(settings, "ZCODE_EVENT_REPORT_URL", f"{base}/api/v1/event/report")
    # 运营位（限时套餐第二通道）+ 官方发行清单（版本跟进）也指向 Mock
    monkeypatch.setattr(settings, "ZCODE_MARKETING_URL", f"{base}/api/v1/marketing/touch")
    monkeypatch.setattr(settings, "ZCODE_RELEASES_URL",
                        f"{base}/api/v1/releases/electron/manifest")
    # OAuth（cli init/poll + api-key 兑换链）全部收敛到 Mock —— 测试永不打真网
    monkeypatch.setattr(settings, "OAUTH_API_BASE", f"{base}/api/v1")
    monkeypatch.setattr(settings, "ZAI_EXCHANGE_ORIGIN", base)

    from app.routes import gateway as gateway_module
    monkeypatch.setattr(gateway_module, "captcha_manager", stub_captcha)

    from app.main import create_app
    gateway = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=gateway), base_url="http://gateway.test"
    ) as client:
        yield client, mock_app


def seed_account(store, secret: str, name: str = "t"):
    return store.add_account("zai", name, secret)
