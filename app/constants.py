"""上游常量收口 —— 所有与 zcode/zai/bigmodel 上游相关的 URL、模型名、判定关键字集中在此。

约定（docs/development/06-dev-guide.md）：
- 模块代码禁止再硬编码上游 URL / 模型名 / 关键字，一律 import 本模块。
- 改动任何常量必须先在 tests/unit/test_constants.py 补断言，防止无意漂移。
- 值均来自源码实证，出处标注见 docs/development/05-upstream-protocols.md。
"""

from __future__ import annotations

# ── 上游 origin ──────────────────────────────────────────────────────────────
# Plan 通道（JWT + 验证码）：zcode.z.ai 的 coding-plan 代理端点
ZCODE_ORIGIN = "https://zcode.z.ai"
# API Key 通道（回退 / bigmodel 同形）：api.z.ai 的 anthropic 兼容端点
ZAI_API_ORIGIN = "https://api.z.ai"
# bigmodel（智谱开放平台）anthropic 兼容端点
BIGMODEL_ORIGIN = "https://open.bigmodel.cn"

# ── messages 端点（settings.UPSTREAM 的默认值来源）────────────────────────────
MESSAGES_PATHS = {
    # zai + jwt（Plan 通道，需 X-Aliyun-Captcha-Verify-Param）
    "zai": "/api/v1/zcode-plan/anthropic/v1/messages",
    # zai + apiKey 回退通道（免验证码）
    "zai_fallback": "/api/anthropic/v1/messages",
    "bigmodel": "/api/anthropic/v1/messages",
}
MESSAGES_URLS = {
    "zai": ZCODE_ORIGIN + MESSAGES_PATHS["zai"],
    "zai_fallback": ZAI_API_ORIGIN + MESSAGES_PATHS["zai_fallback"],
    "bigmodel": BIGMODEL_ORIGIN + MESSAGES_PATHS["bigmodel"],
}

# ── 计费 / 额度端点（quota.py / 未来 claim.py）────────────────────────────────
BILLING_BASE = f"{ZCODE_ORIGIN}/api/v1/zcode-plan"
BILLING_CURRENT_PATH = "/billing/current"
BILLING_BALANCE_PATH = "/billing/balance"
USAGE_PATH = "/usage"
# WAF 风险点：billing/* 连续查询易触发拦截，轮询必须错峰（docs 05 §风险控制）

# ── OAuth ────────────────────────────────────────────────────────────────────
OAUTH_CLI_INIT_PATH = "/api/v1/oauth/cli/init"
OAUTH_CLI_POLL_PATH = "/api/v1/oauth/cli/poll"   # + /{flow_id}

# ── 客户端版本（单一真相源：钉 3.16.3 —— 实证可用的 CLI 请求形状）──────────────
# 客户端 claim 头实测缺版本/平台头 → 上游 3007；client/configs 带 platform 参数 → 3001
# 出处：官方发行清单 /api/v1/releases/electron/manifest（免鉴权，2026-10-30 实测
# stable=3.17.1）；但 **messages 通道只认 3.16.3 这套形状**：
# 2026-10-30 23:00 实测（同账号、同配额窗口、同 CLI 形态）——
#   app_version=3.16.3 → HTTP 200 正常出字
#   app_version=3.17.1 → HTTP 405 {"code":3012,"msg":"unusual activity"}（整池 5 号全中）
# 3.17.x 的官方 CLI 请求形状（UA 后缀 / 签名）变了，硬跟版本号只会把整池打进风控。
# 因此这里钉「实证可用的形状版本」，版本巡检/跟进交给 app/version_sync.py：
# 默认只体检上报（官方最新 vs 当前钉版），要自动跟进必须显式 ZCODE_APP_VERSION_FOLLOW=1，
# 且跟完先用一个健康账号打一次 messages 验证不是 3012。
# zcode-switch 在 Windows 上经 `zcode_app_version()` 读注册表本机真实版去打
# billing（官方升版即自动跟进）；hub 无注册表，只能靠上面这套「形状优先」口径。
# ⚠️ 上游按客户端版本门槛投放活动套餐：钉旧版（3.11.2）时 preview 恒空；
# 2026-10-26 复现第二次——钉 3.14.4 时 preview 恒空（code:0 / plans:[]）。
# 发现活动套餐不可见时先核对 version_sync 报出的「官方最新 vs 当前钉版」。
CLIENT_APP_VERSION = "3.16.3"
CLIENT_PLATFORM = "darwin-arm64"  # asar TH() = process.platform-arch，服务端固定伪装
CLIENT_CONFIGS_URL = f"{ZCODE_ORIGIN}/api/v1/client/configs"


