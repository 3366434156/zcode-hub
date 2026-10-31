#!/usr/bin/env python
"""把 zcode-switch 导出的 `.zsb` 账号封包导入 zcode-hub。

背景：本仓库的 `.zsb` 封包支持（app/bundle.py）仍在路线图里（docs/development/07-roadmap.md），
`cli.py import` 只认明文 JSON。本脚本补齐「.zsb 直接入池」这一环：

  .zsb envelope（zcode-switch src-tauri/src/cipher.rs）
    format   = "zsw-accounts-bundle"
    version  = 1
    kdf      = {algo: pbkdf2-hmac-sha256, iters: 100000, salt: b64}
    cipher   = {algo: aes-256-gcm, nonce: b64(12B), tag: b64, data: b64}
    → 明文 = {format: "zcode-accounts-bundle", version: 2, exportedAt, accounts: [...]}

  enc:v1 字段（zcode-switch src-tauri/src/zcrypto.rs）
    "enc:v1:" + b64url(nonce).b64url(tag).b64url(ct)，key = sha256(secret)，
    secret 默认 = "zcode-credential-fallback:{process.platform}:{home}:{username}"
    （可用 ZCODE_CREDENTIAL_SECRET 覆盖；本机导出/导入才可解）

字段映射（本仓库 provider 语义见 README「支持的账号类型」）：
  zcodejwttoken                              → provider "zai"，mode jwt（Coding Plan 通道）
  config.provider[*].options.apiKey（coding-plan）
                                             → provider "bigmodel"，mode apiKey（智谱回退通道）

用法：
  python scripts/zsb_import.py <bundle.zsb> [--password 口令] [--out 文件] [--apply]
                                [--no-bigmodel] [--no-install]

  口令优先级：--password > ZSW_PASSWORD > ZCODE_BUNDLE_PASSWORD
  默认只导出明文 JSON（供 `python cli.py import <文件>` 使用）；`--apply` 直接写库。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFIX_SEALED = "enc:v1:"
KDF_ITERS = 100_000


# ── 解密 ─────────────────────────────────────────────────────────────────────
def _b64(value: str) -> bytes:
    return base64.b64decode(value)


def derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=KDF_ITERS).derive(password.encode())


def open_bundle(path: Path, password: str) -> dict:
    """解封 .zsb，返回封包明文（结构见模块 docstring）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    envelope = json.loads(path.read_text(encoding="utf-8"))
    kdf, cipher = envelope.get("kdf"), envelope.get("cipher")
    if not isinstance(kdf, dict) or not isinstance(cipher, dict):
        raise SystemExit("不是 .zsb 封包（缺少 kdf / cipher 字段）")
    if kdf.get("algo") != "pbkdf2-hmac-sha256" or cipher.get("algo") != "aes-256-gcm":
        raise SystemExit(f"不支持的算法：kdf={kdf.get('algo')} cipher={cipher.get('algo')}")
    if int(kdf.get("iters") or 0) != KDF_ITERS:
        raise SystemExit(f"KDF 迭代数与互通约定不符：{kdf.get('iters')} != {KDF_ITERS}")
    nonce = _b64(cipher["nonce"])
    if len(nonce) != 12:
        raise SystemExit(f"nonce 长度异常：{len(nonce)}")

    sealed = _b64(cipher["data"]) + _b64(cipher["tag"])
    try:
        plain = AESGCM(derive_key(password, _b64(kdf["salt"]))).decrypt(nonce, sealed, None)
    except Exception:  # noqa: BLE001 - 错口令与篡改同形，按设计统一报错
        raise SystemExit("解密失败：口令错误或封包被篡改（GCM 校验未通过）") from None
    payload = json.loads(plain.decode("utf-8"))
    if payload.get("format") != "zcode-accounts-bundle":
        raise SystemExit(f"未识别的封包内容 format={payload.get('format')!r}")
    return payload


def machine_secret() -> str:
    """zcode-switch 默认的 enc:v1 密文密钥（zcrypto::default_secret）。"""
    env = os.environ.get("ZCODE_CREDENTIAL_SECRET")
    if env:
        return env
    if sys.platform == "win32":
        platform = "win32"
    elif sys.platform == "darwin":
        platform = "darwin"
    else:
        platform = "linux"
    username = os.environ.get("USERNAME") or os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    return f"zcode-credential-fallback:{platform}:{Path.home()}:{username}"


def unseal(value, secret: str):
    """解 enc:v1 字段；非密文原样返回；解不开返回 None。"""
    if not isinstance(value, str):
        return value
    if not value.startswith(PREFIX_SEALED):
        return value
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    parts = value[len(PREFIX_SEALED):].split(".")
    if len(parts) != 3:
        return None
    try:
        nonce, tag, ct = (base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)) for p in parts)
        key = hashlib.sha256(secret.encode()).digest()
        return AESGCM(key).decrypt(nonce, ct + tag, None).decode()
    except Exception:  # noqa: BLE001 - 跨机导出时必然解不开
        return None


