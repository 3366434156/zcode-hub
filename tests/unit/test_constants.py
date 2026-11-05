"""constants 收口守护：上游常量的漂移断言（docs/development/03/05 的实证值）。

改动这里的期望值 = 有意变更上游协议口径，必须同步更新 docs/development/05。
"""

from __future__ import annotations

from app import constants


def test_messages_urls():
    assert constants.MESSAGES_URLS["zai"] == \
        "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages"
    assert constants.MESSAGES_URLS["zai_fallback"] == \
        "https://api.z.ai/api/anthropic/v1/messages"
    assert constants.MESSAGES_URLS["bigmodel"] == \
        "https://open.bigmodel.cn/api/anthropic/v1/messages"


def test_billing_base():
    assert constants.BILLING_BASE == "https://zcode.z.ai/api/v1/zcode-plan"


def test_client_configs():
    assert constants.CLIENT_CONFIGS_URL == "https://zcode.z.ai/api/v1/client/configs"
    # 实测带 platform 参数上游 3001，只允许 app_version
    assert constants.client_configs_query() == "app_version=3.16.3"


def test_client_version_single_source():
    # 钉 3.16.3 = 实证可用的 CLI 请求形状（2026-10-30 23:00：同账号 3.16.3 → 200，
    # 3.17.1 → 405/3012 整池进风控）。版本巡检在 app/version_sync.py。
    # 全部版本出口必须引用同一常量，禁止再出现字面量版本号
    assert constants.CLIENT_APP_VERSION == "3.16.3"
    assert constants.CLI_TITLE == "Z Code@cli"
    assert constants.CLI_RELEASE_CHANNEL == "production"
    assert constants.X_ZCODE_APP_VERSION == constants.CLIENT_APP_VERSION
    assert constants.USER_AGENT == f"ZCode/{constants.CLIENT_APP_VERSION}"
    assert constants.CLIENT_PLATFORM == "darwin-arm64"  # asar TH() = platform-arch


def test_set_client_version_keeps_single_source():
    """运行期跟进（version_sync）也必须保持「改一处、全出口一致」。"""
    original_client = constants.CLIENT_APP_VERSION
    original_billing = constants.BILLING_APP_VERSION
    try:
        constants.set_client_version("9.9.9")
        assert constants.CLIENT_APP_VERSION == "9.9.9"
        assert constants.BILLING_APP_VERSION == "9.9.9"
        assert constants.USER_AGENT == "ZCode/9.9.9"
        assert constants.X_ZCODE_APP_VERSION == "9.9.9"
        assert constants.client_configs_query() == "app_version=9.9.9"
        constants.set_client_version("")   # 空值不改写
        assert constants.CLIENT_APP_VERSION == "9.9.9"
    finally:
        constants.CLIENT_APP_VERSION = original_client
        constants.BILLING_APP_VERSION = original_billing
        constants.USER_AGENT = f"ZCode/{original_client}"
        constants.X_ZCODE_APP_VERSION = original_client


def test_set_billing_version_only_moves_billing():
    """billing 版本独立：只升不降，不碰 messages 钉版。"""
    original_client = constants.CLIENT_APP_VERSION
    original_billing = constants.BILLING_APP_VERSION
    try:
        constants.set_billing_version("9.9.9")
        assert constants.BILLING_APP_VERSION == "9.9.9"
        assert constants.CLIENT_APP_VERSION == original_client
        assert constants.X_ZCODE_APP_VERSION == original_client
        constants.set_billing_version("3.16.3")  # 低版本不回退
        assert constants.BILLING_APP_VERSION == "9.9.9"
    finally:
        constants.BILLING_APP_VERSION = original_billing


def test_billing_version_and_activation():
    # billing 族必须跟官方桌面现行版（活动套餐按客户端版本门槛投放）；messages
    # 仍钉 3.16.3（3.17.x messages 形状实测 3012）。2026-11-03 实证：同账号同设备
    # 3.16.3 → preview 空 / claim 1004；3.17.1/3.17.2 → 出现 zcode-v3-start-plan-1103。
    assert constants.BILLING_APP_VERSION != constants.CLIENT_APP_VERSION
    assert constants.BILLING_APP_VERSION == "3.17.2"
    assert constants.CLIENT_APP_VERSION == "3.16.3"
    assert constants.BILLING_TITLE == "Z Code@electron"
    assert constants.BILLING_RELEASE_CHANNEL == "stable"
    assert constants.EVENT_REPORT_URL == "https://zcode.z.ai/api/v1/event/report"
    assert constants.ACTIVATION_ELEMENTS == ("app_launch", "app_daily_active")
    assert constants.ACTIVATION_SCREEN_RESOLUTION == "2560x1440"


def test_marketing_touch_and_release_manifest():
    """活动运营位 + 官方发行清单（两条 2026-10-30 新增的上游通道）。"""
    assert constants.MARKETING_TOUCH_URL == "https://zcode.z.ai/api/v1/marketing/touch"
    assert constants.MARKETING_CLAIM_ACTION == "claim_zcode_plan"
    assert constants.RELEASES_MANIFEST_URL == \
        "https://zcode.z.ai/api/v1/releases/electron/manifest"


def test_captcha_defaults_match_zcode2api():
    # 实测线上 captcha region=cn（非 zcode2api 的 sgp 兜底），以线上为准
    assert constants.CAPTCHA_DEFAULTS == {
        "enabled": True, "prefix": "no8xfe", "region": "cn", "sceneId": "11xygtvd",
    }


