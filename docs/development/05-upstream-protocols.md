# 05 — 上游协议参考

状态：定稿（三个来源项目实测 + 公开配置验证）。所有端点已在 zcode2api / zcode-switch / zcode-api 中观测到真实流量。

## 1. 端点拓扑

```
认证:    chat.z.ai/api/oauth/authorize        (浏览器授权)
         zcode.z.ai/api/v1/oauth/token        (授权码换 access_token)
         zcode.z.ai/api/v1/oauth/cli/init     (CLI 发起, server-mediated)
         zcode.z.ai/api/v1/oauth/cli/poll/{flow_id}
         chat.z.ai/api/oauth/userinfo         (Bearer access_token)
         api.z.ai/api/auth/z/login            (access_token → 业务 JWT)

AI:      zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages   (Plan 通道, Bearer JWT)
         api.z.ai/api/anthropic/v1/messages                   (API Key 通道, x-api-key)
         open.bigmodel.cn/api/anthropic/v1/messages           (BigModel, x-api-key)

计费:    zcode.z.ai/api/v1/zcode-plan/billing/current   ⚠️ WAF
         zcode.z.ai/api/v1/zcode-plan/billing/balance   ⚠️ WAF
         api.z.ai/api/biz/subscription/list             ✅
         api.z.ai/api/monitor/usage/quota/limit         ✅
         zcode.z.ai/api/v1/client/configs               ✅ 免鉴权

领取:    zcode.z.ai/api/v1/zcode-plan/billing/preview
         zcode.z.ai/api/v1/zcode-plan/billing/claim

风控:    zcode.z.ai/api/v1/agent/configs                (endpoint routing + 签名开关)
```

⚠️ WAF：实测会拦截非浏览器特征的 `billing/current|balance` 访问——**错峰轮询 + 完整身仿真头是硬要求**，`fetch_quota` 对 401 需先排除验证码挑战再判凭证失效。

## 2. 认证链路

### 2.1 zai OAuth（CLI server-mediated，headless 友好）

```
1. POST zcode.z.ai/api/v1/oauth/cli/init
   Headers: Authorization: Bearer <本地随机 poll_token>, Content-Type: application/json
   Body:    {"provider": "zai"}
   → data.{flow_id, authorize_url, poll_token?}（2026-09 新协议：服务端下发 poll_token，
     后续 poll 必须采用该值；未下发时回落本地随机值，兼容旧协议）
2. 浏览器打开 authorize_url（chat.z.ai/api/oauth/authorize?client_id=client_P8X5CMWmlaRO9gyO-KSqtg&...；
   新协议下 redirect 直接指向官方 cli callback，客户端原样使用不再改写）
3. GET zcode.z.ai/api/v1/oauth/cli/poll/{flow_id}   (Bearer poll_token，轮询至授权完成)
   → data.accessToken（含过期时间）+ zcodejwttoken（视返回结构）
   poll 4xx 承载 code=3004 → 会话过期（重新发起），其余 4xx → 终态 failed
4. POST api.z.ai/api/auth/z/login  {"token": "<access_token>"}  → 业务 JWT
5. GET  chat.z.ai/api/oauth/userinfo (Bearer access_token)      → user_id
6. 兑换 API Key：getCustomerInfo(默认机构/项目) → api_keys(name="zcode-api-key") → copy/{key} 取 secretKey
```

桌面客户端（对照）：redirect_uri 为 `zcode://zai-auth/callback`，本地 HTTP 服务器接 code 后走 `POST /api/v1/oauth/token`（`{provider:"zai", code, redirect_uri, state}`）。

### 2.2 bigmodel OAuth

```
authorize: https://bigmodel.cn/login?redirect={REDIRECT_ENC}&appId=zcode&state={p}
回调:      zcode://oauth/callback (桌面) / 本地回调端口（CLI 实现）
token 交换: zcode.z.ai/api/v1/oauth/token
业务侧:    bigmodel.cn 域（getCustomerInfo / api_keys），API 形态同 zai
```

## 3. 对话请求（Plan 通道，start-plan 需验证码）

