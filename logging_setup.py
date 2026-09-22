"""日志初始化模块（里程碑 M0，对应文档 §12）。

- 控制台：INFO+，简洁格式；
- 文件：logs/app_YYYY-MM-DD.log，全级别，含时间/模块/级别；
- 脱敏铁律：日志 Filter 在 Formatter 之前把 SESSDATA / bili_jct / buvid3
  的 cookie 值统一替换为 <redacted>，DEBUG 级也不例外（文档 §12）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

# cookie 名大小写不敏感，值匹配到分号或空白为止（保留结尾分号）
_COOKIE_RE = re.compile(r"(SESSDATA|bili_jct|buvid3)=([^;\s]+)", re.IGNORECASE)

_DEFAULT_LOG_DIR = Path("logs")
_CONSOLE_FORMAT = "%(levelname)s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def redact_cookie(text: str) -> str:
    """把 cookie 值替换为 <redacted>，返回处理后的文本。"""
    return _COOKIE_RE.sub(r"\1=<redacted>", text)


class RedactFilter(logging.Filter):
    """日志脱敏 Filter：在 Formatter 之前改写消息内容。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = redact_cookie(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(
    level: int = logging.INFO,
    log_dir: str | Path = _DEFAULT_LOG_DIR,
    console: bool = True,
    logger_name: str = "ncm2bili",
) -> logging.Logger:
    """初始化 ncm2bili 日志系统（文档 §12）。

    Args:
        level: 控制台级别（文件始终全级别）。
        log_dir: 日志目录，默认项目下 logs/。
        console: 是否挂载控制台 handler。
        logger_name: 目标 logger；重复调用时先清理已有 handler，避免重复输出。
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    redact = RedactFilter()

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        console_handler.addFilter(redact)
        logger.addHandler(console_handler)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    filename = f"app_{datetime.now().strftime('%Y-%m-%d')}.log"
    file_handler = logging.FileHandler(log_path / filename, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # 文件全级别
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATEFMT))
    file_handler.addFilter(redact)
    logger.addHandler(file_handler)

    return logger


__all__ = ["RedactFilter", "redact_cookie", "setup_logging"]
