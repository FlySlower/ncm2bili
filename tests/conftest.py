"""pytest 公共 fixture（文档 §13：mock 铁律基础）。

CLI 测试调用 main() 会触发 setup_logging（文档 §12），把 root logger 提到
DEBUG 并挂 file handler（指向已被 pytest 清理的 tmp 目录）。autouse
fixture 在每个测试后还原 root logger 全局状态，避免跨用例污染
（httpcore DEBUG 日志写已关闭文件导致连接中止）。
"""
from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_root_logger() -> None:
    """每个测试后还原 root logger 的 level/handlers/propagate。"""
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    saved_propagate = root.propagate
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            handler.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    root.propagate = saved_propagate