```http
POST https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages
Authorization: Bearer {zcodejwttoken}
anthropic-version: 2023-06-01
Content-Type: application/json
User-Agent: ZCode/{app_version}
X-ZCode-App-Version: {app_version}
X-ZCode-Agent: glm
HTTP-Referer: https://zcode.z.ai/
X-Title: Z Code@{sourceTitle}
X-Device-Mid: {uuidv4，首次生成永久复用}
X-Aliyun-Captcha-Verify-Param: {verifyParam}        # start-plan 必需
X-Aliyun-Captcha-Region: {region}
```

Body：标准 Anthropic Messages（`model/max_tokens/stream/system/messages`）。上游模型名**大小写敏感**（`GLM-5.2`、`GLM-5-Turbo`）。

API Key 通道差异：`x-api-key: {apiKey}.{secret?}` 替代 Bearer，无验证码头。

**验证码 verifyParam**：Node+jsdom 运行阿里云官方无痕 SDK（`AliyunCaptcha.js`，o.alicdn.com），`startTracelessVerification()` 成功回调给出 `verifyParam = base64(JSON{certifyId, sceneId, isSign, securityToken})`；sceneId/region/prefix 从 `client/configs` 动态取（默认 `11xygtvd/sgp/no8xfe`）。挑战形态：403 + captcha 头；或 400 + body `{"code":3007}`。

**被拒信号**（池分类依据）：

| 上游响应 | 判定 | 动作 |
|----------|------|------|
| 402 / body 含 quota|insufficient|balance|exhaust|额度|余额不足 | 额度耗尽 | exhausted，30min 重试窗 |
| 429 | 限流 | cooling 300s |
| 401 / 403(非验证码) | 凭证失效 | invalid，直到重登 |
| 403 + captcha 挑战 / 400+`code:3007` | 验证码问题 | 刷新 verifyParam 原账号重试 |

## 4. 免费额度（Start Plan）

`GET zcode.z.ai/api/v1/client/configs?app_version={ver}`（免鉴权）→ `data.configs.startPlanPreview`：

| 模型 | 日额度 |
|------|--------|
| GLM-5.3（旗舰，原名 GLM-5.2，服务端可换 showName） | 3,000,000 tokens/日 |
| GLM-5-Turbo（≈Sonnet 级） | 2,000,000 tokens/日 |

独立计算、每日重置；额度由服务端按账号在调用时授予与扣减。`billing/balance` 返回 `data.balances[]`（`show_name/model/total_units/used_units/remaining_units/expires_at`）——即 PlanSlot 数据源。

⚠️ `expires_at` 已经过去的窗口必须**丢弃**：上游在本地日切（09:00）之后仍会把上一窗口列在
`balances[]` 里，`remaining_units` 还是旧数字（实测：日切后仍返回「2.48 亿 / 3.00 亿 · 至
10/26 09:00」）。这类窗口的额度已经不可用，若照单全收，后台会长期显示「已经消失的额度」，
额度耗尽判定也会被旧数字带偏。`quota._window_expired()` 统一按 `expires_at <= now` 过滤
（兼容 unix 秒 / 毫秒 / ISO 字符串），balance 正常返回时以本轮结果为准（过滤后为空也要清掉
上一轮残留快照）。

## 5. 领取（活动套餐）

