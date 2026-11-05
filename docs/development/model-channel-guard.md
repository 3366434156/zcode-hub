# 模型通道硬闸（model channel guard）

2026-11-05 事故后新增。用户口径：**没有检测到 `zcode2api/GLM-5.3-Flash` 就必须停止，不管什么原因。**

## 事故一句话

唤醒器因上下文超阈值做「轮换」时，用 `--prompt`（不带 `--resume`）新建会话；新会话**不继承旧会话的模型绑定**，而是继承 magpie GUI 的「当前模型」。当时 GUI 停在 `hy4-preview`，于是 6 个轮换会话从 03:50 起全部走 `wb-proxy-cn`，5 小时 2,457 次请求 / 642M tokens / **11.1K 积分（¥462）**，网关 6.5 亿额度却空放到期。产出只有 816 KB 的「待命轮」文档和 1 个源码文件的 4 次编辑。

关键认知：`pickedModels.zcode` 只约束 magpie 的候选列表，**管不到新会话的初始模型**；程序侧（settings/profiles/providers/applied/affinity/环境变量/`--model`）都无法写入，只能由用户在 GUI 里切。

## 规则

- 白名单：`ZCODE_REVIVE_MODEL_ALLOW`，默认 `zcode2api/GLM-5.3-Flash,zcode2api/GLM-5.3`。
- 判定依据是**实际用过什么**，不是配置里写了什么。
- 三条证据依次尝试：
  1. 会话库 `model_usage` 最近一条的 `provider_id` + `model_id`（最完整）；
  2. magpie `GET /v1/magpie/usage/requests`（含非 magpie 直连的日志解析结果）；
  3. magpie `GET /v1/magpie/route?session=<id>`。
- 三条都查不到 = `unknown`，**不是放行**：宽限期后按违规停机。

## 三层拦截

| 层 | 时机 | 行为 |
|---|---|---|
| 启动审计 `audit_bindings()` | 网关启动（`app/main.py`） | 绑定会话历史通道不对 → 立刻摘监听 + 封锁；本次启动把 main/s2/s5 三个 hy4 会话摘掉，s3/s4（zcode2api）保留 |
| 唤醒前预检 | 每次 `tick()` 准备唤醒前 | 会话历史通道不是网关 → **一个请求都不发**，直接封锁 |
| 在跑巡检 `guard_tick()` | 独立线程，每 **5s** | 进程在跑但通道不对 → `taskkill /T`；查不到通道超 90s → 也杀；连续 3 次查不到 → 封锁 |

另外：用户自己在 GUI 里跑的监听会话，只要最近 15 分钟内上过非网关通道，也会被摘监听并封锁（Stop 钩子不能接着往付费通道续派）。

## 封锁语义（fail-closed）

命中后：

1. `taskkill /PID <pid> /T /F`；
2. 从 `data/_sup/continue.sessions` 摘除该会话（钩子与唤醒器都续不了）；
3. 写 `data/_sup/model-block.json`，并置 `locked=true` —— **全池停止唤醒**（一路走错说明 GUI 当前模型可能已错，后续新会话都会错）；
4. `data/_sup/revive.log` 留痕，监督页顶部红色横幅显示原因、实际通道、白名单。

`model-block.json` 结构：

```json
{
 "slots": {"main": {"session": "...", "channel": "wb-proxy-cn/hy4-preview",
                    "source": "session-db", "reason": "启动审计：绑定会话不是网关通道",
                    "killed": false, "at": 1793844658.8}},
 "locked": true,
 "reason": "启动审计：绑定会话不是网关通道（main：wb-proxy-cn/hy4-preview）"
}
```

## 解除

监督页「确认 GUI 模型正确，解除封锁」→ `POST /admin/api/burner/revive/unblock`（可选 `{"slot":"main"}`）。

解除只清封锁，**不会把旧会话塞回监听名单**——被封锁会话的模型绑定本身就是错的，还回去等于再烧一次。正确顺序：

1. magpie GUI 当前模型切到 `zcode2api/GLM-5.3-Flash`；
2. 在监督页给缺的炉位重新绑定**网关通道**的会话；
3. 点解除。

## 参数

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ZCODE_REVIVE_MODEL_ALLOW` | `zcode2api/GLM-5.3-Flash,zcode2api/GLM-5.3` | 白名单（精确匹配，项尾 `/` 按前缀） |
| `ZCODE_REVIVE_MODEL_GUARD_INTERVAL` | `5` | 在跑巡检间隔（秒） |
| `ZCODE_REVIVE_MODEL_GRACE` | `90` | 新进程必须在此时间内留下网关证据 |
| `ZCODE_REVIVE_MODEL_UNKNOWN_RETRIES` | `3` | 「查不到通道」连续几次后升级为封锁 |
| `ZCODE_REVIVE_MODEL_STALE` | `900` | GUI 手动会话「最近用过非网关」的时间窗（秒） |
| `ZCODE_REVIVE_ROTATE_TOKENS` | `0` | 默认关闭轮换；开启后新会话仍受硬闸约束 |
| `ZCODE_MAGPIE_URL` | `http://127.0.0.1:3425` | magpie 地址 |

## 验证（2026-11-05 10:12 上线实测）

- 审计命中 3 个：`main`、`s2`、`s5` → `wb-proxy-cn/hy4-preview`；全部摘除并封锁；
- `continue.sessions` 只剩 `s3`、`s4`（`zcode2api/GLM-5.3-Flash`）；
- `reviver.status().modelGuard.blocks.locked = true`，监督页显示红色横幅；
- `guard_tick()` 线程存活（`lastGuard` 每 5s 更新）；
- 单元测试 23 条覆盖：白名单、真实会话分类（hy4 → wrong / 网关 → ok）、立即封锁、未知宽限、启动审计、解除不自动恢复。
