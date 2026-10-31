# 08 — 客户端一键接入（magpie 等）

状态：与 `app/clients.py` 当前实现对齐（2026-10）。

## 1. 它解决什么问题

网关自己有账号池，但**用户真正在用的是各种 AI 客户端**：Claude Code、Codex、Gemini CLI、
DSH、ZCode……每个客户端各有一套配置文件、各写各的 base URL 与 Key。手工改一遍容易漏、
容易写坏，也容易忘记自己改了什么。

「客户端一键接入」把这层收进后台：点一下就把本网关作为一个 provider 写进客户端配置，
点一下就能还原；写入前留备份，写入后记账。第一个接入的是
[magpie](https://github.com/yetone/magpie)（菜单栏模型中枢，它自己也是本地网关
`127.0.0.1:3425`，把 provider 分给上面那批 agent）。

## 2. 模块与文件

```
app/clients.py                      # 核心：注册表 + 计划/写入/还原 + 备份账本 + CLI
app/routes/admin_api.py             # 路由：GET /admin/api/agents, POST .../apply, .../restore
app/routes/pages.py                 # 页面：GET /admin/agents
frontend/admin/agents.html          # 交互层：客户端卡片、状态徽章、一键接入/还原/导入链接
frontend/js/header.js               # 导航入口「客户端接入」
<DATA_DIR>/integrations/
├── state.json                      # 接入账本（每次写入的 SHA-256、首次备份指针）
└── backups/
    └── magpie/                     # 按客户端隔离
        └── 20261026-194500-providers.json
```

`DATA_DIR` 默认 `./data`（`ZCODE_DATA_DIR` 可覆盖）。备份每个原文件名最多留 10 份。

## 3. 设计原则

1. **纯标准库**：`json / os / re / shutil / subprocess / tempfile`，不引 YAML/TOML 解析器。
2. **非破坏性**：只认本网关自己那一条 provider（`id = zcode2api`），文件里其它 provider、
   `groups` / `searches` / `order` / `noAutoGroups` / `pluginLeft` 一律原样保留；写回沿用
   magpie 自身的两空格缩进风格，不重排用户数据。
3. **不敢写就报错**：目标文件不是合法 JSON、顶层不是对象、`providers` 不是数组时直接
   `AgentConfigError`（后台 400、CLI 退出码 1），绝不覆盖。对应 magpie 自己的
   `ErrUnreadable` 语义。
4. **零风险落盘**：写前备份 → 同目录临时文件 → `os.replace` 原子替换；任何一环抛错都把
   已写文件按原字节回滚。文件统一 `0600`（里面带网关 API Key），客户端原本更严的权限
   （如 `0400`）保留不动。
5. **外部修改感知**：账本记录写入后的 SHA-256。文件被用户或客户端改过时，还原**只摘掉本
   网关那一条**，不整体覆盖。

## 4. 客户端注册表

`app/clients.py` 的 `CLIENTS` 字典是唯一扩展点：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `str` | 唯一标识（小写短横线），同时是 API/CLI 的 `client` 参数 |
| `label` / `desc` | `str` | 看板展示名与一句话说明 |
| `protocol` | `str` | 该客户端吃哪种协议（`openai` / `anthropic` / 两者） |
| `provider_id` | `str` | 写入客户端配置时使用的 provider id |
| `paths` | `Callable[[], dict[str, Path]]` | 逻辑名 → 绝对路径（`providers` 为写入目标） |
| `probe` | `Callable[[], bool]` | 是否已安装（配置目录 / 配置文件 / 可执行文件三重探测） |
| `plan` | `Callable[[paths, ctx], dict[Path, str]]` | 纯函数：给定上下文返回「要写入的完整文件内容」 |
| `running` | `Callable[[], bool \| None]` | 进程是否在跑（探测失败返回 `None`，只用于提示） |
| `link` | `Callable[..., str]` | 客户端原生的一键导入链接（无则留空） |
| `strip` | `Callable[[str], tuple[str, bool]]` | 外部改动后「只摘掉自己那一条」的实现 |
| `notes` | `list[str]` | 看板上的使用提示 |

`ctx` 结构：

```python
{
    "base_url": "http://127.0.0.1:3000",      # Anthropic 根地址
    "api_key": "sk-…",                        # 网关 API Key（未启用则为 ""）
    "models": ["GLM-5.3-Flash", "GLM-5.3"],   # 来自 constants.AVAILABLE_MODELS
}
```

## 5. magpie 的写入内容

magpie 的配置目录（复刻 `appdir.Config()`，三大平台一致）：

| 情形 | 目录 |
|---|---|
| 默认 | `~/.config/magpie`（Windows 也是，**不**走 `%APPDATA%`） |
| `$XDG_CONFIG_HOME` 为绝对路径 | `$XDG_CONFIG_HOME/magpie` |
| 便携模式 | 可执行文件旁的 `data/`（里面已有 magpie 的文件，或旁边有 `.portable`） |
| 覆盖 | 环境变量 `ZCODE_MAGPIE_DIR` |

> Windows 上实测 magpie 的配置落在 `C:\Users\<user>\.config\magpie`：它刻意不用 Go 的
> `os.UserConfigDir()`，因为备份、同步与 Docker 挂载都按扁平目录里的文件名找。
>
> 一个坑：若网关进程在一个设置了 `XDG_CONFIG_HOME` 的 shell 里启动，而 magpie 是从
> 开始菜单启动的（没有该变量），两边看到的目录会不同。看板「写入」一行始终展示实际
> 目标路径，对不上时用 `ZCODE_MAGPIE_DIR` 显式指定。

`providers.json` 里追加/更新一条（`{"providers": [...]}` 是 magpie `provider.file` 的形状）：

```json
{
  "id": "zcode2api",
  "name": "zcode2api",
  "key": "<网关 API Key，未启用则空串>",
  "chat": "http://127.0.0.1:3000/v1",
  "anthropic": "http://127.0.0.1:3000",
  "models": ["GLM-5.3-Flash", "GLM-5.3"]
}
```

几个刻意的选择：

- **两个入口都给**：`chat`（OpenAI Chat Completions）+ `anthropic`（Messages 根地址）——
  magpie 按 agent 的原生协议选一个，Claude Code 走 Anthropic、Codex 走 OpenAI，最终都落到
  本网关的同一个账号池。
- **Key 可空**：magpie 的 `keyOptional()` 对 `127.0.0.1` 放行，所以网关没启用 API Key 时
  写空串也能用；启用了就写真实 Key。
- **不写 `catalog`**：`catalog: zhipuai` 会让 magpie 用 models.dev 的名字/思考档位，但那是
  对「模型 id 与 models.dev 完全一致」的假设；这里如实写本网关公布的模型 id，不做猜测。
- **模型 id 用本网关公布的写法**（`GLM-5.3-Flash`）；网关侧 `constants.MODEL_NAME_MAP`
  也接受小写别名，因此 magpie 里手改成 `glm-5.3-flash` 同样可用。

magpie 的 `magpie://import?…` 导入链接（由 `magpie_import_link()` 生成）：

```
magpie://import?name=zcode2api&id=zcode2api&chat=http%3A%2F%2F127.0.0.1%3A3000%2Fv1
  &anthropic=http%3A%2F%2F127.0.0.1%3A3000&key=…&models=GLM-5.3-Flash%2CGLM-5.3
```

参数语义见 magpie `internal/provider/importurl.go`：`name=` 必填（无 preset 时）、
`chat/responses/anthropic/gemini` 至少给一个、`key=` 可省略、`http` 只允许回环/内网地址。
**链接本身不落盘**——magpie 收到后弹窗让用户确认，再自己写 `providers.json`。

## 6. 备份 / 还原语义

| 场景 | 行为 |
|---|---|
| 首次接入时文件已存在 | 备份原件；之后无论接入多少次，账本始终指向**最初**那份备份 |
| 首次接入时文件不存在 | 由本模块创建；只要内容仍等于我们最后一次写入的字节，还原时直接删除 |
| 文件被外部改过再还原 | 只摘掉 `id = zcode2api` 的 provider，其它改动保留（`action = provider-removed`） |
| 备份文件丢失 | 跳过该文件并说明原因（`action = skipped`），不猜 |
| 未接入就还原 | 返回 `note = 尚未接入`，不产生副作用 |

## 7. API 契约

`GET /admin/api/agents`（需后台密钥）

```json
{
  "clients": [{
    "id": "magpie", "label": "magpie", "desc": "…", "protocol": "openai+anthropic",
    "installed": true, "configured": false, "running": false,
    "config_paths": ["…\\magpie\\providers.json", "…\\magpie\\settings.json"],
    "write_target": "…\\magpie\\providers.json",
    "applied": null, "external_change": false,
    "import_link": "magpie://import?…", "notes": ["…"]
  }],
  "models": ["GLM-5.3-Flash", "GLM-5.3"],
  "gateway": {"base_url": "http://127.0.0.1:3000", "base_url_v1": "…/v1", "key_set": false},
  "state_dir": "…\\data\\integrations", "backup_dir": "…\\data\\integrations\\backups",
  "home": "C:\\Users\\…"
}
```

`POST /admin/api/agents/apply` —— `{"client": "magpie", "base_url"?: "…", "models"?: ["…"]}`
→ `{"ok": true, "records": [{"path", "action", "backup", "sha256", "bytes"}], "backup_dir", "state_file"}`。
`base_url` 省略时按本次请求的 Host/Port 推导（非回环绑定回落 `127.0.0.1`）。
错误：`400` 客户端不支持 / 文件读不懂 / `models` 类型不对；`500` 写盘失败。

`POST /admin/api/agents/restore` —— `{"client": "magpie"}` →
`{"ok": true, "restored": [{"path", "action", "reason"?}]}`。

后台 API 一律经 `verify_admin_key`；看板不回显网关 Key 明文，只在「复制导入链接」里带着它
（链接本身就是凭据，等同后台会话权限）。

## 8. CLI（客户端不在网关机器上时）

```bash
python -m app.clients list     [--home DIR] [--state-dir DIR] [--json]
python -m app.clients status   [--home DIR] [--state-dir DIR] [--json]
python -m app.clients apply    --client magpie --base-url URL [--key KEY] [--models a,b]
                               [--dry-run] [--home DIR] [--state-dir DIR] [--json]
python -m app.clients restore  --client magpie [--home DIR] [--state-dir DIR] [--json]
```

- 退出码：`0` 成功 / `1` 配置错误（`AgentConfigError`）/ `2` 参数错误。
- `--dry-run` 走同一条 `plan()` 路径，打印的内容与真实写入逐字节一致。
- `--home` 也决定 magpie 目录（`ZCODE_AGENT_HOME`），`--state-dir` 决定账本与备份
  （`ZCODE_AGENT_STATE_DIR`）；`ZCODE_MAGPIE_DIR` 可直接指定 magpie 配置目录。

## 9. 多台电脑：GitHub 加密同步（推荐）/ 局域网直连

本机装好之后，「让别的电脑也能用」有两条路。看板「其他电脑」区块把两条都列出来。

### 9.1 GitHub 加密同步（magpie 的「同步与备份」）

magpie 自己支持把 providers（含 API Key）、settings、profiles、agents 的模型、library
加密后压进一个私有仓库；另一台电脑接同一个仓库 + 同一个口令，就拿到同一个
`zcode2api` provider（连网关 Key 一起）。

| 项 | 位置 / 说明 |
|---|---|
| 配置 | `~/.config/magpie/sync.json`（url / token / passphrase / keys·agents·library 开关） |
| 上次同步 | `~/.config/magpie/sync-state.json`（`last`、各分区的 sha256） |
| 仓库内容 | `<folder>/magpie/magpie.magpie-backup`（清单）+ `.parts/<n>`（分片，密文） |
| 前提 | 仓库必须**已存在且私有**；令牌需对它有 Contents 读+写；口令每台电脑一致 |

后台做的事（`app/clients.py`）：

- `magpie_sync_state()` — 读上面两个文件，回**掩码后的**令牌、口令是否已设、上次同步时间；
- `magpie_sync_now()` — 跑 `magpie github now`；
- `enable_magpie_sync(repo, token, passphrase, keys/agents/library)` — 跑
  `magpie github on github://owner/repo keys=yes …`，令牌与口令经 **stdin** 传入
  （`token\npassphrase\n`，magpie 的 `webdav_cli.go` 明确了管道语义），不进命令行、
  不进进程列表。

> 同步时要 `keys=yes`（默认就是）：只同步配置不同步 Key，另一台电脑拿到的 provider
> 是空 Key，被网关 401。看板上有一枚「含供应商 Key」徽章确认这一点。

### 9.2 局域网直连

对端 magpie 直接把本网关当 provider（不经同步、不经中转）：

```bash
magpie provider add zcode2api url=http://<本机IP>:3000/v1 \
  anthropic=http://<本机IP>:3000 key=<网关 Key> models=GLM-5.3-Flash,GLM-5.3
```

对端也可用 `magpie://import?…` 导入链接（magpie 只对回环/内网地址放行 `http`）。

### 9.3 相关 API

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/admin/api/agents` | 看板数据；`remote` 里带局域网地址、两条路径的命令、`remote.magpie.sync` 状态 |
| POST | `/admin/api/agents/gateway-key` | 生成新网关 Key（`sk-zcode-…`）并**就地刷新**已接入客户端 |
| POST | `/admin/api/agents/magpie/share` | 开 magpie 局域网共享 + 备 magpie 网关 Key（Remote magpie 路径） |
| POST | `/admin/api/agents/magpie/sync` | 开启 GitHub 同步（repo / token / passphrase，经 stdin 交给 magpie） |
| POST | `/admin/api/agents/magpie/sync-now` | 立刻同步一次 |

### 9.4 安全提示

- `sync.json` 里令牌与口令是**明文**（magpie 的设计，文件 0600）。建议用只对
  该仓库有 Contents 读写权限的**细粒度 token**，别用全 `repo` 权限的 classic token。
- 设置网关 Key 之后，所有 `/v1/*` 调用都要带 Key；看板的「生成网关 Key」会把已接入的
  客户端一并刷新，避免本机自己先 401。
- 同步出去的 provider 指向**局域网 IP**：换网段后会失效，需要重新接入一次（或改用
  Remote magpie / 内网域名）。

## 10. 怎么知道被换了模型（模型兜底）

网关对「这个模型全池都没额度」的请求会自动换模型（`GLM-5.3` 全空时退到 `GLM-5.3-Flash`
等，机制见 `01-architecture.md` §4.1）。对客户端接入方只有三条要记住：

1. **读响应头**：成功响应一定带 `x-zcode-model`，值是**实际服务**的模型；换过模型时额外带
   `x-zcode-model-requested`，值是客户端原本点的模型。失败响应（如 `503`）没有这两个头。
2. **要严格一致性就自己关**：请求带 `x-zcode-no-fallback: 1`（`x-zcode-model-fallback: off`
   同义），这次请求只试点的那个模型，不可用就照实报错。评测、回归对比、需要复现同一模型
   输出的场景用这个。
3. **别把模型名写死进提示词或缓存键**：兜底只换「这一次由谁执行」，不改 `system`、工具定义
   与消息体；同一段对话换过模型后，实际执行模型会变。

几个容易踩的点：

- 写客户端配置（如 magpie 的 provider）时，`models` 列的是「可以点的模型」，与当前额度无关：
  某个模型今天不可用，不代表它被下架了。
- 客户端自己也有重试/换模型逻辑时，先看响应头再决定要不要换：网关已经兜过一次，客户端再
  往下换容易一路退到最弱的模型。
- 后台「请求监控」里有同一件事的台账口径：「模型」= 实际服务，「请求模型」= 客户端点的原名，
  统计卡「换模型」就是两者不同的请求数。

## 11. 新增一款客户端

1. 写 `_plan_xxx(paths, ctx)`：读现有配置文本 → 合并本网关那一条 → 返回 `{path: 新全文}`。
   文本级 upsert（TOML/YAML/env）要注意保留注释与顺序；JSON 用 `json.loads` + 有序回写。
2. 写 `_strip_xxx(text)`：从全文里摘掉本网关那一条（供外部改动后的保守还原）。
3. 在 `CLIENTS` 里登记（`paths` / `probe` / `plan` / `strip` / 可选 `running`、`link`、`notes`）。
4. 在 `tests/unit/test_clients.py` 补往返用例（新建 / 保留他人配置 / 幂等 / 坏文件拒绝 /
   还原 / 外部改动），在 `tests/integration/test_agents_api.py` 补一条 API 用例。
5. 看板与 CLI 自动带出新客户端，无需改路由或前端。

## 12. 测试

```bash
python -m pytest tests/unit/test_clients.py        # 41 条：路径口径 / 合并 / 备份 / 还原 / CLI
python -m pytest tests/integration/test_agents_api.py   # 10 条：鉴权 / 看板 / 接入 / 还原 / 拒绝
python -m ruff check app tests
```

用例全部跑在 `tmp_path` 里（`ZCODE_AGENT_HOME` / `ZCODE_MAGPIE_DIR` /
`ZCODE_AGENT_STATE_DIR`），不会读到开发机上真实的 `~/.config/magpie`。