```
GET  billing/preview   → data.previews[]: ClaimPlan{plan_id,name,description,priority,grants,grant_items[{name,units,period}]}
                          （活动未上线时 404 —— 属正常态，静默跳过）
                          查询参数: app_version 必带；platform 参数 preview 容忍、client/configs 拒绝（3001）
POST billing/claim     → 头: Bearer JWT + 验证码头 + X-Device-Mid + X-ZCode-App-Version + X-Platform
                          （实测缺版本/平台头即使验证码有效也 3007 —— asar claimManualPlan 头形态）; body: {plan_id}
                          成功 → data.server_time（服务端时钟，秒）+ data.plan.{starts_at,ends_at} 生效窗口（3.11.2 起）
                          失败码: 1003 already_claimed / 1005 quota_exhausted（data.plan.ends_at × 1000 = 名额恢复时间 next_at）
                          ⚠️ entitlements[].effective_at 可能晚于领取时刻：2026-10-26 实测 19:54 领取、
                             23:00 才计入余额（billing/preview 的 grant 已可见，billing/balance 未计）。
                             额度面板此时仍显示旧窗口，属正常态，生效后刷新即变。
                          ⚠️ 2026-11-02 复现：领取成功后 `billing/current` 只剩 `{"code":0}`（无 data），
                             唯一证据是 `billing/balance` 的 `data.plans[]`（user_plan：status/
                             starts_at/ends_at/entitlements[].effective_at）。只读 current 会让
                             「已领取、23:00 生效」显示成「无套餐数据」，耗尽判定也看不到赠送池。
                             `quota.fetch_quota` 已按 plan_id 合并两通道 plans（balance 字段优先）。
前置: identity.appVersion ≥ 活动要求的最低客户端版本（否则 ineligible）
```

版本口径（单一真相源 `app/constants.py`）：
`CLIENT_APP_VERSION = "3.16.3"`（messages 的**实证可用请求形状**）；
`BILLING_APP_VERSION = "3.17.2"`（billing 族跟官方桌面现行版，活动投放按最低客户端
版本门槛）。**两者必须分开，不能再合成同源**：

- 2026-10-26 第二次复现：billing 端钉 3.14.4 时 `billing/preview` 对所有账号恒空，
  同账号同一时刻改用 `app_version=3.16.3` 立刻返回 `zcode-v3-start-plan-nt-1026`。
- 2026-11-03 23:20 第三次复现（同账号、同设备）：billing 3.16.3 → preview 空、
  claim 返回 `1004 no permission`；改成 3.17.1/3.17.2 → preview 立刻出现
  `zcode-v3-start-plan-1103`（300M GLM-5.3-Flash），claim 成功。
  当时界面表现就是「领取失败：不符合领取条件」，实际是 billing 版本太低。
- `version_sync` 现在会**自动把 BILLING_APP_VERSION 跟到官方清单最新版**；
  messages 的 `CLIENT_APP_VERSION` 仍钉 3.16.3，只有显式 `APP_VERSION_FOLLOW=1`
  才跟进（默认关闭）。

⚠️ **不要盲目把 messages 跟到官方最新发行版**（2026-10-30 23:00 实证，同一账号、
同一配额窗口、同一 CLI 形态只换版本号）：

| app_version | messages 结果 |
|---|---|
| 3.16.3 | HTTP 200 正常出字 |
| 3.17.1 | HTTP 405 `{"code":3012,"msg":"unusual activity"}`（整池 5 个账号全中，全部进风控冷却） |

3.17.x 的官方 CLI 请求形状（UA 后缀 / 签名）变了，版本号不是唯一变量——跟着升只会把
整池打进风控。所以 messages 的版本策略是「形状优先，钉住可用的那个」；billing 族
（preview/claim/balance/current/usage/configs/event）按活动投放门槛跟官方现行版。

版本巡检（`app/version_sync.py`）：`GET /api/v1/releases/electron/manifest`
（免鉴权，electron-updater YAML，2026-11-02 实测 stable=3.17.2）是官方最新版权威来源
（安装包内 `app-update.yml` 的 provider 被改写过 → localhost，不可依赖）。启动读
`data/app_version.json` 里的上次巡检结果，之后每 6h 比对一次：**billing 版本自动跟到
官方最新**；messages 仍默认只体检、不跟进（`APP_VERSION_FOLLOW=1` 才跟进）。不一致时
warn 并在 `/admin/api/status` 的 `client_version` 暴露「messages 钉版 effective /
官方最新 latest / 是否跟进 follow」。
`ZCODE_APP_VERSION_FOLLOW=1` 才真跟进（`constants.set_client_version()` 一次性改写
UA / 版本头 / configs 查询串，只升不降）；开启前必须先用一个健康账号打一次 messages，
确认不是 3012（验证方法就是上面那张表）。
无账号路径的 `CLIENT_PLATFORM="darwin-arm64"`（asar `TH()` = `process.platform-process.arch`）。
有账号时 `X-Platform` / preview `platform` 跟该号 DeviceProfile 走（成套桌面 SKU，一号一台），禁止再盖成全局 darwin-arm64。
`USER_AGENT` / `X-ZCode-App-Version` / `client_configs_query()` 全部引用版本常量。

