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


def test_redact_filter_exc_info_in_exception_message(tmp_path) -> None:
    """F3-4 验收（§9.4/§12）：异常堆栈含 SESSDATA=xxx 时控制台与文件两处输出
    都必须为 <redacted>（logger.exception 的 exc_info 路径）。"""
    target_logger = logging.getLogger("test.redact.exc")
    target_logger.setLevel(logging.DEBUG)
    target_logger.handlers.clear()
    target_logger.propagate = False

    stream = io.StringIO()
    console = logging.StreamHandler(stream)
    console.setLevel(logging.DEBUG)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    console.addFilter(RedactFilter())
    target_logger.addHandler(console)

    log_file = tmp_path / "exc.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    file_handler.addFilter(RedactFilter())
    target_logger.addHandler(file_handler)
    try:
        # 构造异常：cookie 同时出现在异常消息（args）与 raise 处
        try:
            raise RuntimeError("请求失败 cookie: SESSDATA=leak-token-123;")
        except RuntimeError:
            target_logger.exception("处理收藏时出错 SESSDATA=leak-token-123;")

        console_out = stream.getvalue()
        file_out = log_file.read_text(encoding="utf-8")

        for label, text in (("控制台", console_out), ("文件", file_out)):
            assert "leak-token-123" not in text, f"{label}泄漏明文 cookie"
            assert text.count("SESSDATA=<redacted>") >= 2, (
                f"{label}异常消息与堆栈两处都须脱敏，实际: {text!r}"
            )
            assert "Traceback" in text, f"{label}应包含异常堆栈"
            assert "RuntimeError" in text
    finally:
        file_handler.close()
        target_logger.removeHandler(file_handler)
        target_logger.removeHandler(console)


def test_redact_filter_exc_info_tuple_directly() -> None:
    """F3-4 补充：直接 log(exc_info=sys.exc_info()) 与 stack_info 路径脱敏。"""
    import sys

    target_logger = logging.getLogger("test.redact.exc2")
    target_logger.setLevel(logging.DEBUG)
    target_logger.handlers.clear()
    target_logger.propagate = False

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactFilter())
    target_logger.addHandler(handler)
    try:
        try:
            raise ValueError("bad SESSDATA=stack-secret;")
        except ValueError:
            target_logger.error("外层 SESSDATA=stack-secret;", exc_info=sys.exc_info())

        # stack_info：栈帧源码行内含 cookie 字符串
        target_logger.warning("栈信息 SESSDATA=stack-secret;", stack_info=True)

        out = stream.getvalue()
        assert "stack-secret" not in out
        assert "SESSDATA=<redacted>" in out
        assert "ValueError" in out  # exc_info 元组格式化为堆栈
        assert "Stack (most recent call last)" in out  # stack_info 转储存在且脱敏
    finally:
        target_logger.removeHandler(handler)
