"""运行期配置：环境变量 + 默认值。

所有可调参数集中在此。账号与凭证不在此处，而是持久化到 data/ 目录（见 store.py）。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from . import constants

load_dotenv()

# 项目根目录
ROOT_DIR = Path(__file__).resolve().parents[1]


def _resolve_path(env_name: str, default: str) -> Path:
    raw = (os.getenv(env_name, default) or default).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _int(env_name: str, default: int) -> int:
    try:
        return int(os.getenv(env_name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(env_name: str, default: float) -> float:
    try:
        return float(os.getenv(env_name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(env_name: str, default: bool) -> bool:
    raw = (os.getenv(env_name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ── 目录 ─────────────────────────────────────────────────────────────────────
DATA_DIR = _resolve_path("ZCODE_DATA_DIR", "data")
# 账号与设置持久化到本地 SQLite（与 grok2api 的 local 后端一致）
DB_PATH = DATA_DIR / "accounts.db"
# 请求用量台账（每请求一行：token / 缓存命中 / 首字 / 生成速度 / 结果）
# 单独一个库：写入频率远高于账号库，且需要独立保留策略与聚合查询
USAGE_DB_PATH = _resolve_path("ZCODE_USAGE_DB", str(DATA_DIR / "usage.db"))
# 台账保留：先按天数裁，再按行数裁（0 = 该轴不限）
USAGE_RETENTION_DAYS = _int("ZCODE_USAGE_RETENTION_DAYS", 30)
USAGE_MAX_ROWS = _int("ZCODE_USAGE_MAX_ROWS", 50_000)
# 前端目录（前后端分离）：默认仓库根 frontend/，可用 ZCODE_FRONTEND_DIR 指向
# 独立部署目录（线上 /data/zcode-hub/frontend）；包内 statics 仅作兜底
FRONTEND_DIR = _resolve_path(
    "ZCODE_FRONTEND_DIR",
    "frontend" if (ROOT_DIR / "frontend").is_dir() else str(Path(__file__).resolve().parent / "statics"),
)
# 网关运行日志落盘保留天数（data/logs/gateway-YYYYMMDD.jsonl；0 = 永不清理）。
# 落盘是为了「进程死后还能查案」：自动关闭、上游异常、被谁打了请求，都在文件里。
LOG_RETENTION_DAYS = _int("ZCODE_LOG_RETENTION_DAYS", 14)

# ── 续派监督（Stop 钩子 v0.2 的文件协议）────────────────────────────────────
# 钩子（zcode-supervisor-continue 插件）每回合结束读写这里的开关与一次性指令。
# continue.enabled 总闸 / continue.loop 循环 opt-in / next-prompt.txt 一次性指令。
SUP_DIR = DATA_DIR / "_sup"

# ── 服务 ─────────────────────────────────────────────────────────────────────
PORT = _int("ZCODE_PORT", 3000)
HOST = os.getenv("ZCODE_HOST", "0.0.0.0")

# ── 公网隧道（cloudflared）───────────────────────────────────────────────────
# ZCODE_TUNNEL=1：随网关启动自动建一条临时隧道（start-public.bat 用的就是它）。
# 后台「设置 → 公网访问」里开过一次也会写进数据库，重启后照样自动开。
TUNNEL_AUTOSTART = _bool("ZCODE_TUNNEL", False)
# cloudflared 不在 PATH 时用这个指过去
TUNNEL_BIN = (os.getenv("ZCODE_CLOUDFLARED") or "").strip()

# ── 鉴权 ─────────────────────────────────────────────────────────────────────
# 后台管理密码默认值，首次启动写入 data/accounts.db，之后以数据库（meta 表）为准。
DEFAULT_ADMIN_KEY = os.getenv("ZCODE_ADMIN_KEY", "zcode")

# ── 验证码 ───────────────────────────────────────────────────────────────────
# 预解 token 池。真浏览器求解重（单枚 10–40s、Chromium 数百 MB），池收敛为 1/2
#（TTL 95s → 稳态约 95s 解一枚，Chromium 占空比 ~10–40%）；回滚 legacy 可调大。
CAPTCHA_POOL_MIN = _int("CAPTCHA_POOL_MIN", 1)        # 目标库存（低于则补）
CAPTCHA_POOL_MAX = _int("CAPTCHA_POOL_MAX", 2)        # 池上限
CAPTCHA_TOKEN_TTL = _int("CAPTCHA_TOKEN_TTL", 95_000) # 单枚 token 最大可用时长（ms；上游实际 ~2min）
CAPTCHA_CONFIG_CACHE_TTL = _int("CAPTCHA_CONFIG_CACHE_TTL", 600_000)  # ms

# 验证码求解（真浏览器：puppeteer-core + 系统 Chromium 跑阿里云官方无痕 SDK，
# 对齐 zcode-switch captcha.js。happy-dom 路线 2026-09 起被风控「unusual
# activity」全拒，solver.js 仅留作回滚：ZCODE_CAPTCHA_SOLVER=legacy）
NODE_PATH = os.getenv("ZCODE_NODE_PATH", "node")
CAPTCHA_SOLVER_DIR = ROOT_DIR / "captcha_node"
CAPTCHA_SOLVER_MODE = (os.getenv("ZCODE_CAPTCHA_SOLVER", "pw").strip().lower() or "pw")
CAPTCHA_SOLVER_JS = CAPTCHA_SOLVER_DIR / (
    "solver.js" if CAPTCHA_SOLVER_MODE == "legacy" else "solver_pw.js"
)
# Chromium 可执行文件（solver_pw.js 用）：env 优先，其次按平台常见安装路径探测。
# 探测的每一项就是"这台机器上最可能装着的那个"：Windows 上 Edge 是系统预装的
# Chromium，Linux/macOS 上常见的是发行版 chromium 与 Google Chrome。全都不存在
# 时返回首选项，让 solver 报出可读的错误（提示设 ZCODE_CHROMIUM_PATH）。
_CHROMIUM_CANDIDATES = (
    [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    if os.name == "nt"
    else [
        "/usr/local/bin/chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
)


def _find_chromium() -> str:
    """求解器用的浏览器路径：env 覆盖 > 平台常见路径里第一个真实存在的。"""
    override = os.getenv("ZCODE_CHROMIUM_PATH", "").strip()
    if override:
        return override
    for path in _CHROMIUM_CANDIDATES:
        if path and os.path.exists(path):
            return path
    return _CHROMIUM_CANDIDATES[0]


CHROMIUM_PATH = _find_chromium()
CAPTCHA_SOLVE_RETRIES = _int("ZCODE_CAPTCHA_RETRIES", 4)
# 每次求解超时（秒）：真浏览器含 launch（内存压力下可 30s+）+ SDK 加载 + 无痕验证，
# 且 solver 进程内自旋重试 3 次（约 40s×3），须容得下
CAPTCHA_SOLVE_TIMEOUT = _int("ZCODE_CAPTCHA_TIMEOUT", 240)

# ── 用量监控 ─────────────────────────────────────────────────────────────────
# 后台自动刷新账号额度的间隔（秒）。0 表示关闭后台轮询，仅按需刷新。
# 默认 1800（2.6.5 整改）：zcode-switch 参照系下额度查询是用户开界面才触发
# （人节奏，日均几十次）；60s 轮询 ≈ 每号每天 4300+ billing 请求，是风控
# 「unusual activity」的主信号源（vault「billing 连续查询易触发拦截」落地）。
QUOTA_REFRESH_INTERVAL = _int("ZCODE_QUOTA_REFRESH_INTERVAL", 1800)
# 成功对话后计费刷新的最小间隔（秒）：billing/* 连续查询易触发上游拦截，
# 每条消息都刷是流量放大器，与 monitor 轮询共享 last_checked_at 去抖。
BILLING_REFRESH_MIN_INTERVAL = _int("ZCODE_BILLING_REFRESH_MIN_INTERVAL", 60)
# ── 上游错误重试 / 冷却（参数可设定）─────────────────────────────────────────
# 429 频控：账号不冷却，原地等待后重试，耗尽后换下一个账号（账号保持可用）
RETRY_429_TIMES = _int("ZCODE_RETRY_429_TIMES", 5)       # 429 重试次数
RETRY_429_WAIT = _int("ZCODE_RETRY_429_WAIT", 60)        # 429 重试等待秒数（上游 Retry-After 优先）
RETRY_429_WAIT_MAX = _int("ZCODE_RETRY_429_WAIT_MAX", 120)  # Retry-After 采信上限（防吊死客户端）
# 5xx 等一般错误：重试，耗尽后账号冷却 COOLING_SECONDS 并换下一个账号
RETRY_5XX_TIMES = _int("ZCODE_RETRY_5XX_TIMES", 3)       # 5xx 重试次数
RETRY_5XX_WAIT = _int("ZCODE_RETRY_5XX_WAIT", 5)         # 5xx 重试等待秒数
# 限流（cooling）冷却时长（秒）——仅 5xx 重试耗尽 / 连接失败使用
COOLING_SECONDS = _int("ZCODE_COOLING_SECONDS", 300)
# 风控（3012/405「unusual activity」）指数退避冷却：实测为频道级瞬时频控
#（同号同刻 billing 正常、数小时自愈），冷却自动恢复；累计 RISK_BAN_STRIKES
# 次才升级为禁用（人工恢复）。冷却期零上游流量（is_cooling 全通道门禁）。
RISK_COOLDOWN_BASE = _int("ZCODE_RISK_COOLDOWN_BASE", 900)    # 首次冷却秒数
RISK_COOLDOWN_MAX = _int("ZCODE_RISK_COOLDOWN_MAX", 86400)    # 冷却上限（24h）
RISK_BAN_STRIKES = _int("ZCODE_RISK_BAN_STRIKES", 4)          # 窗口内累计命中达到即禁用
RISK_STRIKE_DECAY_SECONDS = _int("ZCODE_RISK_STRIKE_DECAY_SECONDS", 7 * 86400)
# 距上次风控命中超过该窗口则 strikes 重新起算：跨月偶发命中不累积成禁用
RISK_AUTO_ROTATE = _bool("ZCODE_RISK_AUTO_ROTATE", True)
# 风控「最后一格」保护：strikes 达到该值的账号，在最近一次风控命中后的
# RISK_STRIKE_GUARD_WINDOW 秒内不再被打上游（默认 3 = 离禁用 4 只剩一次）。
# 场景：上游规则变化后每条请求都 3012，客户端还会连着重试——不拦的话一次事故
# 就能把整池推到禁用（禁用会连带换设备指纹，恢复成本高）。窗口过后仍放一次
# 试探，保证「成功即清零 strikes」的自愈路径不被堵死。0 = 关闭保护。
RISK_STRIKE_GUARD = _int("ZCODE_RISK_STRIKE_GUARD", 3)
RISK_STRIKE_GUARD_WINDOW = _int("ZCODE_RISK_STRIKE_GUARD_WINDOW", 1800)
# 风控升级禁用时自动换发设备指纹（新 SKU + 新 device_mid）并后台补跑安装序
# ——「风控后换设备重生」语义自动化，账号重新启用时即全新身份
# 单账号并发上限（0 = 不限）。默认 1：上游对「账号 × 模型」的并发硬限很低——
# 实测同一账号同一模型一路还在跑时再发第二路，直接回
# 429 {"code":3009,"msg":"model concurrency limit exceeded"}。设 1 让网关自己
# 跳号（不惊动上游），并发能力因此等于账号数（4 个号 = 4 路）。
# 运行期可在后台设置改（meta 表即时生效）
ACCOUNT_CONCURRENCY = _int("ZCODE_ACCOUNT_CONCURRENCY", 1)
# 全池并发占满时排队等空位（秒）。账号并发 1 时「4 个号都在跑」是常态而不是
# 故障——这时直接 503 等于把客户端顶回去自己重试。排队后任一槽释放就立刻放行，
# 等满这个时长仍无空位才回 503（原因写明「排队超时」，监控里能和真正的故障区分）。
# 0 = 不排队，恢复「满即 503」的旧行为。
QUEUE_WAIT_MAX = _float("ZCODE_QUEUE_WAIT_MAX", 120.0)
# 排队人数上限：全池卡住时防止请求无限堆叠（每个等待者都占着一条客户端连接）。
# 超过就直接 503（原因「排队已满」），让客户端自己决定是否重试。
QUEUE_MAX_WAITERS = _int("ZCODE_QUEUE_MAX_WAITERS", 16)
# 全局粘号：所有会话的流量都尽量留在同一个账号上（默认开）。只有它不可用
# （额度用完 / 冷却 / 并发满 / 凭证失效）才轮询下一个——上游按「账号 × 模型」
# 计并发租约，摊开到多个号并不能提高并发上限，只会让后台看起来一直在跳号。
# 会话粘性（affinity）仍然优先：同一段对话先回它上次用的号。
ACCOUNT_STICKY = _bool("ZCODE_ACCOUNT_STICKY", True)
# 模型级并发（上游 429 + code 3009「model concurrency limit exceeded」）：实测
# **上一个请求刚结束的 1–2 秒内**再发，所有账号都会回 3009——那是上游按模型计的
# 并发租约还没释放，不是账号坏了。整轮只因为这个原因失败时，短等再来而不是直接
# 回 503（合计最多 ROUNDS × WAIT 秒，客户端完全等得起）
MODEL_BUSY_WAIT = _float("ZCODE_MODEL_BUSY_WAIT", 1.0)
MODEL_BUSY_ROUNDS = _int("ZCODE_MODEL_BUSY_ROUNDS", 3)
# ── 模型兜底（自动换模型）─────────────────────────────────────────────────────
# 请求模型在全池都不可用（每个账号都被上游判成 1005 额度用尽 / 3006 不在套餐）
# 时，按下面顺序自动换到下一个模型，客户端无感（响应头如实回报实际模型）。
# 运行期可在后台设置改（meta 表即时生效）。
MODEL_FALLBACK_ENABLED = _bool("ZCODE_MODEL_FALLBACK", True)
MODEL_FALLBACK_TTL = _int("ZCODE_MODEL_FALLBACK_TTL", 600)  # 判死后跳过该模型的秒数
MODEL_FALLBACK_CHAIN = [
    m.strip() for m in (
        os.getenv("ZCODE_MODEL_FALLBACK_CHAIN")
        or ",".join(constants.MODEL_FALLBACK_CHAIN)
    ).split(",") if m.strip()
]
MODEL_FALLBACK_CHAIN_BIGMODEL = [
    m.strip() for m in (os.getenv("ZCODE_MODEL_FALLBACK_CHAIN_BIGMODEL") or "").split(",")
    if m.strip()
]
MODEL_FALLBACK_CHAINS = {
    "zai": MODEL_FALLBACK_CHAIN,
    "bigmodel": MODEL_FALLBACK_CHAIN_BIGMODEL,
}
# ── 流式保活 / 会话粘性（对齐 magpie 的 keepHeldAfter 与 affinity）────────────
# SSE 心跳：客户端空闲超过该秒数就补一条注释帧 `: keep-alive`。模型思考阶段
# 上游可能长时间只发 thinking 事件，没有心跳客户端只会显示「未响应」。
# 15s 与 magpie 的 keepaliveEvery 同值
SSE_KEEPALIVE_IDLE = _int("ZCODE_SSE_KEEPALIVE_IDLE", 15)
# 上游彻底静默的上限（秒）：httpx 的 read 超时是 None（长回复不能设总时长上限），
# 没有这层看门狗，半死的 TCP 连接会把请求永久挂起。默认 600s 远大于实测思考时长
UPSTREAM_STALL_SECONDS = _int("ZCODE_UPSTREAM_STALL", 600)
# 会话粘性有效期（秒）：同一段对话在有效期内优先回到上次回答它的账号，
# 让上游的前缀缓存能继续命中（换号 = 整段 prompt 重新读一遍，命中归零）
AFFINITY_TTL = _int("ZCODE_AFFINITY_TTL", 1800)
# 套餐自动领取轮间隔（秒）：周期对全部可打 billing 的 JWT 账号轮一遍
# preview + 领取（有可领套餐才补激活上报，见 claim.auto_claim_all_plans）。
# 默认 3600（2.6.5 整改）：zcode-switch 参照系下 preview/领取只在用户点界面
# 时触发；10 分钟轮次 + 每轮 app_launch 上报是标记不消退的帮凶。0 = 关闭轮次，
# 仅入池/手动触发。运行期可在后台设置改（meta 表即时生效）
CLAIM_ROUND_INTERVAL = _int("ZCODE_CLAIM_ROUND_INTERVAL", 3600)

# ── 上游端点 ─────────────────────────────────────────────────────────────────
# 上游端点：默认值统一收口在 constants.py，环境变量仅作覆盖
UPSTREAM = {
    "zai": os.getenv("ZAI_UPSTREAM_URL", constants.MESSAGES_URLS["zai"]),
    "zai_fallback": os.getenv("ZAI_FALLBACK_URL", constants.MESSAGES_URLS["zai_fallback"]),
    "bigmodel": os.getenv("BIGMODEL_UPSTREAM_URL", constants.MESSAGES_URLS["bigmodel"]),
}

# messages 通道请求形态（2026-10-26 实证）：
#   "cli"（默认）—— 官方 ZCode CLI 形态头 + Bearer/x-api-key 双带，system 仅前置官方
#          前两块原文；**不需要**阿里云无痕验证码。上游 3.16.x 实测 200。
#   "desktop"  —— 旧行为（@electron 仿真头 + aliyun 验证码）；3.16.x 起 messages 恒 405/3012。
MESSAGES_STYLE = (os.getenv("ZCODE_MESSAGES_STYLE", "cli").strip().lower() or "cli")

# CLI 形态的 system 收敛（2026-10-31 二分实测）：
#   上游只接受「官方前两块」组成的 system；客户端自带的第 3+ 块（ZCode 3.17.1 的
#   agent 指令、Claude Code 的 system prompt）会让整条请求被判 405/3012
#   （5 块 → 3012；砍成官方两块 → 200，tools/thinking/metadata 全部保留无影响）。
# CLI_SYSTEM_STRICT=0 回到「官方块 + 客户端块」的旧行为（仅在上游规则回滚后使用）。
# CLI_SYSTEM_FOLD_TO_USER=1：被剥掉的客户端块折叠进首条 user 消息，
#   指令不丢（形状合规 + 语义接近），质量敏感场景可设 0 彻底丢弃。
CLI_SYSTEM_STRICT = _bool("ZCODE_CLI_SYSTEM_STRICT", True)
CLI_SYSTEM_FOLD_TO_USER = _bool("ZCODE_CLI_SYSTEM_FOLD_TO_USER", True)

# ZCode 计费 / 额度查询端点
ZCODE_BILLING_BASE = constants.BILLING_BASE
# 激活事件上报（测试时指向 Mock 上游）
ZCODE_EVENT_REPORT_URL = os.getenv("ZCODE_EVENT_REPORT_URL", constants.EVENT_REPORT_URL)
# 活动运营位（限时套餐的另一条投放通道，见 constants.MARKETING_TOUCH_*）
ZCODE_MARKETING_URL = os.getenv("ZCODE_MARKETING_URL", constants.MARKETING_TOUCH_URL)
# 官方发行清单（客户端版本巡检的权威来源）
ZCODE_RELEASES_URL = os.getenv("ZCODE_RELEASES_URL", constants.RELEASES_MANIFEST_URL)
# 客户端版本巡检间隔（秒）：默认每 6h 拉一次官方发行清单做比对（只体检）
VERSION_SYNC_INTERVAL = _int("ZCODE_VERSION_SYNC_INTERVAL", 6 * 3600)
# 客户端版本自动跟进（默认关）：官方 3.17.x 起 CLI 请求形状变了，盲目跟版本会被
# 上游 405/3012 打进风控（2026-10-30 实测整池 5 个号全中）。置 1 前请先按
# docs/development/05 §5 验证新版本的 messages 形状，否则保持钉 3.16.3。
APP_VERSION_FOLLOW = _bool("ZCODE_APP_VERSION_FOLLOW", False)
# 风控（3012）请求体落盘：只在**命中风控**时写（平常不落盘，不产生磁盘抖动），
# 用于「上游说形状不对」时拿到真实 body 做字段级对比——靠日志只能猜。默认开，
# 只保留最近几份；ZCODE_BODY_TRACE=0 可关。文件在 data/debug/（gitignore）。
BODY_TRACE = _bool("ZCODE_BODY_TRACE", True)
BODY_TRACE_KEEP = _int("ZCODE_BODY_TRACE_KEEP", 5)

# OAuth 与兑换链 origin（测试时指向 Mock 上游）
OAUTH_API_BASE = os.getenv("ZCODE_OAUTH_API_BASE", constants.ZCODE_ORIGIN + "/api/v1")
ZAI_EXCHANGE_ORIGIN = os.getenv("ZCODE_EXCHANGE_ORIGIN", constants.ZAI_API_ORIGIN)

USER_AGENT = os.getenv("UPSTREAM_USER_AGENT", constants.USER_AGENT)
APP_VERSION = "2.14.0"

_FRONTEND_VERSION_FILE = FRONTEND_DIR / "version"


def frontend_version() -> str:
    """前端版本号（frontend/version 文件，每次读取 → 前端独立发版即生效）。

    文件缺失/为空时回退 APP_VERSION，保证本地开发与旧部署不破。
    """
    try:
        v = _FRONTEND_VERSION_FILE.read_text("utf-8").strip()
        return v or APP_VERSION
    except OSError:
        return APP_VERSION