# ── 字段映射 ─────────────────────────────────────────────────────────────────
def coding_plan_keys(account: dict) -> list[str]:
    """账号 config 快照里 coding-plan provider 的 apiKey（智谱回退通道凭证）。"""
    providers = ((account.get("config") or {}).get("provider") or {})
    keys: list[str] = []
    for pid, prov in providers.items():
        if "coding-plan" not in pid or not isinstance(prov, dict):
            continue
        key = ((prov.get("options") or {}).get("apiKey") or "").strip()
        if not key or key.startswith(PREFIX_SEALED) or len(key) < 30 or key in keys:
            continue
        keys.append(key)
    return keys


def to_payload(bundle: dict, secret: str, include_bigmodel: bool = True) -> tuple[dict, list[str]]:
    """把封包明文转成 `cli.py import` 认的明文导出结构（store.import_accounts）。"""
    note: list[str] = []
    providers: dict[str, list[dict]] = {"zai": [], "bigmodel": []}
    for idx, item in enumerate(bundle.get("accounts") or []):
        name = (item.get("name") or f"account-{idx + 1}").strip()
        creds = item.get("credentials") or {}
        if not isinstance(creds, dict):
            note.append(f"{name}: credentials 非对象，跳过")
            continue

        jwt = unseal(creds.get("zcodejwttoken"), secret)
        if not jwt:
            note.append(f"{name}: zcodejwttoken 缺失或解不开（跨机导出的 enc:v1），跳过")
        elif jwt.count(".") != 2:
            note.append(f"{name}: zcodejwttoken 不是三段 JWT，跳过")
        else:
            providers["zai"].append({"name": name, "mode": "jwt", "secret": jwt})

        if not include_bigmodel:
            continue
        for key in coding_plan_keys(item):
            if len(key) == 32:  # 仅密钥 ID（缺 secret 段），智谱端点会 401
                note.append(f"{name}: coding-plan apiKey 仅 32 位 ID（无 secret 段），跳过")
                continue
            providers["bigmodel"].append({"name": name, "mode": "apiKey", "secret": key})

    payload = {
        "version": 1,
        "exported_at": time.time(),
        "providers": {p: items for p, items in providers.items() if items},
    }
    return payload, note


def mask(secret: str) -> str:
    return f"{secret[:6]}…{secret[-4:]}（len={len(secret)}）" if len(secret) > 12 else "***"


def main() -> None:
    ap = argparse.ArgumentParser(description="导入 zcode-switch 导出的 .zsb 账号封包")
    ap.add_argument("bundle", help=".zsb 文件路径")
    ap.add_argument("--password", help="封包口令（也可用 ZSW_PASSWORD / ZCODE_BUNDLE_PASSWORD）")
    ap.add_argument("--out", help="明文 JSON 输出路径（默认与封包同目录的 <stem>.json）")
    ap.add_argument("--apply", action="store_true", help="直接写入 zcode-hub 账号库（data/accounts.db）")
    ap.add_argument("--no-bigmodel", action="store_true", help="不导入 bigmodel API Key 账号")
    ap.add_argument("--no-install", action="store_true", help="入池后不跑按账号安装序")
    args = ap.parse_args()

    password = (args.password or os.environ.get("ZSW_PASSWORD")
                or os.environ.get("ZCODE_BUNDLE_PASSWORD") or "")
    if not password.strip():
        raise SystemExit("缺少口令：用 --password 或环境变量 ZSW_PASSWORD / ZCODE_BUNDLE_PASSWORD")

    bundle_path = Path(args.bundle).expanduser().resolve()
    if not bundle_path.is_file():
        raise SystemExit(f"文件不存在：{bundle_path}")

    bundle = open_bundle(bundle_path, password)
    secret = machine_secret()
    payload, notes = to_payload(bundle, secret, include_bigmodel=not args.no_bigmodel)

    total = sum(len(v) for v in payload["providers"].values())
    print(f"封包：{bundle_path}")
    print(f"导出时间：{bundle.get('exportedAt')}   账号数：{len(bundle.get('accounts') or [])}")
    for provider, items in payload["providers"].items():
        print(f"  {provider:8s} × {len(items)}")
        for it in items:
            print(f"    - {it['name']:12s} {it['mode']:7s} {mask(it['secret'])}")
    for msg in notes:
        print(f"  ! {msg}")
    print(f"待导入共 {total} 个账号")

    if not total:
        raise SystemExit("没有可导入的账号")

    out = Path(args.out).expanduser() if args.out else bundle_path.with_suffix(".json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明文导出：{out}（含凭证明文，注意保密）")

    if not args.apply:
        print(f"下一步：python cli.py import {out}")
        return

    from app.install import run_install_sequence_for_account
    from app.store import store

    before = {a.id for a in store.list_accounts()}
    count = store.import_accounts(payload)
    fresh = [a for a in store.list_accounts() if a.id not in before]
    print(f"已写入账号库：{count} 个（实际新增 {len(fresh)}）")
    if args.no_install:
        return
    import asyncio

    for account in fresh:
        try:
            result = asyncio.run(run_install_sequence_for_account(account))
        except Exception as err:  # noqa: BLE001 - 安装序失败不阻断导入
            print(f"  ! {account.name} 安装序异常：{err}")
            continue
        if result.get("skipped"):
            print(f"  · {account.name} 安装序已存在，跳过")
        elif result.get("installed"):
            print(f"  ✔ {account.name} 安装序完成")
        else:
            print(f"  ! {account.name} 安装序部分失败：{'; '.join(result.get('errors') or [])}")


if __name__ == "__main__":
    main()
