# 04 — API 规范

状态：与 2.5.11 实现对齐。对外网关只有 Anthropic Messages、OpenAI Chat Completions、`/v1/models`；**没有** `/v1/responses`。

所有管理端点挂 `/admin/api/*`，需 `Authorization: Bearer <后台密码>`（连续失败达上限后 429 锁 5 分钟）；网关端点按「网关 Key」配置可选鉴权（`Authorization: Bearer` 或 `x-api-key`，未配置即放行——生产必须配置）。

## 1. 网关端点（对外）

### 1.1 `POST /v1/messages`（Anthropic Messages，Phase 1）

- 请求/响应：标准 Anthropic Messages API（含 `stream: true` 的 SSE 透传）。
- 上游转发目标由账号模式决定：JWT → `zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages`；API Key → `api.z.ai/api/anthropic/v1/messages`。
- 行为：池内 round-robin 选号（`store.select`）→ 单账号失败按分类换号（≤`MAX_ACCOUNT_ATTEMPTS=5`，满号跳过不计）→ 验证码挑战原账号重试（≤3）。429/5xx/验证码等待期间释放该账号并发槽，醒后重新占槽或换号。
- 模型兜底：请求模型永远先试；整轮里试过的每个账号都被上游判 1005 / 3006 时，按兜底链换到下一个模型（见 §1.4）。
- 模型名规范化：小写化后映射（`glm-5.2→GLM-5.2`、`glm-5-turbo→GLM-5-Turbo`、`glm-turbo→GLM-5-Turbo`、`glm-5.1→GLM-5.1`、`glm-4.7→GLM-4.7`）；未知名原样透传。

### 1.2 `GET /v1/models`

```json
{ "object": "list", "data": [ { "id": "GLM-5.3-Flash", "type": "model", "display_name": "GLM-5.3-Flash", "created_at": "…" }, ... ] }
```

模型清单来自 `constants.AVAILABLE_MODELS`（按账号实际余额窗口公布，当前 `GLM-5.3-Flash` / `GLM-5.3`）。

### 1.3 `POST /v1/chat/completions`（OpenAI 兼容）

- 入站 OpenAI Chat 格式 → 翻译为 Anthropic 上游 → 翻译回 OpenAI 响应；`stream:true` 逐块翻译。
- usage/tool_calls 映射规则以移植对照表为准（`openai_compat.py`）。
- 兜底头同 §1.4；换过模型时，返回体与 SSE chunk 里的 `model` 字段是**实际服务**的模型。

### 1.4 模型兜底相关头（两个网关端点通用）

额度按「账号 × 模型」计，请求的模型在全池都不可用时网关会按兜底链换到下一个模型继续服务
（设计见 `01-architecture.md` §4.1，判定口径见 `05-upstream-protocols.md` §12）。

| 头 | 方向 | 说明 |
|---|---|---|
| `x-zcode-model` | 响应 | **实际服务**的模型；没换模型时与请求模型一致 |
| `x-zcode-model-requested` | 响应 | 客户端原本点的模型；**只有发生过兜底才有这个头** |
| `x-zcode-no-fallback` | 请求 | `1` / `true` / `yes` / `on` → 本次请求关闭兜底，只要请求的那个模型（`x-zcode-model-fallback: off` 同义） |

- 流式（SSE）与非流式都带这两个响应头；它们不影响响应体格式。
- 开关、兜底链、判死 TTL 用 `PUT /admin/api/settings` 改（全局）；单请求级关闭只认上面这个请求头。
- `/v1/models` 的清单不受兜底影响：那是「客户端可以点的模型」，不是「此刻一定有额度」。

### 1.6 错误格式

```json
{ "error": { "type": "<机器可读类型>", "message": "<人读信息>" } }
```

| HTTP | type | 触发 |
|------|------|------|
| 400 | invalid_request / invalid_request_error | JSON 非法，或请求体不是对象 |
| 401/403 | （FastAPI HTTPException） | 网关 key 缺失/不符 |
| 500 | captcha_error | 验证码求解失败 |
| 500 | internal_error | 调度层未捕获异常（监控条目会收口） |
| 502 | upstream_error | 上游响应无法读取或格式异常 |
| 503 | no_available_account | 池空 / 全部不可用 / 并发已满 |

账号级上游错误在**故障转移耗尽后**回传时：保留上游 status 与 content-type，body 为上游错误原文（转 JSON 失败则 500 字符截断文本）。

## 2. 管理端点（对内，均挂 `/admin/api/*` 且需后台密钥）

