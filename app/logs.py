"""日志工具 — ANSI 彩色终端输出 (Windows GBK 兼容) + 网关日志台账。

控制台输出保持原样（launcher 窗口仍然可看）；同时把每条日志交给
`app/logstore.py` 落盘 + 入内存环，后台「网关日志」页的数据源就是它。
进程死后文件还在——这是「网关怎么自动关了」唯一能查案的地方。
"""

_R = "\033[0m"
_G = "\033[32m"
_C = "\033[36m"
_Y = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[90m"
_B = "\033[1m"
_MAG = "\033[35m"
_W = "\033[37m"


def _store(level: str, tag: str, msg: str, req_id: str = "") -> None:
    """交给台账（延迟导入：logstore 也会 import 本模块的常量，避免环）。

    本模块的第一参在历史上是「模块名」和「req_id」混用的（req/req_err 传
    req_id，其余传模块名）。模块名是可读单词、req_id 是 16 位 hex——按形状
    区分，台账的模块筛选才不会被一堆乱码标签污染。
    """
    try:
        from .logstore import logstore
        if not req_id and _REQ_ID_RE.fullmatch(tag or ""):
            req_id, tag = tag, "gateway"
        logstore.write(level, tag, msg, req_id)
    except Exception:  # noqa: BLE001 - 日志台账失败绝不影响主流程
        pass


# 16 位十六进制 = secrets.token_hex(8) 生成的 req_id
_REQ_ID_RE = __import__("re").compile(r"[0-9a-f]{16}")


def ok(module: str, msg: str):
    print(f"  {_G}[+]{_R} {_DIM}{module}{_R} {msg}")
    _store("INFO", module, msg)


def step(module: str, msg: str):
    print(f"  {_C}[>]{_R} {_DIM}{module}{_R} {msg}")
    _store("INFO", module, msg)


def info(module: str, msg: str):
    print(f"  {_DIM}[-] {module} {msg}{_R}")
    _store("INFO", module, msg)


def warn(module: str, msg: str):
    print(f"  {_Y}[~]{_R} {_DIM}{module}{_R} {msg}")
    _store("WARN", module, msg)


def err(module: str, msg: str):
    print(f"  {_RED}[!]{_R} {_DIM}{module}{_R} {msg}")
    _store("ERROR", module, msg)


def header(module: str, msg: str):
    print(f"  {_B}{_MAG}{module}{_R} {msg}")
    _store("INFO", module, msg)


def req(req_id: str, model: str, stream: bool, last_msg: str):
    """请求日志 — 一行显示关键信息"""
    s = "stream" if stream else "sync"
    msg_preview = last_msg[:40] + ("..." if len(last_msg) > 40 else "")
    print(f"  {_C}>>>{_R} {_DIM}{req_id}{_R}  {_W}{model}{_R}  {_DIM}{s}{_R}  {_DIM}\"{msg_preview}\"{_R}")
    _store("INFO", "gateway", f"{model} {s} \"{msg_preview}\"", req_id)


def req_ok(req_id: str, tokens: int = 0):
    """请求完成"""
    t = f"  {_DIM}{tokens}tok{_R}" if tokens else ""
    print(f"  {_G}<<<{_R} {_DIM}{req_id}{_R}{t}")
    _store("INFO", "gateway", "完成" + (f" · {tokens}tok" if tokens else ""), req_id)


def req_err(req_id: str, msg: str):
    """请求错误 — 只显示 req_id + 简短原因，不泄露上游"""
    print(f"  {_RED}<!>{_R} {_DIM}{req_id}{_R}  {msg}")
    _store("ERROR", "gateway", msg, req_id)


def banner(lines: list[str]):
    import re

    strip_ansi = re.compile(r"\033\[[0-9;]*m")
    w = max(len(strip_ansi.sub("", line)) for line in lines) + 4
    border = f"{_DIM}{'=' * w}{_R}"
    print(f"\n{border}")
    for line in lines:
        print(f"  {line}")
    print(f"{border}\n")
    _store("INFO", "app", " · ".join(strip_ansi.sub("", line) for line in lines))