### 5.1 运营位投放（marketing/touch，2026-10-30 起的第二条通道）

限时套餐的投放通道**在 2026-10-30 换到了运营位**，`billing/preview` 对同一账号同一时刻
只返回 `{"code":0}`（连 `data` 字段都没有）——这正是那一天后台满屏
「领取失败：没有待领取的套餐」的根因（当日 5 个账号全部如此，而 10-28/10-29 的
`zcode-v3-start-plan-1028|1029` 都是 billing/preview 投的）。

```
GET  /api/v1/marketing/touch?seq=0
     头: X-Device-Mid(账号档案) + X-Client-Language + X-ZCode-App-Version + Authorization: Bearer JWT
     → data.deliveries[]: {campaign_id, resource_position:"banner", banner.buttons[].action}
       action = {"type":"claim_zcode_plan","args":{"plan_id":"zcode-v3-start-plan-1030-wk"}}
       套餐元数据在同层 args.zcode_plan（name/priority/starts_at/ends_at/entitlements[]）
POST /api/v1/zcode-plan/billing/claim  ← 领取端点不变（plan_id 取自运营位）
```

实测样本（2026-10-30 22:42）：`zcode-v3-start-plan-1030-wk` = ZCODE WEEKEND BUILD，
priority 101，GLM-5.3-Flash 500,000,000 token `one_time`，`effective_at` 晚于领取时刻
（约 20 分钟后计入），窗口 `ends_at` 1793581200。
实现：`app/marketing.py` 按「形状」抓运营位（action 类型 + plan_id），`claim.preview_plans`
返回 **billing/preview ∪ marketing/touch** 的并集（同名以 billing 为准），运营位失败不阻断
billing；两条通道都空时才把 billing 的错误（如凭证失效）如实抛到界面。
只在运营位里被 `claim_zcode_plan` 点名的套餐才算可领——纯展示位/商品卡不收。

## 6. 免费额度以外的两条通道（认知备查）

| 通道 | 计费 | 端点 | 认证 |
|------|------|------|------|
| API Key 通道 | 用户 BigModel/Z.ai API Key 按量 | `open.bigmodel.cn/api/anthropic` / `api.z.ai/api/anthropic` | `x-api-key` |
| Plan 通道（免费 Start + 付费 Coding） | ZCode 账号额度 | `zcode.z.ai/api/v1/zcode-plan/anthropic` | `Bearer JWT` |

## 7. 风控开关（Phase 4 备查，源自 zcode-api 观测）

- **endpoint routing**：`GET agent/configs` → `data.proxyEndpoint.mapping`（coding-plan Anthropic 端点被映射到 `zcode.z.ai/api/v1/ultra[-zai]/...`）；客户端定期拉取并重写上游 URL，fail-open。
- **client signing V4**：`agent/configs` → `data.codingPlanSignature.enable=true` 时，coding-plan 请求需先握手 `{provider}/api/paas/c1f3a7e2/v2/client`，随后每请求附 Ed25519 签名 + PoW 头；连续两次 401 VERIFY 后进入永久 unsigned 旁路（客户端行为）。start-plan / off-peak 永不免签。

## 8. 客户端身份头完整集（仿真必备）

```json
{
  "User-Agent": "ZCode/{appVersion}",
  "X-ZCode-App-Version": "{appVersion}",
  "X-ZCode-Agent": "glm",
  "X-Platform": "{platform}-{arch}",
  "X-Os-Category": "{osCategory}",
  "X-Release-Channel": "stable",
  "X-Client-Language": "{locale}",
  "X-Client-Timezone": "{timezone}",
  "X-Title": "Z Code@{sourceTitle}",
  "HTTP-Referer": "https://zcode.z.ai/",
  "X-Device-Mid": "{稳定 UUIDv4}"
}
```

