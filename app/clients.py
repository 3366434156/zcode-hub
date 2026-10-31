"""客户端一键接入 —— 把本网关作为 provider 写进 AI 客户端的配置。

设计对齐 workbuddy2api-hub 的 `wb_agents.py`（同一套语义，换成 Python/FastAPI 版）：

1. **纯标准库**：只用 `json / os / re / shutil / hashlib / subprocess / tempfile`，
   不引第三方解析器（网关是绿色工具，装依赖越少越好）。
2. **非破坏性编辑**：只认「本网关自己那一条」provider（`id = zcode2api`），文件里
   其它 provider、routing group、search、order 等字段原样保留；写回沿用 magpie
   自身的两空格缩进风格，不重排用户数据。
3. **零风险写入**：写前备份（首次原件永久锁定，之后无论接几次都还原到首次接入
   前的字节）、临时文件 + `os.replace` 原子落盘、`0600` 权限（文件里带网关
   API Key）。写入中途异常会把已写的文件按原字节回滚。
4. **外部修改感知**：状态账本记下每次写入的 SHA-256；文件被用户或客户端自己改过
   时，还原只摘掉本网关那一条 provider，绝不整体覆盖用户的改动。

当前支持：

* `magpie`（https://github.com/yetone/magpie）—— 菜单栏里的「每个 agent 的模型
  都在一处」：它自己是个本地网关（127.0.0.1:3425），把 provider 交给 Claude Code /
  Codex / Gemini CLI / DSH / ZCode 等 agent 用。接入这里之后，magpie 的模型列表
  里会多出 `zcode2api/glm-5.3-flash` 这类条目，等于让所有 agent 都能白嫖本网关
  背后的 ZCode 账号池。

新增一款客户端见 `docs/development/08-client-integration.md`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from . import constants, settings

# 本网关在客户端里登记的 provider id（小写、无空格，客户端常拿它拼模型名）
PROVIDER_ID = "zcode2api"
PROVIDER_NAME = "zcode2api"

# 备份轮转：每个原文件名最多留这么多份
BACKUP_KEEP = 10
# 本模块写出的文件都带网关 API Key，统一 0600（客户端已有更严权限时保留原值）
WRITE_MODE = 0o600
# 只由本模块管理的 provider 字段：其余字段（proxy / headers / routing …）留给用户
MANAGED_KEYS = ("id", "name", "key", "chat", "anthropic", "models")

_LOCK = threading.RLock()
_HTTP_RE = re.compile(r"^https?://[^\s/]+", re.IGNORECASE)
# 平台判定做成模块级常量（测试可 monkeypatch，不去改 os.name —— pathlib 会跟着变）
_IS_WINDOWS = os.name == "nt"


class AgentConfigError(Exception):
    """客户端配置无法安全写入/还原（给后台与 CLI 的统一错误类型）。"""


# ── 目录解析 ─────────────────────────────────────────────────────────────────
def _env_path(name: str) -> Path | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def home_dir() -> Path:
    """用户主目录；`ZCODE_AGENT_HOME` 可覆盖（测试与远程 CLI 用）。"""
    override = _env_path("ZCODE_AGENT_HOME")
    return override if override else Path.home()


def state_dir() -> Path:
    """状态账本与备份根目录；默认 `<DATA_DIR>/integrations`。"""
    override = _env_path("ZCODE_AGENT_STATE_DIR")
    return override if override else Path(settings.DATA_DIR) / "integrations"


def backups_root() -> Path:
    return state_dir() / "backups"


def state_path() -> Path:
    return state_dir() / "state.json"


def _rooted(raw: str) -> bool:
    """绝对路径（Windows 上 `\\x` / `/x` 这种「当前盘根」也算）。"""
    if not raw:
        return False
    return os.path.isabs(raw) or (_IS_WINDOWS and raw[:1] in ("\\", "/"))


def user_config_dir(home: Path | None = None) -> Path:
    """XDG 风格配置根：`$XDG_CONFIG_HOME`（绝对路径）否则 `~/.config`。

    注意：magpie **不用** Go 的 `os.UserConfigDir()`。它源码里的
    `appdir.Config()` 在三大平台都走 `~/.config/magpie`（Windows 也不走
    `%APPDATA%`），目录刻意保持扁平，备份/同步/Docker 挂载都按文件名找。
    实测（2026-10-26，Windows 11）magpie 的配置文件确实落在
    `C:\\Users\\<user>\\.config\\magpie`。
    """
    home = home_dir() if home is None else home
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    if _rooted(xdg):
        return Path(xdg)
    return home / ".config"


# ── magpie 定位 ─────────────────────────────────────────────────────────────
def magpie_dir() -> Path:
    """magpie 的配置目录；`ZCODE_MAGPIE_DIR` 可覆盖。"""
    override = _env_path("ZCODE_MAGPIE_DIR")
    if override:
        return override
    portable = magpie_portable_dir()
    if portable is not None:
        return portable
    return user_config_dir() / "magpie"


def magpie_portable_dir() -> Path | None:
    """便携模式：magpie 把数据放在可执行文件旁边的 `data/`（或 `.portable` 标记）。

    复刻 `appdir.Portable()` 的判定要点：旁边那个目录里已经有 magpie 自己的文件；
    空的 `data/` 不算（否则会把压缩包解到 Downloads 的用户误导进便携模式）。
    """
    for exe in _magpie_exe_candidates():
        try:
            if not exe.is_file():
                continue
        except OSError:
            continue
        base = exe.parent
        candidates = [base / "data"]
        if (base / ".portable").is_file():
            candidates.append(base)
        for directory in candidates:
            if (directory / "providers.json").is_file() or (directory / "settings.json").is_file():
                return directory
    return None


def magpie_paths() -> dict[str, Path]:
    root = magpie_dir()
    return {
        "providers": root / "providers.json",
        "settings": root / "settings.json",
        "caller_keys": root / "caller-keys.json",
    }


def _magpie_exe_candidates() -> list[Path]:
    out: list[Path] = []
    for var in ("LOCALAPPDATA", "APPDATA"):
        base = (os.environ.get(var) or "").strip()
        if not base:
            continue
        out += [
            Path(base) / "Programs" / "magpie" / "magpie.exe",
            Path(base) / "Programs" / "Magpie" / "magpie.exe",
            Path(base) / "magpie" / "magpie.exe",
        ]
    out.append(home_dir() / ".local" / "bin" / "magpie")
    out.append(Path("/Applications/Magpie.app/Contents/MacOS/magpie"))
    return out


def magpie_installed() -> bool:
    """三重探测：配置目录 / 配置文件 / 可执行文件（满足其一即视为已安装）。"""
    root = magpie_dir()
    if root.is_dir() or (root / "providers.json").is_file():
        return True
    if shutil.which("magpie"):
        return True
    return any(cand.is_file() for cand in _magpie_exe_candidates())


def _process_running(image: str) -> bool | None:
    """当前是否有该进程在跑；探测本身失败返回 None（不代表没装）。"""
    try:
        if _IS_WINDOWS:
            proc = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                capture_output=True, text=True, timeout=8, check=False,
            )
            return image.lower() in (proc.stdout or "").lower()
        proc = subprocess.run(["pgrep", "-f", image], capture_output=True,
                              text=True, timeout=8, check=False)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


# ── 文本/字节工具 ───────────────────────────────────────────────────────────
def _sha256_bytes(data: bytes | None) -> str:
    return hashlib.sha256(data or b"").hexdigest() if data is not None else ""


def _read_bytes(path: Path) -> bytes | None:
    """读文件原始字节；不存在返回 None，其它 IO 错误直接报。"""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise AgentConfigError(f"读取 {path} 失败: {err}") from None


def _read_text(path: Path) -> str:
    data = _read_bytes(path)
    if data is None:
        return ""
    return data.decode("utf-8", "replace")


def _target_mode(path: Path) -> int:
    """已有文件权限更严（如用户手改 0400）就保留，否则收紧到 0600。"""
    try:
        current = path.stat().st_mode & 0o777
    except OSError:
        return WRITE_MODE
    return current if current and current < WRITE_MODE else WRITE_MODE


def _atomic_write(path: Path, data: bytes, mode: int = WRITE_MODE) -> None:
    """同目录临时文件 + `os.replace`：任何时刻读到的都是旧版或新版，不会是半截。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── 备份与状态账本 ───────────────────────────────────────────────────────────
