"""日志脱敏 Filter 单测（验收 2）：cookie 值只能以 <redacted> 出现。"""
from __future__ import annotations

import io
import logging

from logging_setup import RedactFilter, redact_cookie


def test_redact_cookie_sessdata() -> None:
    """SESSDATA 值被替换，结尾分号保留。"""
    assert redact_cookie("SESSDATA=abcdef123456;") == "SESSDATA=<redacted>;"


def test_redact_cookie_all_fields() -> None:
    """bili_jct / buvid3 同样脱敏，原始值绝不出现。"""
    text = "SESSDATA=abc; bili_jct=xyz789; buvid3=uuid-123;"
    out = redact_cookie(text)
    assert "abc" not in out and "xyz789" not in out and "uuid-123" not in out
    assert "SESSDATA=<redacted>" in out
    assert "bili_jct=<redacted>" in out
    assert "buvid3=<redacted>" in out


def test_redact_filter_through_logger() -> None:
    """验收 2：日志含 SESSDATA=abcdef123456; 时输出只能是 SESSDATA=<redacted>;。"""
    logger = logging.getLogger("test.redact")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)

    logger.info("cookie SESSDATA=abcdef123456; 其余内容")
    out = stream.getvalue()
    assert "SESSDATA=abcdef123456" not in out
    assert "SESSDATA=<redacted>;" in out
    # 唯一出现形式必须是脱敏后的
    assert out.count("SESSDATA=") == 1


def test_redact_filter_debug_level() -> None:
    """DEBUG 级也不例外（文档 §12 铁律）。"""
    logger = logging.getLogger("test.redact.debug")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)

    logger.debug("bili_jct=secret-value; buvid3=another-secret;")
    out = stream.getvalue()
    assert "secret-value" not in out and "another-secret" not in out
    assert "bili_jct=<redacted>; buvid3=<redacted>;" in out