`appVersion` 必须可打印 ASCII，非法值静默回退默认；`X-Device-Mid` 永不逐请求随机（防指纹抖动）。

## 9. messages 风控实况：CLI 形态定论（2026-10-26 实测）

上游 3.16.x 起，**桌面端（electron）仿真形态**调用 `zcode-plan/anthropic/v1/messages`
一律返回 `405 {"code":3012,"msg":"request has been blocked due to unusual activity."}`：
与账号无关（5 个账号全中，billing/额度查询正常）、与验证码无关（带真实 verifyParam 仍 3012）、
与客户端版本号无关（3.14.4 → 3.16.3 仍 3012）。

抓包对照后确认上游校验的是**官方 CLI 请求形状**（参照实现：`orangeTZ07/zcode-plan-bridge`）：

| 项 | 桌面端仿真（旧，3012） | 官方 CLI 形态（可用，默认） |
|---|---|---|
| 鉴权 | 仅 `Authorization: Bearer <jwt>` | `Authorization` + `x-api-key` **双带同一 JWT** |
| X-Title | `Z Code@electron` | `Z Code@cli` |
| X-Release-Channel | `stable` | `production` |
| User-Agent | `ZCode/{ver}` | `ZCode/{ver} ai-sdk/provider-utils/4.0.27 runtime/node.js/24` |
| X-Os-Category | darwin→macos 归一 | 原样上报 `win32` / `darwin` / `linux` |
| X-Device-Mid | 必带 | **不带** |
| x-query-id / x-session-id / x-zcode-query-source | 不带（旧结论：误发触发 3012） | **必带**（query-source = `main_turn`） |
| body.system | 官方三块 + 动态 currentModel 块 | **仅官方前两块原文**（第三条及以后一律拒绝，见 §9.1） |
| cache_control / metadata.user_id | 注入 | 不注入（原样透传） |
| 阿里云无痕验证码 | 必需 | **不需要** |

- system 前两块原文 = `app/zcode_system.json` 的前两项，与 orangeTZ07 抓包 marker 逐字节一致；
  改动任一字符即 3012（该仓库 README 亦记录此现象）。

### 9.1 2026-10-31：客户端升版后 system 块数成为「硬校验」（已修复）

ZCode 客户端升到 **3.17.1**（magpie 同步升到 0.1.2240）后，经网关的 messages 请求
**整池 3012**；同一时刻 `billing/balance` 正常、账号用极简请求直连上游 200
（排除额度/账号/IP/出口/我们的版本常量）。用「风控请求体落盘」（见下）拿到真实 body
后二分，结论很干净：

| 变体（其余字段完全不动） | 结果 |
|---|---|
| 原样转发（system = 5 块，134 tools，`output_config`/`metadata`/`tool_choice`，max_tokens 128000） | 405 / 3012 |
| 只删 `output_config` | 3012 |
| 删 `output_config`+`metadata`+`tool_choice` | 3012 |
| 再删掉 100 个 `mcp__*` 工具（剩 34 个） | 3012 |
| 只把 `max_tokens` 改成 32000 | 3012 |
| 换 3.17.1 身份头（同 body） | 3012 |
| **只把 system 砍成「官方前两块」** | **200**（134 tools / thinking / metadata 原样保留） |

也就是说：**上游只接受由「官方前两块」组成的 system**；客户端在官方块之后追加的
prompt（3.17.1 的 agent 指令、Sites/插件约定；Claude Code 的 system prompt 同理）
会让整条请求被判 unusual activity。旧版 3.16.3 客户端追加的块碰巧没被拦。
其他字段（工具数量、`output_config`、`thinking`、`max_tokens`）都不是判定点。

落地（`app/body_transform.apply_cli_system`）：

- `CLI_SYSTEM_STRICT=1`（默认）：system 收敛为官方两块；
- `CLI_SYSTEM_FOLD_TO_USER=1`（默认）：被剥掉的客户端块**折叠进首条 user 消息**，
  指令仍然进模型（只丢「system 身份」这个形状），不至于明显掉质量；
- 想彻底丢弃：`ZCODE_CLI_SYSTEM_FOLD_TO_USER=0`；上游规则若回滚：`ZCODE_CLI_SYSTEM_STRICT=0`。