def client_configs_query() -> str:
    """client/configs 查询串（版本随时可被 version_sync 改写，禁止缓存成常量）。"""
    return f"app_version={CLIENT_APP_VERSION}"


def _version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.strip().split("."))
    except (AttributeError, ValueError):
        return (0,)


def set_client_version(version: str) -> None:
    """运行期把 messages 客户端版本改到官方最新发行版（version_sync 显式跟进）。

    版本在本模块内多处派生（UA/验证码头/configs 查询串），只允许经此函数改写，
    保证「官方升版改一处」的口径在运行期同样成立。billing 族用独立的
    BILLING_APP_VERSION（默认官方桌面现行版），见 set_billing_version。
    """
    global CLIENT_APP_VERSION, BILLING_APP_VERSION, USER_AGENT, X_ZCODE_APP_VERSION
    v = (version or "").strip()
    if not v:
        return
    CLIENT_APP_VERSION = v
    USER_AGENT = f"ZCode/{v}"
    X_ZCODE_APP_VERSION = v
    if _version_key(v) > _version_key(BILLING_APP_VERSION):
        BILLING_APP_VERSION = v


def set_billing_version(version: str) -> None:
    """只改 billing 族版本（活动套餐按客户端版本门槛投放），只升不降。

    messages 的钉版（CLIENT_APP_VERSION）保持不动：3.17.x 的 messages 形状会
    3012；billing 必须跟官方桌面现行版，否则 preview 恒空、claim 1004。
    """
    global BILLING_APP_VERSION
    v = (version or "").strip()
    if v and _version_key(v) > _version_key(BILLING_APP_VERSION):
        BILLING_APP_VERSION = v


# ── 官方发行清单（客户端版本巡检的权威来源）──────────────────────────────────
# GET /api/v1/releases/electron/manifest?platform=windows-x86_64&device_mid=<uuid>
# 免鉴权，返回 YAML（version: 3.17.1 / files[] / releaseNotes）。
# 安装包里的 app-update.yml 被改写过（provider: generic → localhost），不可依赖。
RELEASES_MANIFEST_PATH = "/api/v1/releases/electron/manifest"
RELEASES_MANIFEST_URL = f"{ZCODE_ORIGIN}{RELEASES_MANIFEST_PATH}"

# ── 活动运营位（marketing/touch）────────────────────────────────────────────
# 2026-10-30 实证：限时套餐改由「运营位投放」下发——GET marketing/touch 的
# deliveries[].banner.buttons[].action = {type:"claim_zcode_plan", args:{plan_id}}，
# 套餐元数据（entitlements/窗口）在同层 args.zcode_plan；而 billing/preview 对
# 同一账号同一时刻返回 `{"code":0}`（连 data 都没有）→ 后台表现为
# 「领取失败：没有待领取的套餐」。领取端点仍是 billing/claim（plan_id 取自运营位）。
MARKETING_TOUCH_PATH = "/api/v1/marketing/touch"
MARKETING_TOUCH_URL = f"{ZCODE_ORIGIN}{MARKETING_TOUCH_PATH}"
MARKETING_CLAIM_ACTION = "claim_zcode_plan"