def test_model_map():
    assert constants.MODEL_NAME_MAP["glm-5.3-flash"] == "GLM-5.3-Flash"
    assert constants.MODEL_NAME_MAP["glm-5.2"] == "GLM-5.2"
    assert constants.MODEL_NAME_MAP["glm-turbo"] == "GLM-5-Turbo"
    assert constants.AVAILABLE_MODELS == ["GLM-5.3-Flash", "GLM-5.3"]


def test_model_fallback_constants():
    """模型兜底的默认顺序与「模型不可用」业务码（改这两项 = 改调度策略）。"""
    # 顺序即优先级：主力 → Flash（额度独立计）→ 更老的型号
    assert constants.MODEL_FALLBACK_CHAIN == [
        "GLM-5.3", "GLM-5.3-Flash", "GLM-5.2", "GLM-5-Turbo", "GLM-4.7",
    ]
    # bigmodel（智谱开放平台）模型名不同：默认不兜底，需要时显式给
    assert constants.MODEL_FALLBACK_CHAIN_BIGMODEL == []
    # 1005 额度用尽 / 3006 不在套餐内 = 这个账号的这个模型不可用（→ 换模型）
    assert constants.MODEL_UNAVAILABLE_CODES == (1005, 3006)
    # 3009 是模型并发租约（等一两秒就好），必须**不**在此列，否则会误换模型
    assert 3009 not in constants.MODEL_UNAVAILABLE_CODES
    assert 3012 not in constants.MODEL_UNAVAILABLE_CODES
    assert "exceed quota limit" in constants.MODEL_UNAVAILABLE_MARKERS
    assert "model not allowed" in constants.MODEL_UNAVAILABLE_MARKERS


def test_model_fallback_settings_defaults_from_constants():
    """settings 的兜底链默认值必须来自 constants（禁止模块里再硬编码模型名）。"""
    from app import settings

    assert settings.MODEL_FALLBACK_CHAIN == constants.MODEL_FALLBACK_CHAIN
    assert settings.MODEL_FALLBACK_CHAINS["zai"] == constants.MODEL_FALLBACK_CHAIN
    assert settings.MODEL_FALLBACK_CHAINS["bigmodel"] == constants.MODEL_FALLBACK_CHAIN_BIGMODEL
    assert settings.MODEL_FALLBACK_ENABLED is True
    assert settings.MODEL_FALLBACK_TTL > 0


def test_rejection_signals():
    assert constants.EXHAUST_HTTP_STATUSES == (402,)
    assert "余额不足" in constants.EXHAUST_KEYWORDS
    assert "insufficient" in constants.EXHAUST_KEYWORDS
    # 403 不再无条件判 invalid：需先排除验证码挑战（对齐 zapi classifyAccountFailure）
    assert '"code":3007' in constants.CAPTCHA_BODY_MARKERS


def test_identity_headers():
    # 版本值由 test_client_version_single_source 守护，这里只断言字面量口径
    assert constants.ANTHROPIC_VERSION == "2023-06-01"
    assert constants.X_ZCODE_AGENT == "glm"
    assert constants.HTTP_REFERER == "https://zcode.z.ai"
    assert constants.IDENTITY_TITLE == "Z Code@electron"
    assert constants.IDENTITY_RELEASE_CHANNEL == "stable"
    assert constants.IDENTITY_CLIENT_LANGUAGE == "zh-CN"
    assert constants.IDENTITY_CLIENT_TIMEZONE == "Asia/Shanghai"
    assert constants.IDENTITY_OS_CATEGORY == "macos"
    assert constants.CAPTCHA_REGION_HEADER == "X-Aliyun-Captcha-Verify-Region"


def test_settings_upstream_defaults_from_constants():
    from app import settings
    assert settings.UPSTREAM["zai"] == constants.MESSAGES_URLS["zai"]
    assert settings.UPSTREAM["zai_fallback"] == constants.MESSAGES_URLS["zai_fallback"]
    assert settings.UPSTREAM["bigmodel"] == constants.MESSAGES_URLS["bigmodel"]
    assert settings.ZCODE_BILLING_BASE == constants.BILLING_BASE


# 网关常量（底座原值，回归锁定）
MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5


def test_gateway_constants():
    from app.routes import gateway
    assert gateway.MAX_CAPTCHA_RETRIES == MAX_CAPTCHA_RETRIES
    assert gateway.MAX_ACCOUNT_ATTEMPTS == MAX_ACCOUNT_ATTEMPTS


def test_risk_control_signals():
    # 2026-09-05 3012 事件锁定：HTTP 405 承载 {"code":3012,"msg":"...unusual activity..."}
    assert constants.RISK_CONTROL_HTTP_STATUSES == (405,)
    assert '"code":3012' in constants.RISK_CONTROL_MARKERS
    assert "unusual activity" in constants.RISK_CONTROL_MARKERS


def test_retry_settings_defaults():
    from app import settings
    assert settings.RETRY_429_TIMES == 5
    assert settings.RETRY_429_WAIT == 60
    assert settings.RETRY_429_WAIT_MAX == 120
    assert settings.RETRY_5XX_TIMES == 3
    assert settings.RETRY_5XX_WAIT == 5
    assert settings.COOLING_SECONDS == 300