配套诊断（都默认开，可关）：

- `ZCODE_BODY_TRACE=1`：命中 3012 时把**那一次的真实 body** 落 `data/debug/risk-body-*.json`
  （只留最近 5 份；日常请求不落盘）——这次就是靠它定位的；
- `ZCODE_RISK_STRIKE_GUARD=3` + `ZCODE_RISK_STRIKE_GUARD_WINDOW=1800`：strikes 到 3/4 时，
  最近一次命中后的 30 分钟内不再打上游（防客户端连着重试把整池推到「禁用 + 换设备指纹」）；
  窗口过后仍放一次试探，`成功即清零 strikes` 的自愈路径不被堵死。
- V4 客户端签名（`agent/configs` → `codingPlanSignature.enable=true`，握手
  `/api/paas/c1f3a7e2/v2/client` + Ed25519 + PoW）只约束 **coding-plan（API Key）** 通道；
  **start-plan（JWT）通道实测不需要签名**，不要为它引入握手（徒增失败面）。
- 切回旧形态：`ZCODE_MESSAGES_STYLE=desktop`（保留做回归/对照）。

## 10. 流式事件形态与「未响应」的根因（2026-10-27 实测）

### 10.1 上游真实事件序列（GLM-5.3-Flash，Plan 通道）

一次「需要思考」的回复，上游发的是**一个 thinking 块 + 一个 text 块**：

```
message_start                      usage: {input_tokens, cache_read_input_tokens}
ping                               ← 上游自己的保活帧
content_block_start  index=0  content_block.type="thinking"
content_block_delta  index=0  delta.type="thinking_delta"  delta.thinking="The"
content_block_delta  index=0  delta.type="thinking_delta"  delta.thinking="..."
...（实测一次回复 1996~2000 条，占全部 delta 的 99.8%）
content_block_delta  index=0  delta.type="signature_delta" delta.signature="1def3e..."
content_block_stop   index=0
content_block_start  index=1  content_block.type="text"
content_block_delta  index=1  delta.type="text_delta"     delta.text="天空..."
message_delta                  delta.stop_reason="end_turn"
                               usage: {output_tokens, cache_read_input_tokens}
message_stop
```

要点：

- 思考阶段的**全部内容都在 `thinking_delta` 里**，`text_delta` 只在思考结束后才来；
- `cache_read_input_tokens` 出现在 `message_start` 与 `message_delta` 的 usage 中，
  这就是客户端「命中」一栏的数据源；
- 上游只在开头发一次 `ping`，长思考期间**没有任何保活帧**。

### 10.2 「老是未响应」的根因（已修）

`openai_compat.StreamConverter` 原先只处理 `text_delta` / `input_json_delta` / `tool_use`，
`thinking_delta` 落到 `return []` 被整段丢弃。于是 `/v1/chat/completions` 的客户端在
模型思考期间**一个字节都收不到**（实测：客户端静默 39.33s，收到 0 个 reasoning 块），
界面只能显示「深度求索中 · 暂未响应」——思考越长，看起来越像卡死。

修复后的转换规则（`app/openai_compat.py`）：

| 上游事件 | OpenAI 兼容输出 |
|---|---|
| `content_block_start` type=thinking | 记录状态，不产出 |
| `content_block_delta` type=thinking_delta | `delta.reasoning_content` |
| `content_block_delta` type=signature_delta | 丢弃（思考块签名，客户端无用） |
| `content_block_delta` type=text_delta | `delta.content`（不变） |
| `message_start`/`message_delta` 的 `cache_read_input_tokens` | `usage.prompt_tokens_details.cached_tokens` |
| 非流式 `content[].type=="thinking"` | `message.reasoning_content` |

`/v1/messages` 是原样透传，本来就带 thinking 事件，不受影响。

### 10.3 保活与看门狗（对齐 magpie 的 keepalive / stall）

网关的 SSE 转发统一走 `app/routes/gateway.py::_paced`：

