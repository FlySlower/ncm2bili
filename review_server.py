"""review.html 的本地保存服务（里程碑 M6，文档 §10.3）。

- 仅监听 127.0.0.1 随机端口（localhost only，不暴露公网）；
- 仅处理 POST /save，body 为 {"song_key", "bvid"}；
- BV 号格式校验 ^BV1[a-zA-Z0-9]{9}$，不合法返回 400 且不写文件；
- 写入 manual.json 采用"读-改-写 + 临时文件 + os.replace"原子替换，
  防并发写损坏（文档 §10.3）；
- 服务不读取/返回任何歌单与 cookie 数据，仅接收 BV 号。

用法：
    from review_server import start_review_server
    server, port = start_review_server("manual.json")
    # server.serve_forever() 阻塞；或 serve_in_background() 起后台线程
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 文档 §10.3：BV 号格式校验
BV_RE = re.compile(r"^BV1[a-zA-Z0-9]{9}$")

_HOST = "127.0.0.1"  # 仅回环地址（文档 §10.3）
_MANUAL_FILENAME = "manual.json"


def load_manual(path: str | Path) -> dict:
    """读取 manual.json；缺失、为空或损坏时回退到空 dict。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def write_manual_atomic(path: str | Path, manual: dict) -> None:
    """原子写 manual.json：临时文件 + os.replace，防并发写损坏（文档 §10.3）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".manual-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manual, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def update_manual(path: str | Path, song_key: str, bvid: str) -> dict:
    """读-改-写 manual.json：写入 {song_key: bvid}，返回更新后的完整 dict。"""
    manual = load_manual(path)
    manual[song_key] = bvid
    write_manual_atomic(path, manual)
    return manual


class ReviewHandler(BaseHTTPRequestHandler):
    """POST /save 处理器；manual_path 与 db 由 start_review_server 注入。"""

    manual_path: Path = Path(_MANUAL_FILENAME)
    db: Any = None  # 可选：同步写 whitelist_bv 表（source=manual，文档 §6）
    server_version = "review-server/1.0"

    # ---- 路由 -----------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 - http.server 协议方法名
        if self.path != "/save":
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._reply(400, {"error": "invalid json body"})
            return

        song_key = body.get("song_key")
        bvid = body.get("bvid")
        if (
            not isinstance(song_key, str)
            or not song_key
            or not isinstance(bvid, str)
            or not BV_RE.match(bvid)
        ):
            self._reply(400, {"error": "invalid song_key or bvid"})
            return

        update_manual(self.manual_path, song_key, bvid)
        # 文档 §6：manual 保存同步写 whitelist_bv 表（source=manual），
        # 使表成为优先级统一查询入口
        if self.db is not None:
            self.db.upsert_whitelist_bv(song_key, bvid, source="manual")
        self._reply(200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        self._reply(404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - http.server 签名
        """静默访问日志（仅回环本地工具，无需输出）。"""

    # ---- 工具 -----------------------------------------------------

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_review_server(
    manual_path: str | Path = _MANUAL_FILENAME,
    *,
    host: str = _HOST,
    port: int = 0,
    db: Any = None,
) -> tuple[ThreadingHTTPServer, int]:
    """启动本地保存服务，返回 (server, 实际端口)。

    port=0 → 随机端口（文档 §10.3）。仅监听 host（默认 127.0.0.1）。
    db: 可选 Database 实例，保存时同步写 whitelist_bv 表（source=manual）。
    调用方负责 server.shutdown() / server.server_close()。
    """
    handler = type(
        "BoundReviewHandler",
        (ReviewHandler,),
        {"manual_path": Path(manual_path), "db": db},
    )
    server = ThreadingHTTPServer((host, port), handler)
    return server, server.server_address[1]


def serve_in_background(
    manual_path: str | Path = _MANUAL_FILENAME,
    *,
    host: str = _HOST,
    port: int = 0,
    db: Any = None,
) -> tuple[ThreadingHTTPServer, int]:
    """后台线程启动服务（测试/命令行用），返回 (server, 实际端口)。"""
    server, actual_port = start_review_server(manual_path, host=host, port=port, db=db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, actual_port


__all__ = [
    "BV_RE",
    "ReviewHandler",
    "load_manual",
    "serve_in_background",
    "start_review_server",
    "update_manual",
    "write_manual_atomic",
]