### 账号池

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/accounts` | 全量列表（`public_view`：脱敏凭证 + 状态 + 配额/套餐 + 用量）+ 概览统计 |
| `POST /admin/api/accounts` | `{provider, tokens: [..], name?}` 批量入池（也接受多行字符串）；同凭证幂等；真新增触发安装序 + JWT 自动领取 |
| `PUT /admin/api/accounts/{id}` | 改备注 / 换凭证（按形态自动判 jwt/apiKey） |
| `POST /admin/api/accounts/{id}/enabled` | `{enabled}` 启用/禁用 |
| `DELETE /admin/api/accounts` | body `[ids]` 批量移除 |
| `POST /admin/api/accounts/refresh` | `{all?}` 或 `{ids}` 批量刷新额度（冷却/失效跳过 billing） |
| `POST /admin/api/accounts/{id}/refresh` | 立即刷新该账号额度 |
| `POST /admin/api/accounts/reorder` | `{ids: [...]}` 按拖拽后的顺序重排（**第一行 = 网关最先使用的账号**）。落库 `sort_order`，重启后仍生效；未列出的账号保持原相对顺序排末尾；ids 全不匹配 → 404，空 → 400 |
| `POST /admin/api/accounts/{id}/fingerprint/rotate` | 换发客户端指纹（下一套成套桌面 SKU + 新 device_mid） |
| `GET /admin/api/status` | provider 列表 + 网关 key 是否已配置 + 可选账号计数 + `client_version`（`effective` 生效版本 / `latest` 官方最新（version_sync 拉发行清单）/ `follow` 是否自动跟进） |

### 凭证与登录

| 方法/路径 | 说明 |
|-----------|------|
| `POST /admin/api/login/start` | `{label?}` → `{flow_id, authorize_url, expires_in:300}`（zai server-mediated；前端展示链接，`label` 作账号名入池） |
| `GET /admin/api/login/poll/{flow_id}` | 轮询：`{status}` ∈ `pending`/`ready`/`failed`/`expired`；`failed` 附 `message`；`ready` 附 `account`（JWT 已入池；API Key 兑换/额度刷新后台回填）。官方 poll HTTP 4xx → `failed` 并摘会话；5xx/网络抖动 → `pending` 并打日志。ready/失败后或未知/超时 flow_id 一律 `expired`（会话一次性，防重入兑换链） |

### 凭证导入 / 导出

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/export` | 明文 JSON（name/mode/secret；仅后台密钥保护，安装字段不出网） |
| `POST /admin/api/import` | export 同构 JSON；新账号触发安装序 + JWT 自动领取 |