- **心跳**：客户端空闲满 `ZCODE_SSE_KEEPALIVE_IDLE`（默认 15s，与 magpie
  `keepaliveEvery` 同值）就补一条注释帧 `: keep-alive\n`。注释帧所有 SSE 客户端
  都会跳过，但它让客户端与中间代理知道连接还活着。**结尾不留空行**——有些客户端
  （openai-go v2）把空行当事件解析，会报 JSON 错。
- **看门狗**：上游彻底静默满 `ZCODE_UPSTREAM_STALL`（默认 600s）即判定连接已死并抛错。
  httpx 的 `read` 超时是 `None`（长回复不能设总时长上限），没有这层看门狗，半死的
  TCP 连接会把请求永久挂住。
- 上游读取放在独立 task 里当生产者、消费侧只等队列：空闲超时因此不会取消上游的
  读操作（取消读会破坏 httpx 的流状态）。

参考：magpie `internal/gateway/sse.go`（`comment`）、`internal/gateway/fallback.go`
（`keepHeldAfter = 15s`、`keepaliveEvery = 15s`、`holdThinking = 4min`）、
`internal/gateway/upstream_transport.go`（`upstreamHeaderWait = 10min`）。

### 10.4 会话粘性（affinity）：为什么不能每条消息都换号

上游按「账号 + prompt 前缀」缓存整段对话（`cache_read_input_tokens` 就是命中量）。
严格 round-robin 意味着**每条消息换一个账号**，上游每次都把整段对话重新读一遍：
命中归零、钱照付、首字节更慢，后台看起来还像「账号一直在跳」。

`app/affinity.py` + `store.select(prefer=...)`：

- 会话指纹 = `sha256(system + 第一条 user 文本)[:16]`——多轮增长不影响，正是缓存前缀起点；
- 成功拿到 200 后记住「这段对话由谁回答」，有效期 `ZCODE_AFFINITY_TTL`（默认 1800s）；
- 选号时粘性账号仍在可用池里就直接用它，且**不推进轮询指针**；
- 它不可用（冷却 / 额度用完 / 禁用 / 并发满被 skip）才照常轮询。

只放内存：上游前缀缓存本身也只有 5 分钟寿命，网关重启后重新学一遍的代价可以忽略。

参考：magpie `internal/gateway/affinity.go`（`cacheWorth`、`cacheCold = 5min`、`stickKeep = 24h`）。

## 11. 「HTTP 200 + 业务 code」的错误形态（2026-10-27 实测）

上游并不总是用 HTTP 状态码表达失败，以下两种是踩过的坑：

| 场景 | 上游响应 | 含义 | 正确处理 |
|---|---|---|---|
| 模型额度用尽 | `200` + `{"code":1005,"msg":"exceed quota limit"}` | 该账号对**这个模型**没额度了（同账号其他模型仍可用，实测 GLM-5.3 还有 300 万而 Flash 已用完） | 换号；**不要**把账号标成 `exhausted`（那会把整号踢出轮询，其他模型跟着不可用）；全池命中 → 换模型（§12） |
| 模型并发已满 | `429` + `{"code":3009,"msg":"model concurrency limit exceeded"}` | 上游按模型计的并发租约还没释放：**上一个请求刚结束的 1–2 秒内**再发，所有账号都会回 3009 | 短等重试整轮（`MODEL_BUSY_WAIT=1s` × `MODEL_BUSY_ROUNDS=3`）；**不要**按 429 频控原地等 60s（等待期间自己还占着并发槽，客户端几分钟收不到一个字节） |

实测数据（2026-10-27，4 个 JWT 账号、同一模型）：

```
第一发                       → 200 (3.6s)
+1s 再发                     → 503（全部账号 3009）
+2s 再发                     → 503（全部账号 3009）
+3s 再发                     → 200 (2.8s)      ← 租约约 2 秒释放
```

实现要点（`app/routes/gateway.py`）：

- 200 且 `content-type` 不是 `text/event-stream` 时，先 `aread()` 读进 `_Upstream.preloaded`
  再用 `_business_code()` 判 `code`：非 0 就归一成 400 走统一错误分类——不读这一次，
  这类失败会被当成成功原样透传给客户端，台账里也没有任何用量。
