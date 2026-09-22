"""B 站授权与凭证加密存储（里程碑 M5，对应文档 §11.3 / §9.4）。

- `python main.py auth`：playwright 打开 Chromium 访问 bilibili.com，用户
  登录后自动检测 SESSDATA + bili_jct + buvid3 齐备，抓取后 Fernet 加密
  写入 credentials.db 并关闭浏览器；
- `--paste` 回退：无桌面环境时提示手动粘贴 cookie 字符串（等效 v0.1）；
- 重复执行 auth 视为刷新，覆盖旧凭证（文档 §11.3 第 4 条）；
- 密钥派生自机器特征（/etc/machine-id 或注册表 MachineGuid），缺失时随机
  生成并存于 ~/.ncm2bili/key，权限 600（文档 §9.4）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import sys
import time
from contextlib import suppress
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from db import Database

logger = logging.getLogger(__name__)

# credentials 表中 B 站 cookie 的存储 key
CRED_KEY = "bili_cookie"

# 文档 §11.3：必需 cookie 三件套
REQUIRED_COOKIES = ("SESSDATA", "bili_jct", "buvid3")

# 默认密钥文件位置（用户目录）
_DEFAULT_KEY_FILE = Path.home() / ".ncm2bili" / "key"

# 登录等待超时
_LOGIN_TIMEOUT_S = 300

# playwright 启动的首页
_BILI_HOME = "https://www.bilibili.com/"


class AuthError(Exception):
    """授权流程失败。"""


def _machine_guid() -> str | None:
    """提取机器特征：Linux 的 machine-id / Windows 注册表 MachineGuid。"""
    if sys.platform != "win32":
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                raw = Path(p).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if raw:
                return raw
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(guid) if guid else None
    except OSError:
        return None


def load_or_create_key(key_file: str | Path | None = None) -> bytes:
    """加载或生成 Fernet 密钥。

    优先级（文档 §9.4）：
    1. 已存在的 key 文件（~/.ncm2bili/key，权限 600）直接复用；
    2. 机器特征派生（machine-id / MachineGuid → sha256 → urlsafe b64）；
    3. 两者均不可得 → 随机生成并写入 key 文件（权限 600）。

    Args:
        key_file: 自定义密钥文件路径（测试注入；默认 ~/.ncm2bili/key）。
    """
    key_file = Path(key_file) if key_file is not None else _DEFAULT_KEY_FILE

    if key_file.exists():
        try:
            raw = key_file.read_bytes()
            Fernet(raw)  # 校验密钥格式
            return raw
        except (ValueError, InvalidToken, OSError):
            pass  # 文件损坏/无效，重新生成

    guid = _machine_guid()
    key = (
        base64.urlsafe_b64encode(hashlib.sha256(guid.encode()).digest())
        if guid
        else Fernet.generate_key()
    )

    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(key)
    with suppress(OSError):
        key_file.chmod(0o600)  # 权限 600（Windows 尽力而为）
    return key


def encrypt_cookie(cookie_str: str, key: bytes) -> str:
    """Fernet 加密 cookie 串。"""
    return Fernet(key).encrypt(cookie_str.encode()).decode()


def decrypt_cookie(token: str, key: bytes) -> str:
    """Fernet 解密 cookie 串。"""
    return Fernet(key).decrypt(token.encode()).decode()


def parse_cookie_str(cookie_str: str) -> dict[str, str]:
    """解析浏览器复制的 cookie 字符串，校验必需项（SESSDATA/bili_jct/buvid3）。

    兼容 "k=v; k2=v2" 与 "k=v, k2=v2" 两种分隔。
    缺失必需项时抛 AuthError。
    """
    result: dict[str, str] = {}
    for part in cookie_str.replace(",", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        result[name.strip()] = value.strip()
    missing = [k for k in REQUIRED_COOKIES if k not in result]
    if missing:
        raise AuthError(f"cookie 缺少必需项: {', '.join(missing)}")
    return result


def build_cookie_str(cookies: dict[str, str]) -> str:
    """把 cookie 字典组装为 "k=v; k2=v2" 串。"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def save_credentials(db: Database, cookie_str: str, key: bytes) -> None:
    """Fernet 加密后写入 credentials.db（key='bili_cookie'），重复执行覆盖旧凭证。"""
    token = encrypt_cookie(cookie_str, key)
    db.execute(
        "INSERT INTO credentials (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (CRED_KEY, token, int(time.time())),
    )
    logger.info("凭证已加密写入 credentials.db（SESSDATA=<redacted>）")


def load_credentials(db: Database, key: bytes) -> str | None:
    """从 credentials.db 读取并解密 cookie 串；无凭证时返回 None。"""
    row = db.query_one("SELECT value FROM credentials WHERE key = ?", (CRED_KEY,))
    if row is None or not row["value"]:
        return None
    return decrypt_cookie(row["value"], key)


# ---- playwright 浏览器授权 --------------------------------------------


async def _auth_via_browser(db: Database, key: bytes, *, timeout_s: int = _LOGIN_TIMEOUT_S) -> None:
    """playwright 打开 Chromium 访问 B 站首页，等待登录后抓取 cookie。"""
    from playwright.async_api import async_playwright

    print("正在打开浏览器，请在弹出的 Chromium 中登录 B 站……")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(_BILI_HOME, wait_until="domcontentloaded")

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                await page.wait_for_timeout(1000)
                cookies = await context.cookies()
                values = {c["name"]: c["value"] for c in cookies}
                if all(k in values for k in REQUIRED_COOKIES):
                    cookie_str = build_cookie_str({k: values[k] for k in REQUIRED_COOKIES})
                    save_credentials(db, cookie_str, key)
                    print("登录凭证已捕获并加密保存，正在关闭浏览器……")
                    return
            raise AuthError(f"等待登录超时（{timeout_s}s），未检测到完整凭证")
        finally:
            await browser.close()


# ---- --paste 回退 -----------------------------------------------------


def auth_via_paste(db: Database, key: bytes) -> None:
    """无桌面环境回退：提示手动粘贴 cookie 字符串（等效 v0.1）。"""
    print("无桌面环境，请从浏览器手动复制 Cookie 字符串并粘贴：")
    print(
        "获取路径：F12 → Application → Cookies → bilibili.com "
        "→ 右键 Copy → Copy as cURL，取其 Cookie 头"
    )
    try:
        raw = input().strip()
    except EOFError:
        raise AuthError("未读取到输入") from None
    if not raw:
        raise AuthError("未输入任何内容")
    cookies = parse_cookie_str(raw)
    cookie_str = build_cookie_str({k: cookies[k] for k in REQUIRED_COOKIES})
    save_credentials(db, cookie_str, key)
    print("粘贴成功，凭证已加密保存")


def run_auth(
    db: Database,
    *,
    paste: bool = False,
    key_file: str | Path | None = None,
    timeout_s: int = _LOGIN_TIMEOUT_S,
) -> None:
    """auth 命令入口：key 管理 + 浏览器授权或 --paste 回退。"""
    key = load_or_create_key(key_file)
    if paste:
        auth_via_paste(db, key)
    else:
        asyncio.run(_auth_via_browser(db, key, timeout_s=timeout_s))


__all__ = [
    "AuthError",
    "CRED_KEY",
    "REQUIRED_COOKIES",
    "auth_via_paste",
    "build_cookie_str",
    "decrypt_cookie",
    "encrypt_cookie",
    "load_credentials",
    "load_or_create_key",
    "parse_cookie_str",
    "run_auth",
    "save_credentials",
]