### 额度与领取

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/claim/preview` | 立即拉取当前可领套餐（`?account_id` 单账号；先上报激活事件）。套餐 = `billing/preview` 投放 ∪ 运营位（`marketing/touch`）投放的并集，按 priority 降序；运营位查询失败不阻断 billing，两条都空时才把 billing 的错误如实回传（`error`） |
| `POST /admin/api/claim` | `{account_ids?, plan_id?}` 自动领取（服务端求解验证码；3007 单次即止、拒码出池）。无套餐时回「暂无可领取的套餐（当日已领取，或活动未开始）」 |
| `POST /admin/api/claim/manual` | `{account_id, captcha_verify_param, captcha_region?, plan_id?}` 浏览器滑块人工领取（`captcha_region` 缺省时按上游 captcha 配置补 `X-Aliyun-Captcha-Verify-Region`，缺该头更易 3007） |
| `GET /admin/api/claim/captcha-config` | 前端滑块 SDK 初始化参数（scene/region/prefix/enabled） |

### 监控与设置

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/monitoring` | 实时视图：内存环形日志（最新在前，KEEP=500，含进行中，重启清零）。每条含 `channel`（`public` 公网隧道 / `local` 内网直连） |
| `GET /admin/api/usage` | 持久台账：`range`（1h/6h/24h/today/7d/30d/all）× `outcome`（all/ok/fail/completed/client_aborted/upstream_aborted/failed）× `account` / `model` / `q` × `channel`（`all` 合计 / `public` / `local`）× `limit`(≤500) / `offset`。返回 `rows`（明细）+ `total` + `stats`（调用/成功率/token/缓存命中/首字/tok·s/`fallback` 换模型计数）+ `by_account` + `by_model` + `by_endpoint` + `by_channel`（**恒定忽略 `channel` 筛选**，恒给公网/内网两栏，前端「来源用量」把它加上合计）+ `series`（按小时分桶）+ `heatmap` |
| `POST /admin/api/monitoring/clear` | 清空实时环**与**持久台账（`data/usage.db`） |
| `GET /admin/api/settings` | 不回明文密钥。返回 `admin_key_set` / `admin_key_masked` / `admin_key_is_default`、`gateway_key_set` / `gateway_key_masked`、`quota_refresh_interval`、`account_concurrency`、`claim_round_interval`、`model_fallback_enabled`（bool）、`model_fallback_chain`（**归一化后**的逗号串：小写别名映射上游官方名、去重、去空，与 `GET /admin/api/models` 的 `chain` 数组永远一致。所以 PUT 之后再 GET 看到的可能不是写进去的原文，这是有意的——回填要与网关实际请求的模型名一致）、`model_fallback_ttl`（int 秒） |
| `PUT /admin/api/settings` | 改密钥、刷新间隔、并发上限、自动领取轮间隔、模型兜底三项（改后即生效，落 meta 表；未传字段不动）。数字字段口径：`quota_refresh_interval` / `account_concurrency` / `claim_round_interval` 用 `max(0, …)` 钳制，而 `model_fallback_ttl` **故意不钳制**——负数或非数字直接 `400`「模型兜底冷却必须是非负整数」（0 有明确语义 = 不跳过已判死模型，静默改写成 0 会让人以为冷却关了其实没生效）。`model_fallback_enabled` 收真布尔或 `"1"/"0"/"true"/"false"`（兼容 `yes`/`on`），统一落库成 `"1"`/`"0"`；`model_fallback_chain` 收字符串或字符串数组，只做 split → strip → 去空 → 逗号拼接，**大小写按原样落库**（写 `glm-5.3` 就存 `glm-5.3`，由 `modelpool.chain` 在读取时归一化），**空串合法**（= 不兜底）。前端回填的掩码（含 `…` 或 `••••`）忽略不覆盖；网关 key 传空字符串表示关闭校验 |
| `GET /admin/api/models` | 模型兜底实时台账（固定 `zai`，无 `provider` 参数）：`enabled`、`chain`（**归一化后的数组**：小写别名映射官方名、去重、去空；与 `/settings` 的 `model_fallback_chain` 是同一份链的两种形态，永远一致）、`ttl`，以及 `models[]`——每行就是 `modelpool.snapshot("zai")`：`model` / `state` ∈ `quota_ok`\|`unknown`\|`dead` / `reason`（仅 dead 有值，人读的判死原因）/ `dead_until`（epoch 秒 float，非 dead 为 `null`）/ `alive_accounts`（还有余额的账号数）/ `accounts_total`（启用账号数）/ `remaining`（该模型池内剩余合计，无额度快照为 `null`）/ `in_chain`（是否在兜底链里；不在链内但余额表出现过的模型也会列出） |
| `POST /admin/api/models/revive` | 人工「立即恢复」：`{"model": "GLM-5.3"}` 只清该模型的死标记；`{}` 或不带 body 清空整份台账（`modelpool.clear("zai")`）。一律返回 `{"ok": true}`，模型不在链上也是 no-op（幂等） |

### 探活

| 方法/路径 | 说明 |
|-----------|------|
| `GET /meta` | `{version}`（无鉴权，supervisor/部署脚本探活用） |

## 3. 幂等与并发约定

- 入池按 `{provider}:{credentialString}` 幂等（重复添加返回既有账号）。
- 领取操作同一账号同时只有一个在途（调度器互斥）。
- 请求调度单账号并发上限默认 1（`account_concurrency` 设置，0 = 不限；上游对「账号 × 模型」并发硬限很低，超了回 429 code 3009）；满号不排队，直接跳下一个可选账号，全部满/无号返回 503。
- `POST /admin/api/accounts` 与 OAuth 回调并发入池：以 SQLite 写锁 + upsert 保证一致。
- 网关转发无上游超时（与 ZCode 桌面行为一致）；客户端断开 → 取消上游流（`httpx` stream close）。

## 4. 客户端接入示例

```bash
# Anthropic 兼容（Claude Code 等）
export ANTHROPIC_BASE_URL=http://127.0.0.1:3000
export ANTHROPIC_AUTH_TOKEN=<gateway_key>
npx claude

# OpenAI 兼容
curl http://127.0.0.1:3000/v1/chat/completions \
  -H "Authorization: Bearer <gateway_key>" -H "Content-Type: application/json" \
  -d '{"model":"glm-5.3","messages":[{"role":"user","content":"hi"}],"stream":true}'
```