# ── billing 族版本 / 激活上报（zcode-switch v1.5.4 实证，2026-09-06 移植）─────
# billing 族（preview/claim/balance/current/usage/configs/event）必须跟**官方桌面端
# 现行版**：活动套餐按客户端版本门槛投放。2026-11-03 23:20 实证（同账号、同设备）：
#   billing 3.16.3 → preview 空、claim 返回 1004 no permission
#   billing 3.17.1 / 3.17.2 → preview 立刻出现 zcode-v3-start-plan-1103
# messages 仍钉 3.16.3（3.17.x 的 messages 形状会 3012），两个版本必须分开，
# 不能再引用同一个常量。version_sync 会把 billing 版本跟到官方清单最新版。
BILLING_APP_VERSION = "3.17.2"
BILLING_TITLE = "Z Code@electron"        # zcode-switch billing 头实证形态
BILLING_RELEASE_CHANNEL = "stable"
# 官方客户端每日活跃事件：POST /api/v1/event/report（不在 zcode-plan 下、无
# Authorization）。zcode-switch claim_refresh 在 preview 前上报 app_launch +
# app_daily_active —— 疑似活动套餐投放的资格信号（官方客户端可见活动而纯
# billing 轮询号 preview 为空的差异点）。
EVENT_REPORT_URL = f"{ZCODE_ORIGIN}/api/v1/event/report"
ACTIVATION_ELEMENTS = ("app_launch", "app_daily_active")
ACTIVATION_SCREEN_RESOLUTION = "2560x1440"

# ── 验证码默认配置（client/configs 拉取失败时的兜底；region 实测线上为 cn）────
CAPTCHA_DEFAULTS = {"enabled": True, "prefix": "no8xfe", "region": "cn", "sceneId": "11xygtvd"}

# ── 模型名 ───────────────────────────────────────────────────────────────────
# Z.AI 上游模型名大小写敏感；客户端传小写别名时映射到官方名（gateway.MODEL_NAME_MAP）
MODEL_NAME_MAP = {
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.3": "GLM-5.3",
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
}
# /v1/models 对外公布（2026-09-05 实测：当前账号套餐不含 GLM-5.2/5-Turbo，
# 上游 3006 model not allowed；按账号实际余额窗口公布）
AVAILABLE_MODELS = ["GLM-5.3-Flash", "GLM-5.3"]

# ── 模型兜底（「这个模型全池都没了，就换下一个模型」）─────────────────────────
# 默认顺序：先用最强的主力，再退到 Flash（额度独立计），最后是更老的型号
# （可能不在当前套餐里 → 上游 3006，会被快速标记跳过，不会反复撞墙）。
MODEL_FALLBACK_CHAIN = ["GLM-5.3", "GLM-5.3-Flash", "GLM-5.2", "GLM-5-Turbo", "GLM-4.7"]
# bigmodel（智谱开放平台）模型名不同，默认不兜底，需要时用设置/环境变量显式给
MODEL_FALLBACK_CHAIN_BIGMODEL: list[str] = []
# 「这个模型不可用」的上游业务码：1005 额度用尽 / 3006 模型不在套餐内。
# 注意 3009（model concurrency limit exceeded）不在此列——那是并发租约，
# 等一两秒就恢复，换模型是错的。
MODEL_UNAVAILABLE_CODES = (1005, 3006)
MODEL_UNAVAILABLE_MARKERS = ("exceed quota limit", "model not allowed", "model_not_allowed")

# 上游 max_tokens 合法范围（2026-09-06 实测：超限报 400 code 1210
# 「max_tokens参数非法：限制数值范围[1,131072]」，客户端（如 auto-compact 续传）
# 可能带更大的值，网关统一钳制）
MAX_TOKENS_LIMIT = 131072

