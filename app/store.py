"""账号与设置的持久化存储（SQLite）。

数据保存在项目本目录下的 data/accounts.db，采用 WAL 模式，
与 grok2api 的本地 (local) 账号后端保持一致。

运行期账号对象常驻内存（保证轮询游标与状态实时性），
每次变更同步落库；进程启动时从 SQLite 读取快照。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing

from . import settings
from .models import PROVIDERS, Account, Status

_TBL = "accounts"
_META = "meta"


class Store:
    """线程安全的账号 / 设置存储，含轮询游标。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accounts: dict[str, list[Account]] = {p: [] for p in PROVIDERS}
        self._settings: dict = {}
        self._rotation: dict[str, int] = {p: 0 for p in PROVIDERS}
        # 全局粘号：provider -> (账号 id, 记住的时刻)。只放内存：重启后第一次
        # 请求重新学一遍的代价，远小于把「上次用谁」持久化带来的状态一致性负担。
        self._sticky_id: dict[str, str] = {}
        self._sticky_at: dict[str, float] = {}
        self._sticky_ttl = settings.AFFINITY_TTL
        self._init_db()
        self._load()

    # ── SQLite 基础 ──────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {_META} (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS {_TBL} (
                    id          TEXT PRIMARY KEY,
                    provider    TEXT NOT NULL,
                    name        TEXT,
                    mode        TEXT,
                    status      TEXT,
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    created_at  REAL,
                    data        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_acc_provider ON {_TBL} (provider);
                CREATE INDEX IF NOT EXISTS idx_acc_status   ON {_TBL} (status);
                """
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('admin_key', ?)",
                (settings.DEFAULT_ADMIN_KEY,),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('gateway_key', '')"
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('quota_refresh_interval', ?)",
                (str(settings.QUOTA_REFRESH_INTERVAL),),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('account_concurrency', ?)",
                (str(settings.ACCOUNT_CONCURRENCY),),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('claim_round_interval', ?)",
                (str(settings.CLAIM_ROUND_INTERVAL),),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('model_fallback_enabled', ?)",
                ("1" if settings.MODEL_FALLBACK_ENABLED else "0",),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('model_fallback_chain', ?)",
                (",".join(settings.MODEL_FALLBACK_CHAIN),),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('model_fallback_ttl', ?)",
                (str(settings.MODEL_FALLBACK_TTL),),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('account_sticky', ?)",
                ("1" if settings.ACCOUNT_STICKY else "0",),
            )
            conn.commit()

    def _load(self) -> None:
        with closing(self._connect()) as conn:
            meta_rows = conn.execute(f"SELECT key, value FROM {_META}").fetchall()
            self._settings = {r["key"]: r["value"] for r in meta_rows}
            self._settings.setdefault("admin_key", settings.DEFAULT_ADMIN_KEY)
            self._settings.setdefault("gateway_key", "")
            self._settings.setdefault("quota_refresh_interval", str(settings.QUOTA_REFRESH_INTERVAL))
            self._settings.setdefault("account_concurrency", str(settings.ACCOUNT_CONCURRENCY))
            self._settings.setdefault("claim_round_interval", str(settings.CLAIM_ROUND_INTERVAL))
            self._settings.setdefault("model_fallback_enabled",
                                      "1" if settings.MODEL_FALLBACK_ENABLED else "0")
            self._settings.setdefault("model_fallback_chain",
                                      ",".join(settings.MODEL_FALLBACK_CHAIN))
            self._settings.setdefault("model_fallback_ttl", str(settings.MODEL_FALLBACK_TTL))
            self._settings.setdefault("account_sticky",
                                      "1" if settings.ACCOUNT_STICKY else "0")

            self._accounts = {p: [] for p in PROVIDERS}
            rows = conn.execute(
                f"SELECT data FROM {_TBL} ORDER BY created_at ASC"
            ).fetchall()
            for row in rows:
                try:
                    account = Account.from_dict(json.loads(row["data"]))
                except (json.JSONDecodeError, TypeError):
                    continue
                if account.provider in self._accounts:
                    self._accounts[account.provider].append(account)
            # 人工排序（sort_order）优先，未排过的（0/相同）按入池时间——后台拖拽
            # 保存的顺序必须重启后仍然生效，否则「手动顺序」只是本次会话的错觉
            for accounts in self._accounts.values():
                accounts.sort(key=lambda a: (int(getattr(a, "sort_order", 0) or 0),
                                             a.created_at or 0))

    def _persist_account(self, account: Account) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                f"""INSERT OR REPLACE INTO {_TBL}
                    (id, provider, name, mode, status, enabled, created_at, data)
                    VALUES (?,?,?,?,?,?,?,?)""",
                (
                    account.id, account.provider, account.name, account.mode,
                    account.status, 1 if account.enabled else 0, account.created_at,
                    json.dumps(account.to_dict(), ensure_ascii=False),
                ),
            )
            conn.commit()

    def _delete_account(self, account_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(f"DELETE FROM {_TBL} WHERE id = ?", (account_id,))
            conn.commit()

    def _set_meta(self, key: str, value: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {_META} (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    def save(self) -> None:
        """全量落库（兜底接口）。"""
        with self._lock:
            for accounts in self._accounts.values():
                for account in accounts:
                    self._persist_account(account)

    # ── 设置 ─────────────────────────────────────────────────────────────────
    def get_setting(self, key: str, default=None):
        with self._lock:
            return self._settings.get(key, default)

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self._settings[key] = str(value)
            self._set_meta(key, str(value))

    def admin_key(self) -> str:
        return str(self.get_setting("admin_key", settings.DEFAULT_ADMIN_KEY) or "")

    def gateway_key(self) -> str:
        return str(self.get_setting("gateway_key", "") or "")

    def quota_refresh_interval(self) -> int:
        try:
            return max(0, int(self.get_setting("quota_refresh_interval", settings.QUOTA_REFRESH_INTERVAL)))
        except (TypeError, ValueError):
            return settings.QUOTA_REFRESH_INTERVAL

    def account_concurrency(self) -> int:
        """单账号并发上限（0 = 不限）。运行时可改（meta 表），改后即生效。"""
        try:
            return max(0, int(self.get_setting("account_concurrency", settings.ACCOUNT_CONCURRENCY)))
        except (TypeError, ValueError):
            return settings.ACCOUNT_CONCURRENCY

    def claim_round_interval(self) -> int:
        """套餐自动领取轮间隔（0 = 关闭）。运行时可改（meta 表），改后即生效。"""
        try:
            return max(0, int(self.get_setting("claim_round_interval", settings.CLAIM_ROUND_INTERVAL)))
        except (TypeError, ValueError):
            return settings.CLAIM_ROUND_INTERVAL

    # ── 模型兜底（自动换模型）─────────────────────────────────────────────────
    def model_fallback_enabled(self) -> bool:
        """请求模型全池不可用时是否自动换到下一个模型（运行时可改）。"""
        raw = str(self.get_setting(
            "model_fallback_enabled",
            "1" if settings.MODEL_FALLBACK_ENABLED else "0",
        )).strip().lower()
        return raw in ("1", "true", "yes", "on")

    def model_fallback_chain(self, provider: str = "zai") -> list[str]:
        """兜底模型顺序（逗号分隔，运行时可改；空 = 该 provider 不兜底）。"""
        default = settings.MODEL_FALLBACK_CHAINS.get(provider)
        if default is None:
            default = settings.MODEL_FALLBACK_CHAIN
        raw = self.get_setting("model_fallback_chain", ",".join(default))
        items = [m.strip() for m in str(raw or "").split(",")]
        return [m for m in items if m]

    def model_fallback_ttl(self) -> int:
        """判定「这个模型全池不可用」后跳过它的秒数（0 = 不跳过，每次照试）。"""
        try:
            return max(0, int(self.get_setting("model_fallback_ttl", settings.MODEL_FALLBACK_TTL)))
        except (TypeError, ValueError):
            return settings.MODEL_FALLBACK_TTL

    # ── 账号读取 ─────────────────────────────────────────────────────────────
    def list_accounts(self, provider: str | None = None) -> list[Account]:
        with self._lock:
            if provider:
                return list(self._accounts.get(provider, []))
            return [a for p in PROVIDERS for a in self._accounts[p]]

    def find(self, provider: str, id_or_name: str) -> Account | None:
        with self._lock:
            return self._find_locked(provider, id_or_name)

    def find_any(self, id_or_name: str) -> Account | None:
        with self._lock:
            for p in PROVIDERS:
                for a in self._accounts[p]:
                    if a.id == id_or_name:
                        return a
        return None

    def _find_locked(self, provider: str, id_or_name: str) -> Account | None:
        for a in self._accounts.get(provider, []):
            if a.id == id_or_name or a.name == id_or_name:
                return a
        return None

    # ── 账号增删改 ───────────────────────────────────────────────────────────
    def add_account(self, provider: str, name: str, secret: str) -> Account:
        if provider not in PROVIDERS:
            raise ValueError(f"不支持的 provider: {provider}")
        account = Account.create(provider, name, secret)
        with self._lock:
            for a in self._accounts[provider]:
                if a.secret and a.secret == account.secret:
                    return a  # 跳过重复 token
            self._assign_fingerprint(account)  # 入池即分配独立设备指纹
            account.install_id = str(uuid.uuid4())  # 安装身份：稳定安装令牌
            # 新号排在队尾（人工排序里「后加的在后」才符合直觉；sort_order 全 0 的
            # 存量池按 created_at 排，这里给一个新号所需的最小递增值）
            account.sort_order = max(
                (int(getattr(a, "sort_order", 0) or 0) for a in self._accounts[provider]),
                default=0,
            ) + 1
            self._accounts[provider].append(account)
            self._persist_account(account)
        return account

    def reorder_accounts(self, ordered_ids: list[str]) -> list[str]:
        """按客户端拖拽后的 id 顺序重排账号（跨 provider 各自保序）。

        只认池内存在的 id，且只改出现在列表里的账号；未列出的账号保持相对位置
        （追加在尾部），避免前端分页/过滤时漏传就静默丢掉排序语义。
        顺序直接决定 `select()` 的轮询次序 —— 拖动第一行就是「优先使用」。
        """
        wanted = [str(i).strip() for i in ordered_ids if str(i or "").strip()]
        if not wanted:
            return []
        with self._lock:
            index = {i: n for n, i in enumerate(wanted)}
            applied: list[str] = []
            for provider, accounts in self._accounts.items():
                tail = max(index.values(), default=-1) + 1
                ordered = []
                for account in accounts:
                    pos = index.get(account.id)
                    if pos is None:
                        # 未在请求里出现：保持原相对顺序排在已排账号之后
                        pos = tail
                        tail += 1
                    ordered.append((pos, account))
                ordered.sort(key=lambda pair: pair[0])
                for pos, account in enumerate(a for _p, a in ordered):
                    account.sort_order = pos
                    self._persist_account(account)
                    applied.append(account.id)
                self._accounts[provider] = [a for _p, a in ordered]
            return applied

    @staticmethod
    def _assign_fingerprint(account: Account) -> None:
        """给账号分配客户端指纹并固化为 dict（随 asdict 落库，取用时还原）。"""
        from .fingerprint import profile_for

        profile = profile_for(account)
        account.fingerprint = {
            "platform": profile.platform, "arch": profile.arch,
            "os_version": profile.os_version, "language": profile.language,
            "timezone": profile.timezone, "screen": profile.screen,
            "device_mid": profile.device_mid,
        }

    def remove_account(self, provider: str, id_or_name: str) -> bool:
        with self._lock:
            items = self._accounts.get(provider, [])
            target = next((a for a in items if a.id == id_or_name or a.name == id_or_name), None)
            if not target:
                return False
            self._accounts[provider] = [a for a in items if a.id != target.id]
            self._delete_account(target.id)
            return True

    def update_account(self, account: Account) -> bool:
        """持久化某个账号的当前状态。

        账号已从内存池删除时拒绝写回，避免后台任务 INSERT OR REPLACE 把已删行救活。
        """
        with self._lock:
            if self._find_locked(account.provider, account.id) is None:
                return False
            self._persist_account(account)
            return True

    def set_enabled(self, provider: str, id_or_name: str, enabled: bool) -> bool:
        with self._lock:
            account = self._find_locked(provider, id_or_name)
            if not account:
                return False
            account.enabled = enabled
            if not enabled:
                account.status = Status.DISABLED
            elif account.status in (Status.DISABLED, Status.COOLING):
                # 人工启用 = 确认恢复：禁用洗回 active；冷却中启用视为跳过等待
                #（风控退避本就是自动恢复语义，人工提前放行同样成立）
                account.status = Status.ACTIVE
                account.cooling_until = None
            self._persist_account(account)
            return True

    # ── 全局粘号（「正常情况下就一直用一个账号」）───────────────────────────────
    def sticky_account(self, provider: str) -> str:
        """上次成功服务的账号 id（全局粘号，未过期才算）。

        与会话粘性（affinity）的分工：affinity 管「同一段对话回同一个号」（上游
        前缀缓存命中），全局粘号管「所有流量都尽量待在一个号上」——不认识的会话
        也先回到上次那个号，而不是轮询把流量摊开。返回 "" 表示没有记住的。
        """
        with self._lock:
            at = self._sticky_at.get(provider, 0.0)
            if not at or time.time() - at > self._sticky_ttl:
                return ""
            return self._sticky_id.get(provider, "")

    def set_sticky_account(self, provider: str, account_id: str) -> None:
        """成功拿到 200 之后调用：把全局粘号指向这个账号。"""
        with self._lock:
            if not account_id:
                return
            self._sticky_id[provider] = account_id
            self._sticky_at[provider] = time.time()

    def clear_sticky_account(self, provider: str | None = None) -> None:
        """测试夹具 / 后台「重新粘号」用。"""
        with self._lock:
            if provider is None:
                self._sticky_id.clear()
                self._sticky_at.clear()
            else:
                self._sticky_id.pop(provider, None)
                self._sticky_at.pop(provider, None)

    def account_sticky_enabled(self) -> bool:
        """全局粘号开关（运行时可改，meta 表）。"""
        raw = str(self.get_setting(
            "account_sticky",
            "1" if settings.ACCOUNT_STICKY else "0",
        )).strip().lower()
        return raw in ("1", "true", "yes", "on")

    # ── 轮询选择 ─────────────────────────────────────────────────────────────
    def count_selectable(self, provider: str, now: float | None = None) -> int:
        """当前可被轮询选中的账号数（不推进游标）。

        模型兜底要判断「是不是把全池都问过了」：`store.select` 返回 None 只能说明
        「剩下的都被 skip_ids 挡住了」，而账号数正好等于 MAX_ACCOUNT_ATTEMPTS 时
        循环会先被 attempts 上限截断，永远等不到那个 None。所以要有一个独立的池
        大小口径（见 gateway._dispatch_model）。
        """
        with self._lock:
            return sum(
                1 for a in self._accounts.get(provider, [])
                if a.is_selectable(now)
            )

    def select(self, provider: str, skip_ids: set[str] | None = None,
               prefer: str | None = None) -> Account | None:
        """选择下一个可用账号：粘性命中优先，否则 round-robin。

        prefer 是会话粘性记住的账号 id（见 app/affinity.py）：它仍在可用池里
        就直接用它，且**不推进轮询指针**——粘性命中不是一次"轮到谁"，让指针
        原地不动，别把轮询节奏带偏。它不在池里（冷却 / 额度用完 / 已删除 /
        并发满被 skip）才照常轮询。
        """
        skip_ids = skip_ids or set()
        now = time.time()
        with self._lock:
            pool = [
                a for a in self._accounts.get(provider, [])
                if a.is_selectable(now) and a.id not in skip_ids
            ]
            if not pool:
                return None
            if prefer:
                for a in pool:
                    if a.id == prefer:
                        return a
            idx = self._rotation.get(provider, 0) % len(pool)
            account = pool[idx]
            self._rotation[provider] = (idx + 1) % len(pool)
            return account

    # ── 导入 / 导出 ─────────────────────────────────────────────────────────
    def export(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "exported_at": time.time(),
                "providers": {
                    p: [
                        {"name": a.name, "mode": a.mode, "secret": a.secret}
                        for a in self._accounts[p]
                    ]
                    for p in PROVIDERS
                },
            }

    def import_accounts(self, payload: dict) -> int:
        providers = payload.get("providers", {})
        count = 0
        for provider, items in providers.items():
            if provider not in PROVIDERS or not isinstance(items, list):
                continue
            for it in items:
                secret = it.get("secret") or it.get("token") or it.get("jwtToken") or it.get("apiKey")
                if not secret:
                    continue
                self.add_account(provider, it.get("name", provider), secret)
                count += 1
        return count


# 单例
store = Store()