def _backup_dir(client_id: str) -> Path:
    return backups_root() / client_id


def _backup_file(client_id: str, path: Path, original: bytes) -> str:
    """备份原始字节，返回备份文件名（相对该客户端的备份目录）。"""
    directory = _backup_dir(client_id)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{path.name}"
    if (directory / name).exists():
        name = f"{stamp}-{int(time.time() * 1000) % 1000:03d}-{path.name}"
    _atomic_write(directory / name, original, WRITE_MODE)
    _prune_backups(directory, path.name)
    return name


def _prune_backups(directory: Path, filename: str) -> None:
    try:
        candidates = sorted(
            (p for p in directory.iterdir() if p.is_file() and p.name.endswith(f"-{filename}")),
            key=lambda p: p.name,
        )
    except OSError:
        return
    for stale in candidates[:-BACKUP_KEEP]:
        try:
            stale.unlink()
        except OSError:
            pass


def _load_state() -> dict[str, Any]:
    raw = _read_text(state_path())
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 状态账本坏了不该阻断接入：备份还在，退化成空账本
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    _atomic_write(state_path(), (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── 网关地址 ────────────────────────────────────────────────────────────────
def gateway_endpoints(base_url: str) -> tuple[str, str]:
    """把用户/请求带来的地址规范成 (Anthropic 根地址, OpenAI `/v1` 地址)。"""
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        raise AgentConfigError("缺少网关地址")
    root = raw[:-3].rstrip("/") if raw.endswith("/v1") else raw
    if not _HTTP_RE.match(root):
        raise AgentConfigError(f"网关地址不是 http(s) URL: {base_url}")
    return root, f"{root}/v1"


def default_base_url() -> str:
    host = (settings.HOST or "").strip()
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{settings.PORT}"


def default_models() -> list[str]:
    return list(constants.AVAILABLE_MODELS)


# ── magpie：providers.json 读写 ─────────────────────────────────────────────
def magpie_provider(*, base_url: str, api_key: str = "", models: list[str] | None = None) -> dict[str, Any]:
    """本网关在 magpie 里的 provider 条目。

    同时写 OpenAI Chat（`…/v1`）与 Anthropic Messages（根地址）两个入口：magpie
    按 agent 的原生协议挑一个，Claude Code 走 Anthropic、Codex 走 OpenAI，两条都
    指向本网关同一套账号池。本地回环地址 magpie 允许不带 Key（`keyOptional`）。
    """
    root, v1 = gateway_endpoints(base_url)
    return {
        "id": PROVIDER_ID,
        "name": PROVIDER_NAME,
        "key": (api_key or "").strip(),
        "chat": v1,
        "anthropic": root,
        "models": list(models or default_models()),
    }


def _load_magpie_file(text: str) -> dict[str, Any]:
    """解析 providers.json；读不懂就报错，绝不覆盖（magpie 自己的 `ErrUnreadable`）。"""
    raw = text.lstrip("\ufeff")
    if not raw.strip():
        return {"providers": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise AgentConfigError(f"providers.json 不是合法 JSON，已拒绝改写（{err}）") from None
    if not isinstance(data, dict):
        raise AgentConfigError("providers.json 顶层不是对象，已拒绝改写")
    providers = data.get("providers")
    if providers is None:
        data["providers"] = []
    elif not isinstance(providers, list):
        raise AgentConfigError("providers.json 的 providers 不是数组，已拒绝改写")
    return data


def _upsert_magpie_provider(data: dict[str, Any], entry: dict[str, Any]) -> str:
    """按 id 就地更新，否则追加；返回 "updated" / "added"。"""
    providers = data["providers"]
    for item in providers:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").strip().lower() == PROVIDER_ID:
            for key in MANAGED_KEYS:
                item[key] = entry[key]
            return "updated"
    providers.append(dict(entry))
    return "added"


def _strip_magpie_provider(text: str) -> tuple[str, bool]:
    """只摘掉本网关那条 provider（外部改过文件后的安全还原）。"""
    data = _load_magpie_file(text)
    providers = data["providers"]
    kept = [p for p in providers
            if not (isinstance(p, dict) and str(p.get("id") or "").strip().lower() == PROVIDER_ID)]
    if len(kept) == len(providers):
        return text, False
    data["providers"] = kept
    return _dump_magpie(data), True


def _dump_magpie(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _plan_magpie(paths: dict[str, Path], ctx: dict[str, Any]) -> dict[Path, str]:
    target = paths["providers"]
    entry = magpie_provider(base_url=ctx["base_url"], api_key=ctx["api_key"], models=ctx["models"])
    data = _load_magpie_file(_read_text(target))
    _upsert_magpie_provider(data, entry)
    return {target: _dump_magpie(data)}


def magpie_import_link(*, base_url: str, api_key: str = "", models: list[str] | None = None) -> str:
    """magpie 官方的「Add to magpie」导入链接（`magpie://import?…`）。

    链接只是把 provider 递给正在运行的 magpie，由用户在它自己的弹窗里确认；真正
    落盘与校验都在 magpie 侧完成。首次使用前需要启动过一次 magpie（它负责注册
    `magpie://` 协议）。
    """
    root, v1 = gateway_endpoints(base_url)
    params: list[tuple[str, str]] = [
        ("name", PROVIDER_NAME),
        ("id", PROVIDER_ID),
        ("chat", v1),
        ("anthropic", root),
    ]
    if api_key:
        params.append(("key", api_key))
    if models:
        params.append(("models", ",".join(models)))
    return "magpie://import?" + urlencode(params, quote_via=quote)


# ── 局域网共享 / 其他电脑（magpie 的 Remote magpie）────────────────────────────
# magpie 原生支持「另一台电脑上的 magpie」（Add sheet → Remote magpie）。它靠三件事：
#   1. 本机 magpie 打开 Settings → Network and sharing → Share on local network
#      （settings.json 里是 "lan": true）；
#   2. 本机 magpie 有一个网关 Key（caller-keys.json 里的 sk-magpie-…，可限量）；
#   3. 对端执行 `magpie provider add remote-magpie <key> url=http://<本机>:3425 id=<名字>`。
# 前两件在本机能一键完成，第三件只能在另一台电脑上跑，所以这里生成可复制的命令。
MAGPIE_GATEWAY_PORT = 3425
_DEFAULT_SHARE_KEY_NAME = "zcode-hub"


def lan_ipv4() -> list[str]:
    """本机局域网 IPv4（UDP「连一下」取出口地址，不发包；再并上主机名解析结果）。"""
    found: list[str] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            found.append(str(sock.getsockname()[0]))
        finally:
            sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(str(info[4][0]))
    except OSError:
        pass
    out: list[str] = []
    for ip in found:
        if not ip or ip.startswith(("127.", "169.254.")) or ip in out:
            continue
        out.append(ip)
    return out


def magpie_cli_path() -> str | None:
    """magpie 命令行可执行文件（PATH 优先，其次常见安装位置）。"""
    found = shutil.which("magpie")
    if found:
        return found
    for cand in _magpie_exe_candidates():
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


def _read_json_document(path: Path, what: str) -> dict:
    """读一个 JSON 对象文件；缺失/空白视为空对象，坏 JSON 直接报（不覆盖）。"""
    raw = _read_text(path).lstrip("\ufeff")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise AgentConfigError(f"{what} 不是合法 JSON，已拒绝改写（{err}）") from None
    if not isinstance(data, dict):
        raise AgentConfigError(f"{what} 顶层不是对象，已拒绝改写")
    return data


def _read_caller_keys() -> list[dict[str, Any]]:
    """magpie 的网关 Key 账本（caller-keys.json，0600，明文 secret 在文件里）。"""
    raw = _read_text(magpie_paths()["caller_keys"]).lstrip("\ufeff")
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []  # 读不懂就不猜：只在「无可用 Key」时触发新建，不覆盖文件
    if isinstance(data, dict):
        data = data.get("keys") or []
    return [k for k in data if isinstance(k, dict)] if isinstance(data, list) else []


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return "••••"
    return f"{value[:16]}…{value[-6:]}"


def magpie_gateway_key(name: str = _DEFAULT_SHARE_KEY_NAME) -> dict[str, Any]:
    """取（或新建）一个 magpie 网关 Key，供其它电脑作为 Remote magpie 使用。

    同名且启用的 Key 直接复用（幂等）；没有就调 `magpie gateway-key add <name>`，
    该命令会把 `sk-magpie-…` 打到 stdout，magpie 自己负责文件锁与备份。
    """
    for key in _read_caller_keys():
        if key.get("name") == name and key.get("secret") and not key.get("off"):
            return {"id": str(key.get("id") or ""), "name": name,
                    "secret": str(key["secret"]), "created": False}
    cli = magpie_cli_path()
    if not cli:
        raise AgentConfigError(
            "找不到 magpie 命令行（magpie / magpie.exe）：请在 magpie 的 "
            "Gateway → Gateway keys 里手动新建一个 Key，再把命令里的 Key 换掉。"
        )
    try:
        proc = subprocess.run([cli, "gateway-key", "add", name],
                              capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as err:
        raise AgentConfigError(f"调用 magpie gateway-key add 失败: {err}") from None
    secret = ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("sk-magpie-"):
            secret = line
            break
    if not secret:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        raise AgentConfigError(f"magpie gateway-key add 未返回 Key: {detail or '无输出'}")
    key_id = next((str(k.get("id") or "") for k in _read_caller_keys()
                   if k.get("secret") == secret), "")
    return {"id": key_id, "name": name, "secret": secret, "created": True}


def magpie_sharing_enabled() -> bool:
    """magpie 是否打开了「Share on local network」（settings.json 的 lan 字段）。"""
    try:
        return _read_json_document(magpie_paths()["settings"], "settings.json").get("lan") is True
    except AgentConfigError:
        return False


def enable_magpie_sharing() -> dict[str, Any]:
    """打开 magpie 的局域网共享（只补 `lan: true`，其它设置原样保留，写前备份）。"""
    path = magpie_paths()["settings"]
    with _LOCK:
        original = _read_bytes(path)
        data = _read_json_document(path, "settings.json")
        if data.get("lan") is True:
            return {"changed": False, "path": str(path)}
        data["lan"] = True
        if original is not None:
            _backup_file("magpie-share", path, original)
        payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _atomic_write(path, payload, _target_mode(path))
    return {"changed": True, "path": str(path), "backup_dir": str(_backup_dir("magpie-share"))}


def magpie_lan_reachable(port: int = MAGPIE_GATEWAY_PORT) -> bool | None:
    """从本机经局域网地址能否连上 magpie 网关。

    False 通常意味着：刚打开共享但 magpie 还没重绑监听（`Relisten`），重启 magpie 即生效。
    """
    ips = lan_ipv4()
    if not ips:
        return None
    for ip in ips:
        try:
            with socket.create_connection((ip, port), timeout=1.0):
                return True
        except OSError:
            continue
    return False


def magpie_share_state(*, include_secret: bool = True) -> dict[str, Any]:
    """本机 magpie 作为共享端的现状（共享开关 / 网关 Key / 局域网可达）。"""
    keys = []
    for key in _read_caller_keys():
        secret = str(key.get("secret") or "")
        keys.append({
            "id": str(key.get("id") or ""),
            "name": str(key.get("name") or ""),
            "enabled": not key.get("off"),
            "default_lan": bool(key.get("lan")),
            "masked": _mask_secret(secret),
            **({"secret": secret} if include_secret else {}),
        })
    ips = lan_ipv4()
    return {
        "installed": magpie_installed(),
        "running": _process_running("magpie.exe" if _IS_WINDOWS else "magpie"),
        "sharing": magpie_sharing_enabled(),
        "port": MAGPIE_GATEWAY_PORT,
        "lan_ips": ips,
        "urls": [f"http://{ip}:{MAGPIE_GATEWAY_PORT}" for ip in ips],
        "reachable": magpie_lan_reachable(),
        "keys": keys,
        "cli": magpie_cli_path() or "",
    }


def remote_magpie_command(*, secret: str, url: str, host_id: str) -> str:
    """对端（另一台电脑）要执行的一行命令。"""
    return f"magpie provider add remote-magpie {secret} url={url} id={host_id}"


def direct_provider_command(*, base_url: str, api_key: str, models: list[str] | None = None) -> str:
    """对端不经 Remote magpie、直接连本网关时要执行的一行命令。"""
    root, v1 = gateway_endpoints(base_url)
    parts = [f"url={v1}", f"anthropic={root}"]
    if api_key:
        parts.insert(0, f"key={api_key}")
    if models:
        parts.append(f"models={','.join(models)}")
    return f"magpie provider add {PROVIDER_ID} " + " ".join(parts)


def _share_host_id() -> str:
    """对端给这台机器起的 id（`zcode-hub-<主机名>`，如 zcode-hub-wlh）。

    magpie 的 remote provider id 会出现在对端模型名里（`<id>/<provider>/<model>`），
    带上主机名便于区分多台机器。
    """
    raw = (os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return f"zcode-hub-{slug}" if slug else "zcode-hub"


def _preferred_share_key(magpie: dict[str, Any]) -> dict[str, Any] | None:
    """挑一个对端可用的网关 Key：优先我们自己建的那个，其次 magpie 自己的 LAN Key。"""
    keys = [k for k in magpie.get("keys") or [] if k.get("enabled") and k.get("secret")]
    if not keys:
        return None
    for key in keys:
        if key.get("name") == _DEFAULT_SHARE_KEY_NAME:
            return key
    lan_id = ""
    try:
        lan_id = str(_read_json_document(magpie_paths()["settings"], "settings.json")
                     .get("lanKeyId") or "")
    except AgentConfigError:
        lan_id = ""
    for key in keys:
        if lan_id and key.get("id") == lan_id:
            return key
    for key in keys:
        if key.get("default_lan"):
            return key
    return keys[0]


def remote_share_plan(*, base_url: str | None = None, api_key: str = "",
                      models: list[str] | None = None) -> dict[str, Any]:
    """「让其他电脑也能用」的完整现状 + 可复制命令（看板与 API 共用）。"""
    root, v1 = gateway_endpoints(base_url or default_base_url())
    models = list(models or default_models())
    ips = lan_ipv4()
    gateway_urls = [f"http://{ip}:{settings.PORT}" for ip in ips]
    magpie = magpie_share_state()
    sync_state = magpie_sync_state()
    enabled_key = _preferred_share_key(magpie)
    host_id = _share_host_id()
    lan_gateway = gateway_urls[0] if gateway_urls else root
    remote_cmd = ""
    if enabled_key and magpie["urls"]:
        remote_cmd = remote_magpie_command(secret=enabled_key["secret"],
                                           url=magpie["urls"][0], host_id=host_id)
    return {
        "lan_ips": ips,
        "host_id": host_id,
        "gateway": {
            "loopback": root,
            "loopback_v1": v1,
            "urls": gateway_urls,
            "lan_url": lan_gateway,
            "key_set": bool((api_key or "").strip()),
            "key_masked": _mask_secret((api_key or "").strip()),
            # 任何 OpenAI / Anthropic 兼容客户端只要「粘三样」：地址、Key、模型名
            "key": (api_key or "").strip(),
            "anthropic_base": lan_gateway,
            "openai_base": f"{lan_gateway}/v1",
            "paste": {
                "anthropic_base": lan_gateway,
                "openai_base": f"{lan_gateway}/v1",
                "key": (api_key or "").strip(),
                "key_set": bool((api_key or "").strip()),
                "key_masked": _mask_secret((api_key or "").strip()),
                "models": models,
                "model_hint": models[0] if models else "",
            },
            "direct_command": direct_provider_command(base_url=lan_gateway, api_key=api_key,
                                                      models=models),
            "cli_command": (f"python -m app.clients apply --client magpie "
                            f"--base-url {lan_gateway} --key <网关 Key>"),
            "import_link": magpie_import_link(base_url=lan_gateway, api_key=api_key,
                                              models=models),
        },
        "magpie": {
            **magpie,
            "host_id": host_id,
            "remote_command": remote_cmd,
            "remote_key_name": (enabled_key or {}).get("name", ""),
            "remote_url": magpie["urls"][0] if magpie["urls"] else "",
            "sync": sync_state,
            "other_machine_command": (f"magpie github on {sync_state['url']}"
                                      if sync_state.get("enabled") else ""),
        },
    }


# ── GitHub 加密同步（magpie 的「同步与备份」）──────────────────────────────────
# 多台电脑最省事的一条路：本机 magpie 把 providers（含 API Key）、settings、
# profiles、agents 的模型、library 加密后压进一个私有 GitHub 仓库；另一台电脑
# 用同一仓库 + 同一口令接上，就拿到了同一个 zcode2api provider（含网关 Key）。
# 配置在 magpie 侧：sync.json（url/token/passphrase/开关）与 sync-state.json（上次同步）。
def magpie_sync_paths() -> dict[str, Path]:
    root = magpie_dir()
    return {"config": root / "sync.json", "state": root / "sync-state.json"}


def magpie_sync_state() -> dict[str, Any]:
    """magpie GitHub/WebDAV/S3 同步的现状（token 只回掩码，口令只回是否已设）。"""
    paths = magpie_sync_paths()
    try:
        config = _read_json_document(paths["config"], "sync.json")
    except AgentConfigError as err:
        return {"enabled": False, "error": str(err), "url": "", "last": ""}
    try:
        state = _read_json_document(paths["state"], "sync-state.json")
    except AgentConfigError:
        state = {}
    url = str(config.get("url") or "")
    kind = url.split(":", 1)[0] if "://" in url else ""
    return {
        "enabled": bool(url),
        "url": url,
        "kind": kind,
        "repo": url[len("github://"):] if url.startswith("github://") else "",
        "last": str(state.get("last") or ""),
        "keys": bool(config.get("keys")),
        "agents": bool(config.get("agents")),
        "library": bool(config.get("library")),
        "token_masked": _mask_secret(str(config.get("password") or "")),
        "token_set": bool(config.get("password")),
        "passphrase_set": bool(config.get("passphrase")),
    }


def _sync_cli(args: list[str], *, stdin: str | None = None, timeout: int = 180) -> dict[str, Any]:
    cli = magpie_cli_path()
    if not cli:
        raise AgentConfigError("找不到 magpie 命令行（magpie / magpie.exe）")
    try:
        proc = subprocess.run([cli, *args], capture_output=True, text=True,
                              timeout=timeout, check=False, input=stdin)
    except (OSError, subprocess.SubprocessError) as err:
        raise AgentConfigError(f"magpie {' '.join(args)} 执行失败: {err}") from None
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": output[-1500:]}


def magpie_sync_now() -> dict[str, Any]:
    """立刻同步一次（推送本机改动 / 拉取仓库改动）。"""
    result = _sync_cli(["github", "now"], timeout=240)
    return {**result, "state": magpie_sync_state()}


def enable_magpie_sync(*, repo: str, token: str, passphrase: str,
                       keys: bool = True, agents: bool = True,
                       library: bool = True) -> dict[str, Any]:
    """开启 GitHub 同步（token/口令经 stdin 传入，不进命令行与进程列表）。"""
    repo = (repo or "").strip()
    if repo.startswith("github://"):
        repo = repo[len("github://"):]
    if not repo or "/" not in repo:
        raise AgentConfigError("仓库要写成 owner/repo（例如 me/magpie-sync）")
    if not (token or "").strip():
        raise AgentConfigError("缺少 GitHub 令牌（对该私有仓库有 Contents 读写权限）")
    if not (passphrase or "").strip():
        raise AgentConfigError("缺少同步口令（每台电脑必须一致）")
    flags = [f"keys={'yes' if keys else 'no'}",
             f"agents={'yes' if agents else 'no'}",
             f"library={'yes' if library else 'no'}"]
    result = _sync_cli(["github", "on", f"github://{repo}", *flags],
                       stdin=f"{token.strip()}\n{passphrase.strip()}\n", timeout=240)
    if not result["ok"]:
        raise AgentConfigError(result["output"] or "magpie github on 失败")
    return {**result, "state": magpie_sync_state()}


def _lan_gateway_url(port: int | None = None) -> str:
    """本网关的局域网地址（没有局域网地址时退回 127.0.0.1）。"""
    ips = lan_ipv4()
    return f"http://{ips[0]}:{port or settings.PORT}" if ips else f"http://127.0.0.1:{port or settings.PORT}"


# ── 客户端注册表 ────────────────────────────────────────────────────────────
CLIENTS: dict[str, dict[str, Any]] = {
    "magpie": {
        "id": "magpie",
        "label": "magpie",
        "desc": "菜单栏里的模型中枢：接入后 magpie 的 Claude Code / Codex / Gemini CLI "
                "等 agent 都能选 zcode2api 的模型。写入 magpie 的 providers.json。",
        "protocol": "openai+anthropic",
        "provider_id": PROVIDER_ID,
        "paths": magpie_paths,
        "probe": magpie_installed,
        "plan": _plan_magpie,
        "running": lambda: _process_running("magpie.exe" if _IS_WINDOWS else "magpie"),
        "link": magpie_import_link,
        "strip": _strip_magpie_provider,
        "notes": [
            "接入后打开 magpie → Agents 页，给 Claude Code / Codex 选 zcode2api/glm-5.3-flash。",
            "magpie 正在运行时直接改 providers.json 也能生效（它每次读都重新读文件），"
            "但若同一秒内 magpie 自己也在写配置，改动可能被它覆盖：稳妥做法是先退出 magpie。",
            "magpie 首次启动后才注册 magpie:// 协议，导入链接在首次运行前点不开。",
        ],
    },
}


def configured_clients() -> list[str]:
    """已经接入过（账本里有记录）的客户端 id。"""
    state = _load_state()
    return [cid for cid in CLIENTS if state.get(cid)]


def client_ids() -> list[str]:
    return list(CLIENTS)


def get_client(client_id: str) -> dict[str, Any]:
    key = (client_id or "").strip().lower()
    client = CLIENTS.get(key)
    if client is None:
        raise AgentConfigError(f"不支持的客户端: {client_id}（支持: {', '.join(CLIENTS)}）")
    return client


# ── 生成计划（纯函数，dry-run 与真实写入同一条路径）────────────────────────────
def plan(
    client_id: str,
    *,
    base_url: str | None = None,
    api_key: str = "",
    models: list[str] | None = None,
) -> dict[Path, str]:
    client = get_client(client_id)
    ctx = {
        "base_url": base_url or default_base_url(),
        "api_key": (api_key or "").strip(),
        "models": list(models or default_models()),
    }
    return client["plan"](client["paths"](), ctx)


# ── 一键接入 ────────────────────────────────────────────────────────────────
def integrate(
    client_id: str,
    *,
    base_url: str | None = None,
    api_key: str = "",
    models: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把本网关写进客户端配置：备份 → 原子写入 → 记账。

    `dry_run=True` 只返回将要写入的内容，不备份、不落盘、不记账（与真实写入逐字节
    同源，因此不会漂移）。
    """
    client = get_client(client_id)
    files = plan(client_id, base_url=base_url, api_key=api_key, models=models)
    root, v1 = gateway_endpoints(base_url or default_base_url())
    report: dict[str, Any] = {
        "client": client["id"],
        "label": client["label"],
        "base_url": root,
        "base_url_v1": v1,
        "files": [str(p) for p in files],
        "dry_run": dry_run,
    }
    if dry_run:
        report["contents"] = {str(p): text for p, text in files.items()}
        return report

    with _LOCK:
        state = _load_state()
        previous = state.get(client["id"]) or {}
        first_backup = {
            str(rec.get("path")): rec.get("backup")
            for rec in previous.get("files") or [] if isinstance(rec, dict)
        }
        records: list[dict[str, Any]] = []
        originals: dict[Path, bytes | None] = {}
        written: list[Path] = []
        try:
            for path, text in files.items():
                payload = text.encode("utf-8")
                original = _read_bytes(path)
                originals[path] = original
                if original == payload:
                    # 已经是我们写的内容：不重复备份，只刷新记账
                    backup = first_backup.get(str(path), "")
                    action = "unchanged"
                else:
                    backup = first_backup.get(str(path)) or (
                        _backup_file(client["id"], path, original) if original is not None else ""
                    )
                    _atomic_write(path, payload, _target_mode(path))
                    written.append(path)
                    action = "updated" if original is not None else "created"
                records.append({
                    "path": str(path),
                    "action": action,
                    "existed": original is not None,
                    "backup": backup or "",
                    "sha256": _sha256_bytes(payload),
                    "bytes": len(payload),
                })
        except BaseException:
            # 两阶段写入的第二阶段出错：把已经写过的文件按原字节还回去
            for path in written:
                try:
                    if originals[path] is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_write(path, originals[path] or b"", _target_mode(path))
                except OSError:
                    pass
            raise

        state[client["id"]] = {
            "applied_at": _now(),
            "base_url": root,
            "base_url_v1": v1,
            "key_set": bool((api_key or "").strip()),
            "models": list(models or default_models()),
            "provider_id": client.get("provider_id", PROVIDER_ID),
            "files": records,
        }
        _save_state(state)

    report["files"] = [rec["path"] for rec in records]
    report["records"] = records
    report["backup_dir"] = str(_backup_dir(client["id"]))
    report["state_file"] = str(state_path())
    return report


# ── 一键还原 ────────────────────────────────────────────────────────────────
def restore(client_id: str) -> dict[str, Any]:
    """撤销接入。

    * 文件仍是我们上次写入的内容 → 还原到首次接入前的字节（首次是我们建的文件则删除）；
    * 文件被外部改过 → 只摘掉本网关那一条 provider，其余改动一律保留。
    """
    client = get_client(client_id)
    with _LOCK:
        state = _load_state()
        entry = state.get(client["id"])
        if not entry:
            return {"client": client["id"], "label": client["label"],
                    "restored": [], "note": "尚未接入，无需还原"}
        results: list[dict[str, Any]] = []
        for rec in entry.get("files") or []:
            if not isinstance(rec, dict):
                continue
            path = Path(str(rec.get("path") or ""))
            if not str(path):
                continue
            current = _read_bytes(path)
            if current is None:
                results.append({"path": str(path), "action": "missing",
                                "reason": "文件已不存在"})
                continue
            if _sha256_bytes(current) == rec.get("sha256"):
                if rec.get("existed") and rec.get("backup"):
                    backup = _backup_dir(client["id"]) / str(rec["backup"])
                    original = _read_bytes(backup)
                    if original is None:
                        results.append({"path": str(path), "action": "skipped",
                                        "reason": f"备份 {backup} 已丢失，保持原样"})
                        continue
                    _atomic_write(path, original, _target_mode(path))
                    results.append({"path": str(path), "action": "restored"})
                else:
                    path.unlink(missing_ok=True)
                    results.append({"path": str(path), "action": "deleted"})
                continue
            # 外部修改过：保守还原
            strip = client.get("strip")
            if strip is None:
                results.append({"path": str(path), "action": "skipped",
                                "reason": "文件已被外部修改，跳过"})
                continue
            try:
                new_text, removed = strip(current.decode("utf-8", "replace"))
            except AgentConfigError as err:
                results.append({"path": str(path), "action": "skipped", "reason": str(err)})
                continue
            if removed:
                _atomic_write(path, new_text.encode("utf-8"), _target_mode(path))
                results.append({"path": str(path), "action": "provider-removed",
                                "reason": "文件被外部修改过，只摘掉本网关的 provider"})
            else:
                results.append({"path": str(path), "action": "unchanged",
                                "reason": "文件里已没有本网关的 provider"})
        state.pop(client["id"], None)
        _save_state(state)
    return {"client": client["id"], "label": client["label"], "restored": results}


# ── 看板数据 ────────────────────────────────────────────────────────────────
def overview(
    *,
    base_url: str | None = None,
    api_key: str = "",
    models: list[str] | None = None,
) -> dict[str, Any]:
    """后台看板用：每个客户端的安装/接入状态 + 网关信息 + 导入链接。"""
    root, v1 = gateway_endpoints(base_url or default_base_url())
    models = list(models or default_models())
    state = _load_state()
    clients: list[dict[str, Any]] = []
    for client_id, client in CLIENTS.items():
        entry = state.get(client_id) or {}
        paths = client["paths"]()
        external = False
        for rec in entry.get("files") or []:
            if not isinstance(rec, dict):
                continue
            try:
                current = _read_bytes(Path(str(rec.get("path") or "")))
            except AgentConfigError:
                external = True
                break
            if _sha256_bytes(current) != rec.get("sha256"):
                external = True
                break
        try:
            installed = bool(client["probe"]())
        except OSError:
            installed = False
        running = None
        if client.get("running"):
            try:
                running = client["running"]()
            except OSError:
                running = None
        link = ""
        if client.get("link"):
            try:
                link = client["link"](base_url=root, api_key=api_key, models=models)
            except AgentConfigError:
                link = ""
        clients.append({
            "id": client_id,
            "label": client["label"],
            "desc": client["desc"],
            "protocol": client["protocol"],
            "installed": installed,
            "configured": bool(entry),
            "running": running,
            "config_paths": [str(p) for p in paths.values()],
            "write_target": str(paths.get("providers") or next(iter(paths.values()))),
            "applied": ({
                "at": entry.get("applied_at"),
                "base_url": entry.get("base_url"),
                "models": entry.get("models") or [],
                "files": len(entry.get("files") or []),
                "key_set": bool(entry.get("key_set")),
            } if entry else None),
            "external_change": external,
            "import_link": link,
            "notes": list(client.get("notes") or []),
        })
    return {
        "clients": clients,
        "models": models,
        "gateway": {"base_url": root, "base_url_v1": v1, "key_set": bool((api_key or "").strip())},
        "remote": remote_share_plan(base_url=root, api_key=api_key, models=models),
        "state_dir": str(state_dir()),
        "backup_dir": str(backups_root()),
        "home": str(home_dir()),
    }


# ── 命令行（客户端不在网关机器上时用）──────────────────────────────────────
def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    # 共享父解析器：--home/--state-dir/--json 写在子命令前后都认（SUPPRESS 让
    # 子解析器不覆盖顶层已解析的值，workbuddy2api 的 CLI 也是这个套路）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS, help="用户主目录（默认当前用户）")
    common.add_argument("--state-dir", default=argparse.SUPPRESS,
                        help="状态与备份目录（默认 <DATA_DIR>/integrations）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="机器可读输出")

    parser = argparse.ArgumentParser(
        prog="python -m app.clients",
        description="把 zcode2api 网关一键接入 AI 客户端（magpie 等）。",
    )
    parser.add_argument("--home", help="用户主目录（默认当前用户）")
    parser.add_argument("--state-dir", help="状态与备份目录（默认 <DATA_DIR>/integrations）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出支持的客户端", parents=[common])
    sub.add_parser("status", help="显示安装/接入状态", parents=[common])

    apply_cmd = sub.add_parser("apply", help="一键接入", parents=[common])
    apply_cmd.add_argument("--client", required=True, help="客户端 id，如 magpie")
    apply_cmd.add_argument("--base-url", required=True, help="网关地址，如 http://127.0.0.1:3000")
    apply_cmd.add_argument("--key", default="", help="网关 API Key（未启用则省略）")
    apply_cmd.add_argument("--models", default="", help="模型列表，逗号分隔")
    apply_cmd.add_argument("--dry-run", action="store_true", help="只打印将写入的内容")

    restore_cmd = sub.add_parser("restore", help="撤销接入", parents=[common])
    restore_cmd.add_argument("--client", required=True, help="客户端 id，如 magpie")

    args = parser.parse_args(argv)
    if getattr(args, "home", None):
        os.environ["ZCODE_AGENT_HOME"] = args.home
    if getattr(args, "state_dir", None):
        os.environ["ZCODE_AGENT_STATE_DIR"] = args.state_dir
    json_out = bool(getattr(args, "json", False))

    try:
        if args.command == "list":
            payload = []
            for cid, client in CLIENTS.items():
                try:
                    installed = bool(client["probe"]())
                except OSError:
                    installed = False
                payload.append({"id": cid, "label": client["label"], "desc": client["desc"],
                                "protocol": client["protocol"], "installed": installed})
            if json_out:
                _print_json(payload)
            else:
                for row in payload:
                    flag = "yes" if row["installed"] else "no"
                    print(f"{row['id']:10s} {row['label']:10s} installed={flag:3s} {row['desc']}")
            return 0
        if args.command == "status":
            data = overview(base_url=default_base_url())
            if json_out:
                _print_json(data)
                return 0
            print(f"网关   {data['gateway']['base_url']}  key={'set' if data['gateway']['key_set'] else 'none'}")
            print(f"模型   {', '.join(data['models'])}")
            print(f"状态   {data['state_dir']}")
            for client in data["clients"]:
                print(f"\n[{client['id']}] {client['label']}  installed={client['installed']} "
                      f"configured={client['configured']} external={client['external_change']}")
                print(f"  写入   {client['write_target']}")
                if client["applied"]:
                    print(f"  接入   {client['applied']['at']} → {client['applied']['base_url']}")
            return 0
        if args.command == "apply":
            models = [m.strip() for m in (args.models or "").split(",") if m.strip()]
            report = integrate(args.client, base_url=args.base_url, api_key=args.key,
                               models=models or None, dry_run=args.dry_run)
            if json_out:
                _print_json(report)
            else:
                for path in report["files"]:
                    print(f"写入 {path}")
                if args.dry_run:
                    for path, text in (report.get("contents") or {}).items():
                        print(f"\n--- {path} ---\n{text}")
                else:
                    print(f"备份 {report.get('backup_dir')}")
            return 0
        if args.command == "restore":
            result = restore(args.client)
            if json_out:
                _print_json(result)
            else:
                for rec in result["restored"]:
                    print(f"{rec['action']:18s} {rec['path']} {rec.get('reason', '')}")
                if result.get("note"):
                    print(result["note"])
            return 0
    except AgentConfigError as err:
        print(f"错误: {err}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())



