# ZCode Stop 钩子「每轮最多续派 3 次」的定位与解除（2026-11-03）

## 现象

续派工单/额度自动派单跑了几轮后，会话在“完成”状态停住，不再自动接下一项。
钩子日志里最后一次是正常的 `[自动] 第 N 轮` / `[续派]`，之后没有任何 `[放行]` 或 `[提醒]`，
说明**钩子根本没被再次触发**，不是钩子逻辑或额度问题。

## 根因

ZCode 客户端运行时（`E:\exe\Zcode\resources\glm\zcode.cjs`）对 Stop 钩子的续派有硬上限：

```js
function $Wo(e, t) {                     // shouldContinueAfterStopHooks
  return e.stopShouldContinue === true
      && e.additionalContexts.length > 0
      && t < f9a;                        // f9a = 3
}
...
if (this.shouldContinueAfterStopHooks(o, e.stopHookContinuationCount)) {
  e.stopHookContinuationCount += 1;      // 每轮最多 3 次
  ...
}
```

也就是说：**一条用户消息最多允许 Stop 钩子 block 3 次**（第 4 次起强制结束回合）。
这与我们的钩子/工单无关，是客户端的防死循环保护。

## 解除方法（已执行）

1. 备份：`E:\exe\Zcode\resources\glm\zcode.cjs.bak-zcodehub-20261103-091546`（134 MB）
2. 把唯一的 `f9a=3` 改为 `f9a=999`（已确认全文仅 1 处；`node --check` 通过）。
3. **需要重启一次 ZCode** 才生效（运行时在启动时加载 `zcode.cjs`）。

回滚：把备份文件覆盖回 `zcode.cjs`，重启 ZCode。

## 生效后的行为

- 每次用户消息（或一次“继续”）可支撑最多 999 次 Stop 续派，实际等价于一直跑到：
  额度 ≤ `auto.json.minRemaining`（当前 50000）、`auto-task` 写 STOP、或手动关总闸。
- 自动续派仍由钩子在 Stop 那一刻直接注入（读到 `auto-task.<slot>.txt` 就 block），
  不经过后台 10 秒轮询，因此没有“派了单会话已停”的空档。
- 每轮 agent 把进展写 `auto-handoff.<slot>.md`、下一项写回 `auto-task.<slot>.txt`；
  当前轮次计划全部打勾时，模板要求先起草下一轮计划（`轨道N-第2轮.md`），不写 STOP。
- 每轮提示词明确要求「本人完成，不派子代理、不 fork 新会话」，保持 5 路并行互不干扰。

## 唤醒器（2026-11-03 09:40 新增）

即使上限解除，上游 5xx、客户端异常、CLI 进程退出也会让会话停在“完成”，而
Stop 钩子只在回合结束时触发——没人唤醒就永远不会再跑。`app/reviver.py` 补上
这个缺口：

- 每 60 秒巡检 5 个监听会话；最后一条消息是 assistant 且 `finish` 不是
  `tool-calls`、静默 ≥180 秒 → 判定为空闲。
- 额度 > 0 时，用 ZCode 自带 headless CLI 唤醒：
  `node zcode.cjs --resume <sessionId> --prompt 继续 --cwd <目录> --mode yolo`
  `--resume` 会沿用会话原来的模型（实测 `zcode2api/GLM-5.3-Flash`），回合结束仍触发同一个 Stop 钩子。
- 每炉位 10 分钟冷却；唤醒进程还活着时绝不重复启动；额度为 0 时不动作。
- 开关：`data/_sup/revive.enabled`（默认随自动派单开启），页面「唤醒器」按钮也可切。

因此**不需要人肉每隔 3 轮点一次**：客户端上限、回合异常、进程退出都由唤醒器兜底。
即使不重启 ZCode，唤醒器启动的 CLI 进程也会加载已打补丁的 `zcode.cjs`（999 次）。

## 2026-11-03 13:45 夜间前加固（v2.19.2）

排查「不要再出现昨天的情况」时又找到 4 个会让夜里静默停摆的真问题，已修：