- 命中 1005/3009 时只换号并记 `reason`，不动账号状态（1005 在全池命中时会升级成换模型，
  见 §12）；整轮失败**全是** 3009 时
  `_dispatch` 会短等 `MODEL_BUSY_WAIT` 秒再来一轮（最多 `MODEL_BUSY_ROUNDS` 轮），
  这样「刚结束就再发」的请求能接上而不是直接 503；换号全失败时 503 的
  message 直接说明原因（如「GLM-5.3 并发已满（上游 code 429 · 换号后仍无可用）」）。
- 其它 200+code（如 3007 验证码）仍走原有分支：清池重解 / 换号 / 快速失败。

## 12. 模型级不可用信号（1005 / 3006 / 3009，2026-10-27 实测）

§11 讲的是「HTTP 200 + 业务 code」这种**承载形态**；本节把三种「模型级」信号摆在一起区分：
1005 / 3006 走业务码（200 或 4xx 的 body），3009 走 429。三者的作用层级和处置完全不同——
混判的后果是把好模型锁死，或者把客户端吊在 429 上原地等。

| 上游信号 | 承载 | 含义 | 层级 | 网关处置 |
|---|---|---|---|---|
| `code 1005` `exceed quota limit` | HTTP 200，body 里（实测） | 这个**账号**对这个**模型**的额度用尽 | 账号 × 模型 | 换号；整轮都如此 → 判该模型全池不可用，**换模型** |
| `code 3006` `model not allowed` | body 里的业务码（与 1005 同形） | 这个账号的套餐**不含**该模型 | 账号 × 模型 | 同上（换号 → 换模型），并快速标记跳过，不再反复撞墙 |
| `code 3009` `model concurrency limit exceeded` | HTTP 429，body 里（实测） | 该模型的**并发租约**还没释放：上一个请求刚结束的 1–2 秒内再发必现 | 模型（全局） | 短等重试整轮（`MODEL_BUSY_WAIT=1s` × `MODEL_BUSY_ROUNDS=3`）；**不换模型、不换号、不动账号状态** |

为什么 3009 绝不能当「模型不可用」：它不是额度问题，租约约 2 秒就释放（实测序列见 §11），
把它判死等于**永久**把流量赶去更弱的模型，而用户点的那个模型其实一直可用。同理也不能按
普通 429 频控原地等 60s——等待期间自己还占着并发槽，客户端几分钟收不到一个字节。

判定口径（`app/routes/gateway.py`）：

- `_is_model_unavailable(business_code, text)`：`business_code ∈ (1005, 3006)`，或文本命中
  `exceed quota limit` / `model not allowed` / `model_not_allowed`（`constants.MODEL_UNAVAILABLE_MARKERS`）；
- 3009 走**独立分支**（`business_code == 3009` 或文本含 `concurrency limit`），在模型不可用判定
  **之前**返回，两者互斥；
- 命中 1005 / 3006 时只换号并记 `reason`，**不把账号标 `EXHAUSTED`**——标了整号会被踢出轮询，
  同账号其他模型跟着不可用。

「该模型全池不可用」的**反应式**证据要同时满足三条（缺一不判死，否则「没问到」会被误当成
「不可用」）：

1. 这一轮里**试过的每个账号**都是 1005 / 3006；
2. 真的把可轮询的账号试完了（`store.select` 返回 `None`），而不是撞上 `MAX_ACCOUNT_ATTEMPTS=5` 收工；
3. 没有账号因「本账号并发已满」被跳过（那是没问到，不是不可用）。

**主动式**证据：账号额度快照（`billing/balance` → `account.quota`，见 §4）里该模型**所有**
启用账号的 `remaining ≤ 0` → 直接判死，连上游请求都不发。

判死后在 TTL（`model_fallback_ttl`，默认 600s）内跳过该模型；**一次真实成功即自愈**（清死标记
+ 记一条「实测可用」的信任票），所以额度窗口刚重置、刚领到新套餐、快照陈旧都不需要人工干预。
后台「立即恢复」（`POST /admin/api/models/revive`）提供人工清标记的入口。