# ── 请求头 ───────────────────────────────────────────────────────────────────
ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = f"ZCode/{CLIENT_APP_VERSION}"
X_ZCODE_APP_VERSION = CLIENT_APP_VERSION
X_ZCODE_AGENT = "glm"
# 无尾斜杠：与 zcode-switch（billing 实证形态）及本服务 billing 头统一，
# 消除 messages/billing 通道间 Referer 形状抖动
HTTP_REFERER = "https://zcode.z.ai"
CAPTCHA_HEADER = "X-Aliyun-Captcha-Verify-Param"
# region 头（zapi captcha.ts REGION_HEADER；与 PARAM 成对下发，缺失易 3007）
CAPTCHA_REGION_HEADER = "X-Aliyun-Captcha-Verify-Region"

# ── 身份头仿真（对齐 zapi identity.ts 的 pio；顺序、取值逐字段镜像官方客户端）──
# 官方客户端 companion 头集合，让代理在指纹层与官方 ZCode 桌面端不可区分。
# darwin-arm64 桌面身份。X-Device-Mid 走 quota.device_mid() 持久化复用。
IDENTITY_TITLE = "Z Code@electron"      # X-Title = "Z Code@{sourceTitle}"；桌面端账号走 electron
IDENTITY_RELEASE_CHANNEL = "stable"
IDENTITY_CLIENT_LANGUAGE = "zh-CN"
IDENTITY_CLIENT_TIMEZONE = "Asia/Shanghai"
# X-Os-Category：darwin→macos / win32→windows / 其它→linux
IDENTITY_OS_CATEGORY = "macos"
# X-Os-Version：os.release() 语义；darwin 25.x 对应 macOS 15。固定伪装值。
IDENTITY_OS_VERSION = "25.5.0"

# ── CLI 形态身份头（2026-10-26 实证：zcode.z.ai 只认官方 CLI 请求形状）─────────
# 背景：桌面端（electron）仿真形态自 3.16.x 起对 messages 一律 405/3012；抓包对照
# 后确认上游校验的是「CLI 形态」：X-Title=Z Code@cli、release-channel=production、
# UA 带 ai-sdk 后缀、Bearer 与 x-api-key 双带、并带 x-query-id/x-session-id/
# x-zcode-query-source；同时**不发送** X-Device-Mid。参照实现：orangeTZ07/zcode-plan-bridge。
CLI_TITLE = "Z Code@cli"
CLI_RELEASE_CHANNEL = "production"
CLI_QUERY_SOURCE = "main_turn"
# 官方 CLI UA 后缀（ai-sdk provider 指纹 + 运行时 node 主版本）
CLI_UA_SUFFIX = "ai-sdk/provider-utils/4.0.27 runtime/node.js/24"

# ── 上游被拒信号 → 账号动作（gateway / quota 判定共用）────────────────────────
EXHAUST_HTTP_STATUSES = (402,)
EXHAUST_KEYWORDS = ("quota", "insufficient", "balance", "exhaust", "额度", "余额不足")
# 验证码挑战：HTTP 403 + 文案，或 HTTP 400 + body {"code":3007}（docs 05 §被拒信号表）
# 注意：403 不再无条件判 invalid —— 需先排除验证码挑战（captcha/verify 文案或
# challenge 头），否则一次人机校验续期就把账号错杀成 INVALID（对齐 zapi
# classifyAccountFailure：403 + captcha 文案 → 非账号失败）。
CAPTCHA_BODY_MARKERS = ('"code":3007', '"code": 3007')

# ── 风控信号（2026-09-05 3012 事件实证）────────────────────────────────────────
# 3012「unusual activity」= 上游风控（HTTP 405 承载），不在官方公开错误码表；
# 高频请求触发，官方政策定性为临时限制（限流/冻结，3 次以上违规才封号）。
# 处置：账号指数退避冷却 + 暂停验证码池预热（池预热本身即上游流量，会加剧风控）。
RISK_CONTROL_HTTP_STATUSES = (405,)
RISK_CONTROL_MARKERS = (
    '"code":3012', '"code": 3012',
    "unusual activity",
)