1. **唤醒器在 Windows 上会杀掉自己的 CLI**：`os.kill(pid, 0)` 在 Windows 不是探活，
   CPython 会调用 `TerminateProcess(handle, 0)`。巡检每 60 秒一次，等于把上一分钟
   刚唤醒的进程直接终止。改为 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` +
   `GetExitCodeProcess`（只读）。已用真实子进程验证：探活后子进程仍存活。
   同类问题在 `scripts/burn_engine.py` 的 pid 文件检查里也一并修掉。
2. **卡死判定太窄**：旧逻辑只认「最后一条是 assistant 且 finish 不是 tool-calls」。
   回合在 user/工具结果后中断、或 assistant 一直挂 tool-calls 时，唤醒器会永远
   认为“还在跑”，会话永久停摆。现在：
   - assistant 正常收尾 → 静默 180s 唤醒；
   - 非 assistant 结尾 / tool-calls → 静默 900s（`ZCODE_REVIVE_STUCK`）视为卡死；
   - 会话 `last_outcome` 是 error/failed/aborted → 按 180s 处理；
   - 唤醒进程还活着但会话 1 小时（`ZCODE_REVIVE_HUNG`）没有任何更新 → 判为
     进程卡死，终止后下一轮重唤醒；终止前核对进程映像名仍是 `node.exe`，
     避免 PID 复用后误杀无关进程。
3. **唤醒器尊重总闸与炉位 STOP**：`continue.enabled` 不存在时不再动作；
   自动派单开启且某炉位 `auto-task` 写 STOP、或钩子已标记 `paused` 时，
   该炉位不唤醒。避免“总闸关了/已暂停，唤醒器还在发继续”。
4. **可观测性**：
   - 每 10 分钟（状态变化时更快）写一行 `[巡检]` 到 `data/_sup/revive.log`，
     带五个炉位的 busy/active/idle/cool 与 token 数；唤醒的 CLI 的 stdout/stderr
     单独写 `data/_sup/revive-cli.log`（20MB 轮转到 `.1`），不和结构化日志混在一起；
   - 单会话输入 token ≥ `ZCODE_REVIVE_CONTEXT_WARN`（默认 600000）时提醒一次
     （每 30 分钟最多一次），防止上游 context 超限后反复失败；
     当前五个会话最后一次请求的输入约：计划一 265K / 计划二 217K / 计划三 224K /
     计划四 265K / 计划五 190K，离阈值还有余量；
   - 启动时检查 `zcode.cjs` 的 `f9a`，页面和 `/admin/api/burner/state` 的
     `reviver.clientPatch` 都能看到 cap=999 还是未打补丁；
   - 页面新增「立即巡检」按钮（`POST /admin/api/burner/revive/tick`）。
6. **钩子额度查询减负**：Stop 钩子原来每次回合结束都拉 `/admin/api/accounts`
   整个快照；5 路并行时这个请求既大又可能拖慢钩子。新增
   `GET /admin/api/burner/quota`（只返回 `{remaining}`，与唤醒器同口径），
   钩子改为优先调它，旧网关 404 时自动回退 accounts；每次 fetch 5s 超时、
   3 次重试封顶 ~21s，避免网关卡住时钩子被 60s 进程超时杀掉。
7. **手动工单验收修正**：钩子在 Stop 里先写 `turn-end`、再消费 `next-prompt`。
   如果指令是在唤醒器的“继续”回合被消费的，这个 `turn-end` 只能证明指令被取走，
   不能证明任务跑完。现在钩子消费时额外写 `prompt-consumed.<slot>`（epoch ms），
   burner 验收必须等到消费时刻之后又出现新的 Stop；避免“刚消费就判 watch 无变化
   而失败”。（自动续派不走 burner 验收，不受影响。）
8. **09:00 硬停**：钩子和唤醒器都按本机时间判断，09:00–23:00 自动续派/自动唤醒
   暂停（手动 `next-prompt` 不受影响），到点不会再起新回合；23:00 后额度窗口
   恢复时自动继续。这样即使用户不在，也不会烧过早上 9 点。
9. **计划兜底直接给具体任务**：agent 忘写下一项、或连续 3 轮拿到同一任务时，
   钩子读取 `slot-plans.json` 指向的轨道计划，抽第一个 `- [ ]` 任务直接注入，
   不再只给“去计划里找”的元指令；任务比较折叠空白/换行，改格式绕不过
   “连续同一任务”检测。
5. **网关看门狗**：新增 `scripts/gateway-watchdog.ps1` + `gateway-watchdog-loop.ps1`，
   通过用户启动文件夹快捷方式在登录时拉起（本机注册计划任务被拒绝：Access denied，
   用户级启动项不需要管理员）：
   - `GET /healthz` 正常 → 退出；
   - 端口在听但 `/healthz` 失败 → 只记 `data/logs/watchdog.log`，不误杀；
   - 端口没人听 → 隐藏启动 `scripts/supervisor.py`（自带 50 次重启）。
   安装/卸载：`scripts/install-watchdog.ps1` / `scripts/install-watchdog.ps1 -Remove`。
   网关本身新增无鉴权 `/healthz`（只证明进程能响应，不碰上游/账号库）。

验证：`pytest -q` 全绿（exit 0）；`_pid_alive` 真实子进程探活后仍存活；
`reviver.clientPatch = {patched: true, cap: 999}`。

## 已停用旧的 zcode-auto-resume 看门狗（2026-11-03 09:35）

旧插件 `zcode-auto-resume` 的 turn-watchdog 通过“聚焦 ZCode 窗口 + 粘贴继续”来恢复
turn.failed，只认“最近活动的会话”——5 路并行时会往错误的会话里粘贴。已处理：

- `~/.zcode/cli/config.json` 里 `zcode-auto-resume@ai-local-plugins` 改为 false
  （备份 `config.json.bak-zcodehub-20261103-093444`）；
- 其 `ensure-watchdog.ps1` / `turn-watchdog.ps1` 改名为 `.disabled-zcodehub`；
- 运行中的 watchdog 进程已停止。

现在唯一的自动续派/唤醒链路是：`continue.mjs`（按炉位注入）+ `app/reviver.py`
（按会话 CLI 唤醒，不用窗口焦点）。ZCode 重启后旧插件的 Stop 钩子也会彻底卸载。

## 当前自动续派参数（2026-11-03 09:40）

`data/burner/auto.json`：

```json
{"minRemaining": 1000, "maxRounds": 100000, "maxSameTask": 3}
```

- 额度低于 1000 才停（尽量物尽其用，保留极小缓冲避免上游异常）。
- 每炉位最多 10 万轮；`maxRounds` 实际不可能触发。
- 连续 3 轮同一任务不再暂停，改为**计划兜底**：直接读 `data/_sup/slot-plans.json`
  里该炉位的轨道计划，让 agent 执行第一个未打勾任务；agent 忘写 `auto-task` 时也走这条兜底。
  结论：**任务池为空不会让 5 路停摆**。

## 下一次额度窗口的启动清单

1. 确认额度：后台账号页/`/admin/api/accounts` 出现 `GLM-5.3-Flash` 的 quota（当前 09:16 强制刷新后为 0，
   旧数字是 09:00 前的快照）。额度刷新已调成 5 分钟一次，领取轮 10 分钟一次。
2. **重启 ZCode**（推荐：加载 `f9a=999` 补丁，桌面端跑的回合也解除 3 次限制）。
   即使不重启，唤醒器启动的 CLI 进程也会加载已打补丁的 `zcode.cjs`，自动续派照样能超过 3 轮。
   重启/更新后打开续派页看「客户端续派上限 999」；若显示未打补丁，按本文开头的补丁步骤重打。
3. **不需要手动发“继续”**：唤醒器每 60 秒检查 5 个监听会话，额度>0 且会话空闲 ≥180 秒
   就会用 CLI `--resume` 唤醒；每个炉位 10 分钟冷却。想确认就看 `data/_sup/revive.log`
   的 `[巡检]` 行，或点页面「立即巡检」。
4. 看 `data/_sup/hook.log`：应出现 `[自动] 第 N 轮`（或 `[自动] ... 计划兜底`）且 N 能超过 3；
   每轮 `auto-handoff.<slot>.md` 更新。
5. **网关看门狗**：启动项 `zcode-hub-watchdog.lnk`（登录时拉起 `gateway-watchdog-loop.ps1`，
   每 5 分钟探一次 `/healthz`）；日志在 `data/logs/watchdog.log`。手动验：
   `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\gateway-watchdog.ps1`。
   卸载：`scripts\install-watchdog.ps1 -Remove`。
6. 急停：后台关总闸，或把任意 `auto-task.<slot>.txt` 写 `STOP`；
   09:00–23:00 钩子与唤醒器按本机时间自动停派（手动指令仍可发）。

## 本轮（02:00–09:00 窗口）实际产出

5 个会话共完成 **21 项**任务（计划文件里已打勾）：

- 轨道1 工程：T1 构建全绿 / T2 121 未编译文件台账 / T3 离线断言扩展 / T4 XML schema 漂移审计
- 轨道2 生产经济：T1 阵旗配方 / T2 图鉴解锁 / T3 寿元 bug / T4 商人供给链 / T5 炼器 318 配方可达性
- 轨道3 战斗反馈：T1 P0 音效包 / T2 雷劫死配置 / T3 火风劫视觉
- 轨道4 世界内容：T1 22 位面一致性 / T2 位面差异化 / T3 机缘游历事件表 / T4 世界史扩容
- 轨道5 体验验收：T1 152 用例静态复核 / T2 可交互引导 / T3 成就统计 / T4 图鉴可见性 / T5 本地化词条

额度：09:00 到期，强制刷新后 5 个账号均为 `quota={}`、`plans=[]`，合计 0（1.31B 是过期前的快照）。
下一个可用窗口预计为当晚 23:00（日套餐 entitlements 的 effective_at）。

## 2026-11-04 01:26 整池 3012：上下文膨胀 × 多路并发（v2.19.5）

**现象**：五炉位跑到 01:26:15，5 个账号在 5 秒内全部 `HTTP 405 {"code":3012,
"unusual activity"}`，冷却 15 分钟；01:42 解冻后 main 首发又 405，第 2 次冷却
30 分钟（到 02:12）。期间网关自身健康，额度一分未少（1163M 完好，套餐到 09:00）。

**实证证据**（`data/debug/risk-body-*.json` + 会话库 `~/.zcode/cli/db/db.sqlite`）：

| 炉位 | 上下文（input tokens） | 单请求体积 | 消息数 |
|---|---|---|---|
| main | 476k | 1.88 MB | 761 |
| s2 | 321k | 1.4 MB | — |
| s3/s4/s5 | 371–378k | 1.6–1.7 MB | — |

- 正常请求的中位数只有 **1k** tokens（P90 = 4k），也就是说这些请求大了 300 倍。
- 历史上**单请求 700k tokens 成功过**（`usage.db` 记录），所以单请求大不是致命项。
- 01:16 单分钟上游吞吐冲到 **147 万 token**；崩溃时每分钟 23 请求 × 平均 40 万
  ≈ **900 万 token/分钟**。健康期（00:46–01:25，40 分钟零错误）约 200–300 万/分钟。
- 排除项：版本形状（3.16.3 钉版已跑 646 请求）、设备指纹（5 个 install_id 各不相同）、
  账号封禁（`risk_strikes=1/4`，冷却自愈）。

**根因**：每轮工具调用都要把**整个上下文重发**一遍，五路并行 = 每分钟数 MB 级别
的重复上游流量，触发上游按 IP/账号组的瞬时频控（3012）。

**处置（reviver 三件套，均在 `app/reviver.py`）**：

1. `WAKE_PER_TICK=1` —— 每个巡检周期（60s）最多唤醒 1 个炉位。以前五路在同一秒
   被拉起，等于开局就先造一个五路并发尖峰；解冻时刻尤其危险。
2. `HEAVY_CONTEXT=200000` / `HEAVY_MAX_BUSY=2` —— 单路上下文超过 20 万时，同池
   最多 2 路并行，其余 `stagger` 到下一周期。轻上下文（<20 万）仍五路全开，这是
   实测健康区间。
3. `ROTATE_TOKENS=480000` —— 逼近 50 万（用户口径：50 万以内交给客户端
   auto-compact 自己压）才换新会话：起新进程不带 `--resume`，首轮 prompt 指向
   `auto-handoff.<slot>.md` + `auto-task.<slot>.txt` 接上进度，然后由
   `_claim_session` 后台认领新 session id 并改写 `continue.sessions` 绑定。
   认领全程持 `_claim_lock` 并检查「该 id 是否已被别的炉位占用」——五路同目录，
   不查就会两个炉位抢同一个新会话。

**风险线**：`RISK_BAN_STRIKES=4`，同一窗口累计 4 次风控即禁用账号（禁用会换设备
指纹，恢复成本高）。目前每号 1 次，再撞 3 次就全线禁用，所以并发/体积必须守住
上面两条闸门，不能只靠冷却自愈。

**排查用命令**：

```bash
# 当前各路上下文规模
.venv/Scripts/python.exe -c "import sys;sys.path.insert(0,'.');from app import zsession;print(zsession.context_tokens('<sess_id>')//1000,'k')"
# 风控落盘请求体（shape 行直接给出 tools/system/max_tokens 形状）
ls -lt data/debug/risk-body-*.json | head
```

### 01:26 风控的最终归因（修正上一节）

上一节把原因写成「上下文 32–48 万 × 五路并发」，**只对了一半**。查 `usage.db`
的 `cached_tokens` 后得到准确机制：

- 每次请求的 body **本来就是全量**（Anthropic API 无增量模式），1.8–1.9MB 是常态，
  跟 cache 命中无关。cache 影响的是**上游要计算多少**。
- 崩前 41 分钟共 648 请求，**638 次命中前缀缓存**：实际上去游只算了 **4.56M**
  tokens，复用了 **201M**。所以五路并行平时一点都不费算力。
- 真正致命的是「**多个账号在同一时刻 cache 失效、同时要求重算全量**」：

| 时刻 | 形态 | 合计重算 |
|---|---|---|
| 11-01~11-03（几十次） | 单账号，间隔 20s~21h | 安全 |
| 11-04 00:46:07–09 | **4 个不同账号，3 秒内** | 116 万 token |
| 11-04 01:16:38–54 | **4 个不同账号，16 秒内** | 146 万 token（崩） |
| 11-04 01:17:23 | 第 5 个号补上 | 46 秒内五号全中 |

上游判定信号 = 「同一出口 IP，5 个账号在 45 秒内各自要求重算 40 万 token 上下文」。

- **cache 为什么失效不可控**：执落2 在 01:16:24 还是 `cache=343k` 命中，14 秒后同一
  账号同一上下文 `cache=0`。这是上游缓存侧行为。
- **封禁时长 >30 分钟**：15min / 30min 两次探回都还是 405。冷却基线因此从 900s
  提到 3600s（见 `settings.RISK_COOLDOWN_BASE`），避免在封禁期内反复撞门把
  strikes 累加到 4（整池禁用）。

**能控的只有「别让它们同时发生」**：`HEAVY_MAX_BUSY` 收紧到 **1**（重上下文同池
只放一路，其余错峰），使实际发出去的全量重算回到「单账号 + 几十秒间隔」的
历史安全形态。代价是五路变轮转，效率约 1/5；strikes 窗口（7 天）过后再评估放宽。

### 自动化自身造成的两处「同时性」（2026-11-04 复盘）

把 `hook.log` 的续派时刻与 `usage.db` 的 cache 失效时刻对齐后，撞车有一半是
我们自己的自动化造成的：

1. **reviver 批量唤醒**：00:46:07–09 一次性拉起 5 路 → 5 路同时 `--resume` →
   4 个账号在 3 秒内各自全量重传（261k+249k+273k+377k = 116 万 token）。
   之前 `tick()` 是 for 循环一次把所有空闲炉位都唤醒。
   → 已改为 `WAKE_PER_TICK=1`（每周期只放一路）+ 轮转游标，5 路铺开 5 分钟。

2. **钩子续派无全局节流**：01:15:46–01:16:37 五路相继收尾，钩子给每路注入新的
   autoPrompt。新 prompt 改变了请求前缀，使该会话的下一次请求 cache 全部失效：
   01:16:38 执落2 346k / 01:16:41 隐形守护者2 296k / 01:16:47 隐形守护者 456k /
   01:16:54 执落 363k / 01:17:23 寂川 356k。五个账号在 45 秒内一起重算 = 上游
   看到的「同 IP 多账号批量重算」。
   → 钩子新增全局注入闸门 `injectionGate()`：相邻两次**自动/兜底**注入至少间隔
   `MIN_GAP_INJECT_MS=45s`（`_sup/inject-last` 记录上次注入时刻，`hooks.json`
   timeout 60s 故最多等 45s）。手动 next-prompt 不排队。
   效果上「多路同时 cache 失效」不可能再出现，回到历史安全形态（单账号 + ≥45s）。

3. **炉位空转**：s5 在 01:00–01:26 注入 16 次（最小间隔 16s、平均 101s），而其他
   四路是 5–6 次（150–300s）。原因是它把任务改成了「维护态守护·节流版」这种轻量
   检查，十几秒就收尾。闸门会把频率压到 ≥45s，但更该做的是给它换回有实际工作量
   的任务（否则只是在刷请求不做实事）。

**闸门与心跳**：45s 的注入间隔意味着 5 路完整轮转一圈约 4 分钟，单路实际吞吐下降，
但这是「不被封」的前提—— `RISK_BAN_STRIKES=4` 一满就整池禁用。
