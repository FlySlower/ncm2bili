"""auth.py 单测（里程碑 M5，文档 §11.3 / §9.4）。

覆盖：
- cookie 字符串解析与必需项校验；
- Fernet 加密/解密往返；
- 密钥文件生成（权限 600）与机器特征派生；
- 凭证加密落盘 + 日志脱敏联合用例（credentials.db 中无明文 SESSDATA）。
"""
from __future__ import annotations

import io
import logging

import pytest

from auth import (
    AuthError,
    build_cookie_str,
    decrypt_cookie,
    encrypt_cookie,
    load_credentials,
    load_or_create_key,
    parse_cookie_str,
    save_credentials,
)
from db import Database
from logging_setup import RedactFilter

COOKIE_STR = (
    "SESSDATA=abcdef1234567890; bili_jct=xyz789; "
    "buvid3=UUID-1234; DedeUserID=1;"
)


# ---- cookie 解析 ------------------------------------------------------


def test_parse_cookie_str_extracts_required() -> None:
    cookies = parse_cookie_str(COOKIE_STR)
    assert cookies["SESSDATA"] == "abcdef1234567890"
    assert cookies["bili_jct"] == "xyz789"
    assert cookies["buvid3"] == "UUID-1234"


def test_parse_cookie_str_accepts_comma_separator() -> None:
    """兼容 "k=v, k2=v2" 分隔（浏览器导出常见格式）。"""
    cookies = parse_cookie_str("SESSDATA=a1, bili_jct=b2, buvid3=c3")
    assert cookies == {"SESSDATA": "a1", "bili_jct": "b2", "buvid3": "c3"}


def test_parse_cookie_str_missing_required_raises() -> None:
    with pytest.raises(AuthError, match="SESSDATA"):
        parse_cookie_str("bili_jct=xyz; buvid3=u")


def test_build_cookie_str_roundtrip() -> None:
    cookies = parse_cookie_str(COOKIE_STR)
    rebuilt = build_cookie_str(cookies)
    assert parse_cookie_str(rebuilt) == cookies


# ---- 加密/解密 --------------------------------------------------------


def test_encrypt_decrypt_roundtrip(tmp_path) -> None:
    key = load_or_create_key(tmp_path / "key")  # 测试用临时密钥文件
    token = encrypt_cookie(COOKIE_STR, key)
    assert token != COOKIE_STR  # 密文 ≠ 明文
    assert "SESSDATA" not in token  # 密文中无明文 cookie 字段
    assert decrypt_cookie(token, key) == COOKIE_STR


# ---- 密钥文件 ---------------------------------------------------------


def test_load_or_create_key_creates_file_with_600(tmp_path) -> None:
    key_file = tmp_path / "key"
    key1 = load_or_create_key(key_file)
    assert key_file.exists()
    assert len(key1) == 44  # Fernet key 为 32 字节 urlsafe base64
    # 权限 600
    import os

    if os.name != "nt":  # Windows 无 POSIX 权限位
        assert key_file.stat().st_mode & 0o777 == 0o600
    # 重复调用复用同一文件，密钥稳定
    key2 = load_or_create_key(key_file)
    assert key1 == key2


def test_load_or_create_key_machine_derived(tmp_path, monkeypatch) -> None:
    """机器特征可用时密钥由 machine-id 派生（确定性）。"""
    machine_id = "deadbeef-cafe-1234-5678-9abcdef01234"
    monkeypatch.setattr("auth._machine_guid", lambda: machine_id)
    key1 = load_or_create_key(tmp_path / "k1")
    key2 = load_or_create_key(tmp_path / "k2")
    assert key1 == key2  # 同一机器特征 → 同一密钥


# ---- 凭证落盘 + 脱敏联合用例（验收） ----------------------------------


def test_credentials_db_no_plaintext_and_log_redacted(tmp_path) -> None:
    """验收：运行后 credentials.db 与日志中均无明文 SESSDATA。"""
    db = Database(tmp_path / "credentials.db")
    key = load_or_create_key(tmp_path / "key")

    # 捕获 auth 模块日志（挂 RedactFilter）
    logger = logging.getLogger("auth")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)

    save_credentials(db, COOKIE_STR, key)

    # credentials.db 中无明文
    row = db.query_one("SELECT value FROM credentials WHERE key = 'bili_cookie'")
    assert row is not None
    assert "abcdef1234567890" not in row["value"]
    assert "SESSDATA" not in row["value"]
    # 密文可解密还原
    assert decrypt_cookie(row["value"], key) == COOKIE_STR
    # load_credentials 往返
    assert load_credentials(db, key) == COOKIE_STR

    # 日志无明文 SESSDATA
    out = stream.getvalue()
    assert "abcdef1234567890" not in out
    assert "SESSDATA=<redacted>" in out


def test_credentials_overwrite_on_rerun(tmp_path) -> None:
    """重复 auth 视为刷新，覆盖旧凭证（文档 §11.3 第 4 条）。"""
    db = Database(tmp_path / "c.db")
    key = load_or_create_key(tmp_path / "key")

    save_credentials(db, COOKIE_STR, key)
    new_cookie = "SESSDATA=new-secret; bili_jct=new-jct; buvid3=new-b3;"
    save_credentials(db, new_cookie, key)

    assert load_credentials(db, key) == new_cookie
    row = db.query_one("SELECT value FROM credentials WHERE key = 'bili_cookie'")
    assert row is not None
    assert "new-secret" not in row["value"]  # 密文，无明文


def test_save_credentials_chmod_600(tmp_path, monkeypatch) -> None:
    """V5-P2-6（§9.4）：凭证落盘后对 credentials.db 执行 os.chmod(path, 0o600)。

    Windows os.chmod 的 POSIX 权限位语义受限（仅只读位），用例只断言调用本身：
    目标路径为 credentials.db、mode 为 0o600。先建密钥再 patch，避免密钥文件的
    Path.chmod（内部同样走 os.chmod）混入调用记录。
    """
    import auth

    db_path = tmp_path / "credentials.db"
    db = Database(db_path)
    key = load_or_create_key(tmp_path / "key")  # 先建密钥：其 chmod 不被捕获

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(auth.os, "chmod", lambda p, mode: calls.append((p, mode)))

    save_credentials(db, COOKIE_STR, key)

    assert calls == [(db.path, 0o600)]
