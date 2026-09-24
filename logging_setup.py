"""日志初始化模块（里程碑 M0，对应文档 §12）。

- 控制台：INFO+，简洁格式；
- 文件：logs/app_YYYY-MM-DD.log，全级别，含时间/模块/级别；
- 脱敏铁律：日志 Filter 在 Formatter 之前把 SESSDATA / bili_jct / buvid3
  的 cookie 值统一替换为 <redacted>，DEBUG 级也不例外（文档 §12）。
- F3-4（§9.4/§12）：脱敏同时覆盖普通消息与异常堆栈——record.exc_info
  预先格式化为脱敏后的 exc_text（Formatter 优先复用 exc_text 缓存），
  stack_info 同步脱敏；任何日志/异常堆栈不得输出完整 cookie。
"""
from __future__ import annotations

import logging
import re
import traceback
from datetime import datetime
from pathlib import Path

# F3-5（§11.4）：默认日志目录基于项目根解析，与 CWD 无关
_PROJECT_ROOT = Path(__file__).resolve().parent

# cookie 名大小写不敏感，值匹配到分号或空白为止（保留结尾分号）
_COOKIE_RE = re.compile(r"(SESSDATA|bili_jct|buvid3)=([^;\s]+)", re.IGNORECASE)

_DEFAULT_LOG_DIR = _PROJECT_ROOT / "logs"
_CONSOLE_FORMAT = "%(levelname)s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def redact_cookie(text: str) -> str:
    """把 cookie 值替换为 <redacted>，返回处理后的文本。"""
    return _COOKIE_RE.sub(r"\1=<redacted>", text)


class RedactFilter(logging.Filter):
    """日志脱敏 Filter：在 Formatter 之前改写消息内容。

    F3-4（§9.4/§12）：覆盖三条输出路径——
    1. 普通消息 record.msg/args（cookie 出现在日志文本中）；
    2. 异常堆栈 record.exc_info：此处预先格式化为 exc_text 并脱敏；
       logging.Formatter.format() 在 exc_text 非空时直接复用（不再自行
       traceback.formatException），故控制台/文件输出均为脱敏文本；
    3. record.stack_info（logger.error(..., stack_info=True) 的栈帧转储）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = redact_cookie(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        if record.exc_info:
            # 预格式化并缓存到 exc_text：Formatter 检测到 exc_text 非空会原样复用
            record.exc_text = redact_cookie(
                "".join(traceback.format_exception(*record.exc_info))
            )
        elif record.exc_text:
            record.exc_text = redact_cookie(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_cookie(record.stack_info)
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
