# zcode-hub

> **项目出处**：本项目基于 [liu5269/zcode2api](https://github.com/liu5269/zcode2api)（AGPL-3.0）深度二次开发而来，
> 另借鉴了 [pjpv/zcode-switch](https://github.com/pjpv/zcode-switch) 与 [TriDefender/zcode-api](https://github.com/TriDefender/zcode-api)
> 的部分设计。本仓库为**重开历史的独立新仓**（非 fork），复刻期的历史存档见
> [3366434156/zcode2api](https://github.com/3366434156/zcode2api)。依据 AGPL-3.0 许可证义务，
> 本仓继续以 **AGPL-3.0** 发布，原项目版权与许可声明见 [LICENSE](LICENSE) 与文末「许可证」章节。

**ZCode 账号运营 + 双协议网关一体机**：把池内 ZCode Coding Plan / Start Plan / API Key 账号额度，统一转换为标准的
**Anthropic Messages API**（`/v1/messages`）与 **OpenAI Chat Completions API**（`/v1/chat/completions`），向 Claude Code、Cline、Codex CLI、Chatbox、NextChat 等各类 Agent 与客户端提供高可用 API 服务。

自带一套完整的账号运营控制台：多账号池轮询、高可信成套桌面指纹仿真、官方首启安装序仿真、单账号并发控制、429/5xx/3012 阶梯容灾、请求全景监控、限时套餐自动与手动领取、OAuth 免密登录入池，以及免浏览器的阿里云无痕验证求解——开箱即用，自托管部署。

---

## 核心能力

- **双协议兼容网关**：原生支持 `POST /v1/messages`（Anthropic Messages 协议，支持流式 SSE）与 `POST /v1/chat/completions`（OpenAI 格式双向翻译，支持流式与非流式），并提供 `GET /v1/models` 模型列表。
- **高可信桌面指纹池（一号一台）**：入池自动生成加权桌面成套 SKU（Mac Sequoia/Tahoe、Win11 主流分辨率等），每账号独立全新 UUIDv4 `device_mid`，杜绝宿主机云 Linux 内核共享导致的账号集群关联风险。
- **官方首启安装序仿真**：入池与指纹轮换自动执行 `client/configs` 与激活事件上报（`app_launch` / `app_daily_active`），持久化 `install_id` 与 `installed_at`。
- **反代与 CDN 基础设施头深度清洗**：自动剔除 `x-forwarded-*`、`forwarded`、`cf-*`、`cdn-loop`、`x-real-ip`、`via`、`cookie`、`referer`、`origin`、`accept-language` 等网络中间件头部，杜绝部署网络拓扑泄露与客户端伪装冲突。
- **单账号精细化并发控制**：支持单账号并发上限（默认 1 = 一账号一路，可在后台运行时热改，0 不限）。满并发账号智能跳过，不计入重试轮数；客户端断开、异常与等待期间即时释放并发槽位。
- **模型兜底（自动换模型）**：额度按「账号 × 模型」计，请求的模型在全池都被上游判成额度用尽（1005）或不在套餐内（3006）时，按兜底链（默认 `GLM-5.3 → GLM-5.3-Flash → GLM-5.2 → GLM-5-Turbo → GLM-4.7`）自动换到下一个模型继续服务，客户端无感；响应头 `x-zcode-model` 如实回报实际服务的模型。
- **阶梯式故障转移与容灾策略**：
  - **429 智能退避重试**：优先根据上游 `Retry-After` 原地等待重试（等待期间释放并发槽位，防止阻塞）；重试耗尽无损换号且**不冷却账号**；Plan 通道 429 耗尽且同账号有 API Key 时自动切 `api.z.ai` 回退通道。
  - **5xx 熔断冷却**：重试 3 次后进入 300s 冷却，自动切下一个可用账号。
  - **3012/405 真风控隔离**：识别 "unusual activity" 立即标记 `DISABLED` 禁用账号保护资产，停止自动化空转，支持后台人工确认后一键启用恢复。
  - **401/403 鉴权失效标记**：非验证码的凭证失效直接标 `INVALID`。
- **全景请求监控与安全防护**：
  - 请求监控面板（`/admin/monitoring`）：实时展示在飞请求、历史请求耗时、状态码、模型、账号及错误堆栈。
  - 后台暴力破解防护：客户端 IP 连续登录失败达上限自动锁定 5 分钟。
  - 设置密钥脱敏回显：GET 设置接口密钥自动打码，保障安全性。
- **限时套餐领取（双通道 + 双轨机制）**：
  - 双通道发现：可领套餐 = `billing/preview` 投放 ∪ **运营位（`marketing/touch`）** 投放。2026-10-30 起上游把限时套餐搬到运营位下发，只看 `billing/preview` 的后台会满屏「没有待领取的套餐」；现在两条通道取并集，领取端点仍是 `billing/claim`。
  - 客户端版本巡检：每 6h 拉官方发行清单（`/api/v1/releases/electron/manifest`）比对「钉版 vs 官方最新」并在 `/admin/api/status` 暴露（活动按版本门槛投放，钉旧版会让整池看不到活动）。**默认只体检不跟进**——官方 3.17.x 起 CLI 请求形状变了，盲目跟版本会被上游 405/3012 打进风控（2026-10-30 实测整池 5 个号全中），要跟进须自行验证后置 `ZCODE_APP_VERSION_FOLLOW=1`。
  - 自动领取：入池即自动领取全部可领套餐，并有可配置的周期自动领取轮（默认 3600 秒，后台设置页可改，0 = 关闭），持续盯当期活动赠送。
  - 领取语义对齐官方 3.11.2：成功回执携带上游 `server_time` 与套餐窗口；1005「名额用完」附带名额恢复时间 `next_at`，领取轮按其退避（等待期不重试领取、不耗验证码，`next_at` 过后自动恢复）。
  - 浏览器过码：支持在前端调起阿里云滑块验证码进行手动领取，应对极严风控环境（region 头缺省时按上游 captcha 配置补齐，避免 3007）。
- **OAuth CLI 免密登录**：
  - 提供海外邮箱与 Google 登录直达引导卡片，解决上游 `chat.z.ai/auth` 隐藏第三方登录按钮问题。
  - 严格遵循官方 CLI 的干净请求头规范，JWT 授权入池秒级完成，API Key 兑换异步后台回填。
  - 跟进官方新协议：采用 init 响应服务端下发的 `poll_token` 轮询（未下发时回落自造，兼容旧协议）；poll 4xx 承载的 `code=3004` 明确映射为会话过期。

## 支持的账号类型

| Provider | 模式 | 说明 |
|----------|------|------|
| `zai` | `jwt` | Coding Plan 额度（Plan 通道，需无痕验证码），自动续领活动套餐 |
| `zai` | `apiKey` | Z.AI API Key（回退通道，免验证码） |
| `bigmodel` | `apiKey` | 智谱开放平台（Anthropic 兼容端点） |

> 一个 Z.AI 账号可同时持有 JWT 与 API Key：OAuth 登录后会一并入池，JWT 额度告罄或 429 耗尽时自动无缝走 API Key 回退通道。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # 按需修改密钥、端口等
.venv/bin/python cli.py serve   # 启动网关 + 后台 UI（默认 http://0.0.0.0:3000）
```

- 后台管理：`http://127.0.0.1:3000/admin/login`（初始账号见 `.env` 的 `ZCODE_ADMIN_KEY`）
- 对话端点：
  - Anthropic 协议：`POST http://127.0.0.1:3000/v1/messages`（兼容 Claude Code、Cline）
  - OpenAI 协议：`POST http://127.0.0.1:3000/v1/chat/completions`（兼容 OpenAI 客户端）
  - 模型列表：`GET http://127.0.0.1:3000/v1/models`
  - 探活端点：`GET http://127.0.0.1:3000/meta`

> 使用 Z.AI **JWT 模式**需要 Node.js 求解验证码，首次先执行：`cd captcha_node && npm install`。

## 让它一直跑着（守护进程 / 自动重启）

网关「老是自动关闭」的实测结论：**不是崩溃**——Windows 事件日志里没有 python 崩溃记录、
日志里也没有 traceback，而是被外部终止（最常见就是启动器的控制台窗口被顺手关掉）。
所以启动器现在默认跑在守护进程下：

```
start-zcode-proxy.bat          # 前台（有窗口），内部由 scripts/supervisor.py 守护
install-autostart.bat          # 后台常驻：注册登录自启，隐藏窗口、脱离终端、重启不丢
install-autostart.bat /remove  # 撤销自启
```

- 子进程退出后**自动重启**（退避 2s → 60s，连续失败 50 次停手，避免 crash loop 空转）；
- 每次退出都记进 `data/logs/supervisor.log`：`网关异常退出 code=... 存活 14s` ——
  下次再关就能判断是被强杀还是自己退的；
- 想彻底免疫「关窗口」就执行一次 `install-autostart.bat`。

## 网关日志（`/admin/logs`）

日志同时进三处，职责不同：控制台（窗口）、内存环（2000 条，页面分页用）、
**落盘 `data/logs/gateway-YYYYMMDD.jsonl`（保留 14 天）**。落盘的意义是
**进程死了之后仍然能查案**——自动关闭、上游异常、被谁打了什么请求，全在文件里。

- 页面是终端视窗风格：级别（INFO / WARN / ERROR）、模块标签（按实际出现过的模块动态生成）、
  关键字筛选，5 秒增量轮询、自动滚屏、复制、导出 `.jsonl`、清空；
- **uvicorn 的 access / error 日志也接了进来**（以前只在启动器窗口里滚，窗口一关就没了）；
- 自动脱敏：`sk-` 开头的串、三段式 JWT、`Bearer <token>` 一律打码（保留前 4 后 4）。

相关端点：`GET /admin/api/logs`、`POST /admin/api/logs/clear`、`GET /admin/api/logs/download`。

## 快速上手一个账号

```bash
# 方式一：OAuth 登录（免密，自动入池 + 自动兑换回退 Key）
.venv/bin/python cli.py login zai

# 方式二：手动加入现成凭证（JWT 三段点分 或 API Key）
.venv/bin/python cli.py add-account zai 名称 <jwt|key>
```

## 命令行

```bash
python cli.py serve [--port 3000]    启动网关 + 后台 UI
python cli.py login zai              通过 OAuth 登录 Z.AI 并自动入池
python cli.py add-account <zai|bigmodel> <name> <jwt|key>   添加账号
python cli.py accounts [zai|bigmodel]  查看账号列表
python cli.py remove-account <provider> <id|name>  删除账号
python cli.py quota                  查看各账号实时额度
python cli.py status                 查看配置概览
python cli.py set-admin-key <key>    设置后台密码
python cli.py export [file]          导出全部账号
python cli.py import <file>          导入账号
```

## 后台 UI

| 页面 | 说明 |
|------|------|
| `/admin/login` | 后台登录（Bearer 密钥鉴权，连续失败 IP 锁定防护，凭证加密存于浏览器 localStorage）|
| `/admin/accounts` | 账号池：新增/导入/导出、启用禁用、指纹换发、**实时额度与状态监控**、套餐领取（自动/手动）、**账号排序**（按额度/最近使用/名称/状态临时视图，或拖拽自定义顺序——自定义顺序即网关轮询优先序，落库重启不丢）|
| `/admin/monitoring` | **数据看板**：KPI 卡（Token / 请求数 / 生成速度 / 缓存命中 / 换模型 / 成功率环形）+ **来源用量（公网 / 内网 / 合计）** + Token 时序图 + 活动热力图 + 用量明细（明细 / 实时 / 按账号 / 按模型 / 按端点 / 按来源）|
| `/admin/logs` | **网关日志**：终端视窗实时查看（级别 / 模块 / 关键字筛选、自动滚屏、复制导出），日志落盘 `data/logs/` 保留 14 天 |
| `/admin/settings` | 后台密码、网关 API Key（脱敏掩码）、额度刷新间隔、单账号并发上限、**全局粘号**、**模型兜底**（开关 / 顺序 / 冷却 + 实时状态表）、**客户端接入**（一键写 magpie、多机同步、局域网分享）|

### 数据看板能看到什么

每个请求一行（SQLite `data/usage.db`，默认保留 30 天 / 5 万行，重启不清零），
看板把同一份台账按不同角度聚合（5 秒轮询，一次请求拿全部聚合，口径天然一致）：

| 区块 | 内容 |
|---|---|
| KPI 卡 | TOKEN 消耗（含输入 / 缓存 / 输出占比条）、请求数、生成速度、缓存命中率、换模型次数、成功率环形——每张带 sparkline 趋势与「对比上一等长周期」的涨跌 |
| Token 时序 | 按小时分桶，可切 Token / 请求数 / 吞吐量（原生 canvas，无外部图表库） |
| 最近请求 | 实时活动流（在途 + 刚结束） |
| 来源用量 | 公网（Cloudflare 隧道传入）/ 内网（局域网、本机）/ **合计** 三栏：请求数、占比、成功率、Token；点卡片即按来源筛选整个看板 |
| 工作回溯 | 近 12 周每日请求热力图（GitHub 风格） |
| 用量明细 | 六个维度 tab：逐请求明细（服务端分页，公网请求带「公网」标记）、实时环、按账号 / 按模型 / 按端点 / 按来源聚合 |

| 维度 | 字段 |
|---|---|
| 归属 | 时间、端点（`/v1/messages`、`/v1/chat/completions`）、账号 + 通道（JWT / API Key）、模型（含客户端点的原名） |
| 结果 | 终态 `outcome`（completed / failed / client_aborted / upstream_aborted）+ 独立记录的 HTTP 状态码 + 错误全文 |
| 速度 | 首字延迟（TTFT）、生成耗时、总耗时、**生成速度 tok/s**（按生成窗口算，不含排队与首字等待） |
| 消耗 | 输入 / 输出 tokens、**缓存命中 tokens 与命中率**、上游上报的 `credit`（有则记） |
| 换号 | **换号轨迹**（从谁换成谁、为什么，见下节） |

- 上游没报 usage 的请求也照样落行并标记 `usage_missing`：请求数、成功率、时延样本不会因此失真。
- `/v1/messages` 是原样透传，token 靠旁路嗅探（`UsageSniffer`）统计，不再永远是空。
- 账号口径：多账号重试不分裂，记**最终服务**的那个账号；`outcome` 与 HTTP 码分开记——「200 但客户端取消」不会被算成成功。
- 聚合（按账号 / 按模型 / 按端点 / 按小时）都在 SQL 侧按当前筛选条件算，翻页不改变口径。
- 换过模型的请求单独计数（KPI 卡「换模型」）：台账里「模型」记**实际服务**的模型，「请求模型」记客户端点的原名，两者不同即为一次兜底。

账号池页实时展示每个账号的**状态（正常 / 额度用完 / 限流 / 异常 / 禁用）**、各模型剩余额度、
调用与失败次数；并提供「手动刷新额度」与「批量领取套餐」入口。

## 局域网访问（其它电脑打开后台）

网关默认监听 `0.0.0.0`（`ZCODE_HOST` 可改），端口 3000。但 **Windows 防火墙默认拦截入站**，
所以首次要让同网段的其它电脑打开后台，先放行：

```bat
allow-firewall.bat          :: 双击 → UAC 确认 → 自动放行「网关解释器」与 3000 端口
allow-firewall.bat remove   :: 撤销这两条规则
```

之后在其它电脑用 `http://<本机IP>:3000/admin` 打开（本机 IP 看「客户端接入」页的局域网地址，
或 `ipconfig`）。自检：`curl http://<本机IP>:3000/meta` 应返回 `{"version":"..."}`。

> ⚠️ 后台默认口令是 `zcode`，**放行前先改掉**（设置 → 后台密码）。`/v1/*` 另有网关 API Key
> 单独把关（设置 → 网关 API Key），两者不要用同一个值。

## 公网访问（换个网络也能用）

网关没有「只允许局域网」的代码限制：进程监听 `0.0.0.0:3000`，**只要网络层能路由到这个端口就能用**。
真正要解决的是三件事——可达性（家用宽带多为 NAT / CGNAT，外网连不进来）、TLS（裸 HTTP 会把 API Key
和后台口令明文暴露）、鉴权（`/admin` 是整台机器的控制面）。

### 一键开启（Cloudflare 隧道，已内置）

网关自带隧道管理，不需要公网 IP、不用在路由器上开端口，也不用另开一个 cloudflared 窗口：

- **双击 `start-public.bat`**：等同平时的启动脚本，只是额外让网关自己拉起一条临时隧道。
  几秒后控制台出现 `[+] tunnel 公网地址已就绪：https://xxxx.trycloudflare.com`。
- 或者正常启动网关，在**设置 → 公网访问**点「一键开启公网访问」：页面直接给出公网地址、
  OpenAI / Anthropic 端点，并可一键复制成「粘三样」发给对方。
- 临时地址是随机域名，**随进程变化、网关关闭即失效**；开过一次会写进数据库，下次启动自动重开
  （想常驻就用下面的固定域名）。
- 开启时会检查：后台口令是否还是默认值、网关 Key 是否为空——任一命中就在卡片上红字提醒。

固定域名（需要域名托管在 Cloudflare）：控制台建隧道 → 复制 token → 粘进「公网访问 → 固定域名」，
Public Hostname 指向 `http://127.0.0.1:3000`。等价的手动命令：

```bat
cloudflared tunnel login
cloudflared tunnel create zcode
cloudflared tunnel route dns zcode gw.example.com
cloudflared tunnel run --token <token>
```

- 隧道是**本机出站**连接，所以可以把 `ZCODE_HOST` 设成 `127.0.0.1`，再 `allow-firewall.bat remove`
  撤掉入站放行——3000 端口不再对局域网/公网敞开，只有隧道进得来。
- cloudflared 会注入 `CF-Connecting-IP`，`app/auth_admin.py` 优先读它，按 IP 的登录失败锁定照常生效。
- 想再收紧后台：Cloudflare Zero Trust → Access 里给 `gw.example.com/admin*` 加一条策略（邮箱 OTP / Google 登录），
  公网就打不开后台，`/v1/*` 仍可凭网关 Key 直连。
- 后台已做手机端适配（≤720px 头部拆两行、账号池每行改卡片、监控只留时间/账号/结果/内容），
  手机浏览器打开公网地址就能管理账号池和看日志。

环境变量：`ZCODE_TUNNEL=1` 让网关启动即开临时隧道（`start-public.bat` 就是这么做的）；
`ZCODE_CLOUDFLARED` 指定 cloudflared.exe 路径（不在 PATH 时）。

### 备选 1：Tailscale（只给自己的设备）

装 Tailscale 并登录，本机拿到 `100.x.y.z`；其它设备加入同一 tailnet 后直接访问
`http://100.x.y.z:3000/admin`。不对公网暴露任何端口，源 IP 仍是真实设备 IP，限流照常按 IP 生效。

### 备选 2：公网 IP + 端口映射 / VPS 反代

- 有公网 IPv4：路由器把 3000 映射到本机，**务必**再套一层 HTTPS（Caddy 自动证书最省事）。
- CGNAT（联通/移动宽带常见）拿不到公网 IP：用 VPS 跑 `frp` / `nps` 做反向代理。

> 反代只在 `X-Forwarded-For` 里写来源 IP 时，`auth_admin.py` 认不出真实客户端，所有请求都会算成反代那一个
> IP——一个人连续输错 8 次会把**所有人**（包括你自己）锁 5 分钟。nginx 补一行
> `proxy_set_header X-Real-IP $remote_addr;`，Caddy 用 `header_up X-Real-IP {remote_host}`。

### 暴露前的检查清单

1. **改掉后台口令**：默认 `zcode`，而 `/admin` 能改账号池、网关 Key 和全部设置，等于交出整台机器的控制权。
2. **网关 API Key 不能留空**：留空时 `verify_gateway_key` 直接放行，等于把上游额度免费开放给扫端口的任何人。
3. `ZCODE_HOST`：走隧道或本机反代时设 `127.0.0.1`；只有要直连局域网时才用 `0.0.0.0`。
4. 网关转发上游前会剔除 `x-forwarded-*` / `cf-*` 等中间件头（见 `app/agent.py`），不会把部署拓扑带出去。

### 对外之后怎么收紧

从外到内四层，**优先在 Cloudflare 层拦，不用改代码**：

- **Cloudflare Access 护住后台**：Zero Trust → Access → Applications 新建应用，域名填 `gw.example.com`、
  路径填 `admin*`，策略放行指定邮箱（Google / 邮箱 OTP 均可）。免费额度 50 用户。之后公网打开后台会先跳登录页，
  而 `/v1/*` 对 API 客户端保持直通。
- **WAF 自定义规则**（免费版 5 条）：按 IP / 国家 / UA / 路径粗过滤，例如
  `(http.request.uri.path contains "/admin" and not ip.src in {1.2.3.4 5.6.7.8})` → Block。
- **Windows 防火墙白名单**：只放行固定出口 IP，最硬也最脆（对方 IP 一变就断）：
  `netsh advfirewall firewall add rule name="zcode-gw" dir=in action=allow protocol=TCP localport=3000 remoteip=1.2.3.4,5.6.7.8`
- **绑定 127.0.0.1 + 隧道**（见上方「一键开启」）：源站不出公网，只能走隧道，绕不过 CDN 上的规则。

> Cloudflare 的 Rate Limiting 规则属付费版（Pro 起）；免费版靠 Access + WAF 已足够挡扫描器。

应用层现状（`app/auth_admin.py`）：`/v1/*` 是**单一共享 Key**，`hmac.compare_digest` 精确比对，
没有限速、没有按 Key 配额、没有 IP 白名单；且这个 Key 会被写进生成的客户端配置与导入链接
（`app/clients.py:424`、`:629`），转发一次链接＝转发 Key，Key 泄露只能整体更换、所有用户一起失效。
`/admin` 是口令 + 每 IP 连续失败 8 次锁 5 分钟；`/admin/*.html` 页面不打码即可下载，但数据接口全部要 Key。

需要「一 Key 一人、分别限额、随时停用」时，得在网关里加多 Key 表与按 Key 的配额检查（可复用用量台账），
这是唯一必须动代码的部分。

## 多账号轮询与故障转移

- 在账号池粘贴 Coding Plan JWT（3 段点分）或 API Key，每行一个即可加入轮询。
- 网关每次请求选择下一个「可用」账号，跳过用完 / 限流 / 异常 / 禁用的账号。
- **单账号并发限制**：所选账号在飞并发达上限时智能跳过，不排队、不计入 `MAX_ACCOUNT_ATTEMPTS` 计数。
- **模型并发已满（3009）**：上游按模型计的并发租约，上一个请求刚结束的 1–2 秒内再发会全部回 3009。
  整轮都只是这个原因时，网关短等 1 秒再来（最多 3 轮，`ZCODE_MODEL_BUSY_WAIT` / `ZCODE_MODEL_BUSY_ROUNDS`），
  接得上就接、接不上才 503，不会像过去那样原地等 60 秒把客户端吊住。
- **模型额度用完（1005）/ 不在套餐（3006）**：只换号、**不动账号状态**——那是「这个模型」的额度没了，同账号其他模型还能用；整轮都如此则按模型兜底换到下一个模型（见下）。
- **额度用完**（余额为 0 / 上游 402 / 错误体含 quota、余额不足等关键词）→ 标记 `exhausted`，换下一个账号。
- **上游 429** → **不冷却账号**，按 Retry-After（封顶）原地等待重试；等待期间释放该账号并发槽。耗尽后换号，账号保持可用。Plan 通道 429 耗尽且同账号有 API Key 时切回退通道。
- **上游 5xx** → 重试 3 次后标记 `cooling` 冷却 300 秒，自动换下一个账号。
- **真风控（3012 / 405 unusual activity）** → 标记 `disabled` 并熔断停用，防止账号进一步受损，需在后台手动恢复。
- **鉴权失败 401/403（非验证码）** → 标记 `invalid`，需重新登录。
- **验证码过期（403 / code 3007）** → 原地刷新验证码对同一账号重试（最多 3 次）。
- 达到请求上限仍无可用账号 → 返回 `503 no_available_account`。

账号状态机：

```
            ┌─────────┐   额度用完(定期自动再探)    ┌──────────┐
  登录 ────▶│ ACTIVE  │◀─────────────────────────│ EXHAUSTED│
            │         │   5xx 重试耗尽(冷却 N 秒) └──────────┘
            │         │◀───────┐      ┌─────────┐
            │         │        ├──────│ COOLING │
            │         │        └─────▶└─────────┘
            │         │ 401/403(非验证码)
            │         │──────────────────────▶ INVALID（凭证失效，重新授权）
            │         │ 3012/405(真风控)
            │         │──────────────────────▶ DISABLED（风控保护，后台手动恢复）
            └─────────┘
```

> 成功响应可清除 `COOLING` / `EXHAUSTED`；额度恢复时后台监控也会自动回 `ACTIVE`。

### 模型兜底（自动换模型）

额度是按**账号 × 模型**计的，不是按账号：实测同一个账号 GLM-5.3 还剩 300 万，GLM-5.3-Flash
已经用完。只会换号的网关在这种时候很被动——4 个账号的 GLM-5.3 都空了，哪怕池子里的
GLM-5.3-Flash 刚重置、完全可用，客户端拿到的仍是 `503`。

模型兜底给「模型」这一维补上轮询：**客户端点的模型永远先试**，只有一整轮里试过的每个账号
都被上游判成「这个模型不可用」时，才认定「该模型全池不可用」，按兜底链换到下一个模型重试。
客户端不用改任何东西。

兜底链（后台「设置 → 模型兜底」可改，默认）：

```
GLM-5.3 → GLM-5.3-Flash → GLM-5.2 → GLM-5-Turbo → GLM-4.7
```

- 请求模型永远排第一；其后按配置顺序，其中「有余额证据」的排在「状态未知」的前面
  （稳定排序，不打乱你写的顺序）。
- 已经判死的模型直接跳过；一个活的都没有时只试请求模型本身——让上游给出诚实的错误，
  而不是瞎换。
- 兜底链只对 `zai` 生效。`bigmodel`（智谱开放平台）的模型名与 Z.AI 不同，链默认是空的
  （= 不兜底），需要时用 `ZCODE_MODEL_FALLBACK_CHAIN_BIGMODEL` 显式给。
- 链里的模型名**大小写不敏感**：写 `glm-5.3-flash` 也能用。保存后设置页回填的是归一化后的
  官方名（`GLM-5.3-Flash`），和网关实际请求的名字一致——所以看到的可能不是你写进去的原文，
  这是有意的。

判死有两种证据：

| 证据 | 怎么来的 | 效果 |
|---|---|---|
| 反应式 | 一整轮里试过的每个账号都被上游判 1005 / 3006 | 记「该模型全池不可用」，TTL 内直接跳过它，不再拿 N 个账号去撞同一堵墙 |
| 主动式 | 账号额度快照里该模型所有账号的 `remaining` 都 ≤ 0 | 直接判死，**连上游请求都不发** |

只是「没问到」不算判死，还要同时满足两个前提：这一轮真的把可轮询的账号试完了（不是撞上
`MAX_ACCOUNT_ATTEMPTS=5` 就收工），且没有账号因为「本账号并发已满」被跳过——否则流量
会被白白赶去更弱的模型。

成功一次就自愈：任何一次真实成功都会清掉死标记、记一条「实测可用」的信任票（同样在 TTL
内有效），所以额度窗口刚重置、刚领到新套餐、额度快照陈旧这几种情况都不需要人工干预。
TTL 设 0 = 不跳过，每次照试。

**不换模型的情况**（这些是账号或通道问题，换模型救不了）：

- `3009` 模型并发已满 → 短等重试整轮（见上面并发那段），不换模型也不换号；
- `402` / 余额不足关键词 → 账号整体没额度了，换号；
- `401` / `403`（非验证码）→ 换号 / 标 `invalid`；
- `5xx` → 重试耗尽后冷却换号；
- `3012` / `405` 风控 → 熔断换号。

客户端怎么知道被换了：

| 头 | 方向 | 含义 |
|---|---|---|
| `x-zcode-model` | 响应 | **实际服务**的模型 |
| `x-zcode-model-requested` | 响应 | 客户端原本点的模型，**只有换过才有** |
| `x-zcode-no-fallback: 1` | 请求 | 本次请求关掉兜底：只要点的那个模型，宁可失败也不换（`x-zcode-model-fallback: off` 同义）。评测、回归对比用 |

配置项（后台改即时生效，落 meta 表，不用重启）：

| 后台「设置 → 模型兜底」 | 环境变量 | 默认 |
|---|---|---|
| 启用自动换模型 | `ZCODE_MODEL_FALLBACK` | 开 |
| 兜底模型顺序（逗号分隔） | `ZCODE_MODEL_FALLBACK_CHAIN` | `GLM-5.3,GLM-5.3-Flash,GLM-5.2,GLM-5-Turbo,GLM-4.7` |
| 判死冷却（秒） | `ZCODE_MODEL_FALLBACK_TTL` | 600（0 = 不跳过） |

> 设置页的「实时状态」表读的是网关自己的台账（`GET /admin/api/models`）：每个模型的状态
> （可用 / 未知 / 不可用）、剩余额度、有余额账号数、判死原因都在里面。「立即恢复」清掉单个
> 模型的死标记，「全部恢复」清空整份台账。

### 会话粘性（同一段对话不换号）

轮询是「还没人回答过」时的规则。一旦某段对话有了回答，网关会把它**粘在**那个账号上
（有效期 `ZCODE_AFFINITY_TTL`，默认 30 分钟）：上游按「账号 + prompt 前缀」缓存整段
对话，换号等于让上游把整段对话重新读一遍——缓存命中归零、钱照付、首字节更慢。
粘住的账号不可用（冷却 / 额度用完 / 禁用 / 并发满）时照常轮询到下一个。

会话指纹 = `system` 提示词 + 第一条 user 消息的哈希，多轮增长不影响（对齐 magpie 的
affinity 设计）。

### 全局粘号（正常情况下就固定用一个账号）

会话粘性只管「同一段对话」。但如果你不想看到后台一直在跳号，还有一层**全局粘号**
（`ZCODE_ACCOUNT_STICKY`，默认开，设置页可关）：

- 所有会话的流量都尽量留在**同一个账号**上，只有它不可用（额度用完 / 冷却中 /
  并发满 / 凭证失效）才轮到下一个；
- 会话粘性优先级更高（同一段对话仍先回它上次用的号）；
- 每次成功服务都会把这个账号钉成全局粘号，所以「上一个号恢复正常」之后会自动粘回去。

**粘号在忙怎么办？排队等它，而不是换号。** 单账号并发上限（默认 1）意味着同一个号
同一时刻只能跑一路；第二路请求到来时，老实现会立刻换下一个号（后台就开始跳号、
上游缓存也要重读一份）。现在它挂在「等这个号空出来」上（`ZCODE_QUEUE_WAIT_MAX`，
默认 120 秒），槽一释放立刻唤醒重试——**用户看到的永远是同一个账号**。
整池都占满时同样排队，而不是直接回 503；排队人数超过 `ZCODE_QUEUE_MAX_WAITERS`
（默认 16）或等满预算才 503，原因写进错误信息。

**为什么不把流量摊到多个账号？** 上游按「账号 × 模型」计并发租约（同一个号同一个
模型两路并发直接回 429 code 3009），摊开并不能提高并发上限，只会让缓存命中率下降、
后台看起来一直在跳号。

### 换号原因（每个请求都可追溯）

台账里每个请求都记着**换号轨迹**：从哪个账号换成哪个、为什么。请求监控的「账号」列
显示蓝色「换号 N」徽标，悬停或点开详情看完整链路，例如：

```
账号A → 账号B：GLM-5.3 并发已满（上游 code 429）
账号B → 账号A：GLM-5.3 并发已满（上游 code 429）
```

记录口径覆盖全部 19 处换号分支：模型并发满（3009）、模型额度用尽（1005）、不在套餐
（3006）、本账号并发已满、429 限流耗尽、5xx 冷却、风控（3012）、鉴权失败（401/403）、
凭证无效、连接失败、验证码连续失败。**「跳号」是否有必要，看原因就知道了**。

### 流式保活与「未响应」

- **思考内容透传**：GLM 这类模型的思考阶段上游只发 `thinking_delta`，网关把它转成
  OpenAI 的 `delta.reasoning_content`（Anthropic 端点原样透传）。丢掉这些事件，客户端
  在整段思考期间收不到任何字节，界面就只能显示「未响应」——这是 2026-10-27 实测到的
  根因（一次回复 1996 条 thinking_delta，客户端静默 39 秒）。
- **SSE 心跳**：客户端空闲满 `ZCODE_SSE_KEEPALIVE_IDLE`（默认 15s）补一条注释帧
  `: keep-alive`，客户端与中间代理都能判断连接还活着。
- **上游看门狗**：上游彻底静默满 `ZCODE_UPSTREAM_STALL`（默认 600s）判定连接已死并
  断开，避免半死 TCP 连接把请求永久挂住。
- **缓存命中可见**：`cache_read_input_tokens` 报成 `usage.prompt_tokens_details.cached_tokens`，
  客户端状态栏的「命中」才有数。

## 客户端一键接入（magpie）

后台「客户端接入」页可以把本网关**一键写进 AI 客户端的配置**，让客户端（以及它管理的
agent）直接用上这里的 ZCode 账号池。第一个支持的是
[magpie](https://github.com/yetone/magpie)：菜单栏里的「每个 agent 的模型都在一处」——
它自己是个本地网关（`127.0.0.1:3425`），把 provider 分给 Claude Code / Codex / Gemini CLI /
DSH / ZCode 等 agent。

一键接入做三件事：

- 往 magpie 的 `providers.json`（默认 `~/.config/magpie/providers.json`，Windows 也是这个
  路径）写一条 provider（id 固定为 `zcode2api`），同时给出 OpenAI（`…/v1`）与 Anthropic
  （根地址）两个入口，模型取本网关 `/v1/models` 的清单；
- 写前备份原件、`0600` 落盘、临时文件 + `os.replace` 原子替换（中途失败按原字节回滚）；
- 「一键还原」回到首次接入前的字节；期间文件被外部改过，则只摘掉本网关那一条 provider，
  不覆盖其它改动。

接入后：打开 magpie → **Agents** 页 → 给 Claude Code / Codex 选 `zcode2api/glm-5.3-flash`。

也可以命令行（客户端不在网关这台机器上时）：

```bash
python -m app.clients list
python -m app.clients apply --client magpie --base-url http://127.0.0.1:3000 --key <网关 Key>
python -m app.clients restore --client magpie
```

看板上还给出 magpie 官方的 `magpie://import?…` 导入链接（复制 / 直接打开）：那条路由
magpie 自己弹窗确认、自己落盘，适合不想让网关写文件、或 magpie 跑在别的机器上时用。
注意首次使用前需要启动过一次 magpie（`magpie://` 协议由它注册）。

细节与「新增一款客户端」的步骤见
[docs/development/08-client-integration.md](docs/development/08-client-integration.md)。

## 无痕验证（真浏览器求解）

JWT 账号调用上游（对话 / 领取套餐）时需携带阿里云无痕验证参数（请求头
`X-Aliyun-Captcha-Verify-Param`）。该参数由 **Node + puppeteer-core 驱动真实
Chromium** 运行阿里云官方无痕 SDK 求出（happy-dom 伪造环境 2026-09 起被上游风控
全拒，`solver.js` 仅留作 `ZCODE_CAPTCHA_SOLVER=legacy` 回滚）。

- 求解器：`captcha_node/solver_pw.js`，首次使用前执行 `cd captcha_node && npm install`。
- **浏览器自动探测**：优先读 `ZCODE_CHROMIUM_PATH`；未设置时按平台常见路径找——Windows
  先找 Edge（系统预装，也是 Chromium）、再找 Chrome；Linux/macOS 找 `chromium` /
  `google-chrome` / 两个 .app。装的 Edge 就能用，不用额外下载。
- 预解池：`app/captcha.py` 后台常驻 1~2 枚 token（TTL 95s），热路径直接取现成的
  （亚毫秒），取走后立刻补货；上游返回挑战时整池作废重解。
- 求解失败会把求解器的 stderr 原样带出来（例如「未找到可执行 Chromium」），
  不会再只显示一句「多次重试无结果」。
- 配置兜底：`client/configs` 拉取失败时回落内置默认 scene/region/prefix。

## 鉴权

- **后台鉴权**：所有 `/admin/api/*` 需 `Authorization: Bearer <后台密码>`（也支持 `?app_key=`），内置 IP 失败节流保护。
- **网关鉴权（可选）**：在「设置」配置「网关 API Key」后，`/v1/messages` 与 `/v1/chat/completions` 须携带
  `Authorization: Bearer <key>` 或 `x-api-key: <key>`；留空则不校验。

## 环境变量

`cp .env.example .env` 后按需修改（.env 永不入库）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ZCODE_PORT` | 3000 | 服务端口 |
| `ZCODE_HOST` | 0.0.0.0 | 监听地址 |
| `ZCODE_ADMIN_KEY` | change-me | 后台密码初始值（之后写库，以库为准）|
| `ZCODE_GATEWAY_KEY` | 空 | 网关 API Key；留空不校验（生产务必设置）|
| `ZCODE_TUNNEL` | 关 | `1` = 启动即开一条临时公网隧道（`start-public.bat` 用的就是它；后台开过一次也会写库）|
| `ZCODE_CLOUDFLARED` | 自动探测 | cloudflared 可执行文件路径（不在 PATH 时指定）|
| `ZCODE_DATA_DIR` | data | 数据目录（SQLite 存放处）|
| `ZCODE_QUOTA_REFRESH_INTERVAL` | 1800 | 后台刷新额度间隔（秒），0 关闭 |
| `ZCODE_COOLING_SECONDS` | 300 | 限流冷却时长（秒）|
| `ZCODE_ACCOUNT_CONCURRENCY` | 1 | 单账号并发上限：上游对「账号 × 模型」并发硬限很低（超了回 429 code 3009），默认一账号一路，0 不限（运行时可在后台设置改，以库为准）|
| `ZCODE_QUEUE_WAIT_MAX` | 120 | 所有账号并发都占满时排队等空位的最长秒数（任一槽释放即放行），0 = 不排队、直接 503 |
| `ZCODE_QUEUE_MAX_WAITERS` | 16 | 同时排队的人数上限，超过直接 503（防全池卡住时请求无限堆叠）|
| `ZCODE_CLAIM_ROUND_INTERVAL` | 3600 | 套餐自动领取轮间隔（秒），0 关闭（运行时可在后台设置改，以库为准）|
| `ZCODE_APP_VERSION_FOLLOW` | 关 | 自动跟进官方发行版（默认关：3.17.x 起 messages 形状变化，盲跟会被 3012 打风控；开启前先用健康账号验证一次真实对话）|
| `ZCODE_VERSION_SYNC_INTERVAL` | 21600 | 版本巡检间隔（秒），默认每 6h 拉一次官方发行清单比对「钉版 vs 官方最新」|
| `ZCODE_MARKETING_URL` | 官方 `marketing/touch` | 运营位投放端点覆盖（测试指向 Mock；限时套餐的第二条发现通道）|
| `ZCODE_CLI_SYSTEM_STRICT` | 开 | CLI 形态把 `system` 收敛为官方前两块（上游只认这个形状，多一块就 3012）。`0` = 回到「官方块 + 客户端块」旧行为 |
| `ZCODE_CLI_SYSTEM_FOLD_TO_USER` | 开 | 被剥掉的客户端 system 块折叠进首条 user 消息（指令不丢）。`0` = 彻底丢弃 |
| `ZCODE_BODY_TRACE` | 开 | 命中风控（3012）时把那次真实请求体落 `data/debug/`（只留最近 5 份，日常不落盘），用于定位形状问题 |
| `ZCODE_RISK_STRIKE_GUARD` / `_WINDOW` | 3 / 1800 | 风控次数到 3/4 的账号，最近一次命中后 1800 秒内不再打上游（防连试把自己推到禁用）；`0` = 关闭 |
| `ZCODE_MODEL_FALLBACK` | 开 | 模型兜底（自动换模型）总开关（运行时可在后台设置改，以库为准）|
| `ZCODE_MODEL_FALLBACK_CHAIN` | `GLM-5.3,GLM-5.3-Flash,GLM-5.2,GLM-5-Turbo,GLM-4.7` | 兜底模型顺序，逗号分隔（运行时可在后台设置改；空 = 不兜底）|
| `ZCODE_MODEL_FALLBACK_TTL` | 600 | 判「该模型全池不可用」后跳过它的秒数，0 = 不跳过（运行时可在后台设置改）|
| `ZCODE_ACCOUNT_STICKY` | 开 | 全局粘号：所有会话尽量固定用一个账号，只有它不可用才轮询（运行时可在后台设置改）|
| `ZCODE_QUEUE_WAIT_MAX` | 120 | 全池并发占满 / 粘号在忙时排队等槽的上限（秒），0 = 不排队直接换号或 503 |
| `ZCODE_QUEUE_MAX_WAITERS` | 16 | 同时排队等待的上限，超过即 503（保护进程不被打爆）|
| `ZCODE_LOG_RETENTION_DAYS` | 14 | 网关日志落盘保留天数（`data/logs/gateway-YYYYMMDD.jsonl`），0 = 永不清理 |
| `ZCODE_NODE_PATH` | node | 验证码求解所用 Node 可执行文件 |
| `ZCODE_CHROMIUM_PATH` | 自动探测 | 求解器用的浏览器可执行文件；不设则按平台找 Edge / Chrome / chromium |
| `ZCODE_CAPTCHA_SOLVER` | pw | 求解器实现；`legacy` 回滚到 happy-dom 版 solver.js |
| `ZCODE_CAPTCHA_RETRIES` | 4 | 单次求解失败重试次数 |
| `ZCODE_CAPTCHA_TIMEOUT` | 240 | 单次求解超时（秒）|
| `ZAI_UPSTREAM_URL` / `ZAI_FALLBACK_URL` / `BIGMODEL_UPSTREAM_URL` | — | 上游端点覆盖 |

## 开发与测试

```bash
.venv/bin/python -m pytest            # 全量测试（Mock 上游，无真实网络）
.venv/bin/ruff check app tests
.venv/bin/mypy app/constants.py
```

测试全部走 **Mock 上游**：`tests/mock_upstream/` 模拟 Z.AI 被依赖的全部端点，
支持 `x-mock-scenario` 故障注入（具体见 `docs/testing`），不依赖真实网络，可离线回归。

## 文档

完整文档见 [`docs/README.md`](docs/README.md)：

- **开发**：架构 · 数据格式 · API 规范 · 上游协议 · 开发指南 · 路线图 · 客户端一键接入
- **测试**：测试策略 · 单元用例 · 互通向量 · 集成 E2E · 验收标准

## 技术栈

- Python 3.11+ · FastAPI · Uvicorn · httpx
- SQLite（账号 / 设置持久化，WAL 模式）
- Node.js + jsdom（免浏览器求解阿里云无痕验证）

## 许可证

本项目采用 [AGPL-3.0](LICENSE) 许可证。

## 免责声明

本仓库仅供学习、研究、个人实验与内部验证使用，不提供任何形式的商业授权、适用性保证或结果保证。

作者不因使用、修改、分发、部署或依赖本项目产生的任何直接或间接损失、账号封禁、数据丢失、
法律风险或第三方索赔负责。请勿将本项目用于违反服务条款、协议、法律或平台规则的场景；商业前请自行确认许可证与相关协议。

