"""review.html 的本地保存服务（里程碑 M6，文档 §10.3）。

- 仅监听 127.0.0.1 随机端口（localhost only，不暴露公网）；
- 仅处理 POST /save，body 为 {"song_key", "bvid"}；
- BV 号格式校验 ^BV1[a-zA-Z0-9]{9}$，不合法返回 400 且不写文件；
- 写入采用"读-改-写 + 临时文件 + os.replace"原子替换，
  防并发写损坏（文档 §10.3）；
- 服务不读取/返回任何歌单与 cookie 数据，仅接收 BV 号。
- F1-4（§10.3 写入侧安全）：启动时生成一次性 token 注入页面，
  POST /save 必须携带 X-Token 头且校验通过；校验 Origin 头同源
  （仅接受 http://127.0.0.1:<port>）；song_key 长度设上限防滥用。

用法：
    from review_server import start_review_server
    server, port, token = start_review_server("manual.json")
    # server.serve_forever() 阻塞；或 serve_in_background() 起后台线程
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 文档 §10.3：BV 号格式校验
BV_RE = re.compile(r"^BV1[a-zA-Z0-9]{9}$")

# F1-4（§10.3）：song_key 长度上限（防滥用写入）
_SONG_KEY_MAX_LEN = 300

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
    """POST /save + GET /review.html 处理器；manual_path、db、token、
    review_html_path 由 start_review_server 注入。

    V5-P0-1（§10.3 意图）：review_server 托管 review.html 页面本身——
    用户从浏览器打开 http://127.0.0.1:<port>/review.html?token=<token> 即同源，
    POST 的 Origin 校验安全意图完整保留，file:// 跨源 403/501 阻断同时消失。
    """

    manual_path: Path = Path(_MANUAL_FILENAME)
    db: Any = None  # 可选：同步写 whitelist_bv 表（source=manual，文档 §6）
    token: str = ""  # F1-4（§10.3）：一次性 token，由 start_review_server 注入
    review_html_path: Path | None = None  # V5-P0-1：托管的 review.html 路径
    server_version = "review-server/1.0"

    def _expected_origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    # ---- POST /save：保存 BV 到 manual.json（文档 §10.3）----------------

    def do_POST(self) -> None:  # noqa: N802 - http.server 协议方法名
        if self.path != "/save":
            self._reply(404, {"error": "not found"})
            return

        # F1-4（§10.3）：Origin 同源校验（仅接受 http://127.0.0.1:<port>）
        origin = self.headers.get("Origin", "")
        if origin != self._expected_origin():
            self._reply(403, {"error": "forbidden origin"})
            return

        # F1-4（§10.3）：一次性 token 校验（POST 必须携带 X-Token 头）
        x_token = self.headers.get("X-Token", "")
        if not x_token or x_token != self.token:
            self._reply(403, {"error": "invalid token"})
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
        # F1-4（§10.3）：song_key 长度上限（超长直接拒绝，防滥用写入）
        if (
            not isinstance(song_key, str)
            or not song_key
            or len(song_key) > _SONG_KEY_MAX_LEN
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

    # ---- GET /review.html：托管页面（V5-P0-1，§10.3 意图）----------------
    # 页面 URL 带 token 查询参数（与 POST 的 X-Token 校验同一套 token），
    # 用户从浏览器打开即同源，file:// 的 Origin:null / 无 Origin / OPTIONS
    # 三个 403/501 阻断同时消除。

    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        if parsed.path != "/review.html":
            self._reply(404, {"error": "not found"})
            return

        # V5-P0-1（§10.3）：token 查询参数校验（与 POST X-Token 同一 token）
        params = parse_qs(parsed.query)
        token_param = params.get("token", [""])[0]
        if not token_param or token_param != self.token:
            self._reply(403, {"error": "invalid token"})
            return

        if self.review_html_path is None or not self.review_html_path.exists():
            self._reply(404, {"error": "review.html not found"})
            return

        data = self.review_html_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- OPTIONS：CORS 预检（V5-P0-1，同源限定）-------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        """CORS 预检：仅同源放行（file:// 跨源不返回 Access-Control 头）。"""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", self._expected_origin())
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.end_headers()

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
    review_html_path: str | Path | None = None,
) -> tuple[ThreadingHTTPServer, int, str]:
    """启动本地保存服务，返回 (server, 实际端口, 一次性 token)。

    port=0 → 随机端口（文档 §10.3）。仅监听 host（默认 127.0.0.1）。
    db: 可选 Database 实例，保存时同步写 whitelist_bv 表（source=manual）。
    review_html_path: V5-P0-1（§10.3 意图）——托管的 review.html 路径；
        GET /review.html?token=<token> 返回该文件内容。None 时不托管页面。
    F1-4（§10.3）：启动时生成一次性 token 注入 handler，返回供页面嵌入。
    调用方负责 server.shutdown() / server.server_close()。
    """
    token = secrets.token_hex(16)  # F1-4（§10.3）：32 字符十六进制一次性 token
    handler = type(
        "BoundReviewHandler",
        (ReviewHandler,),
        {
            "manual_path": Path(manual_path),
            "db": db,
            "token": token,
            "review_html_path": Path(review_html_path) if review_html_path else None,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    return server, server.server_address[1], token


def serve_in_background(
    manual_path: str | Path = _MANUAL_FILENAME,
    *,
    host: str = _HOST,
    port: int = 0,
    db: Any = None,
    review_html_path: str | Path | None = None,
) -> tuple[ThreadingHTTPServer, int, str]:
    """后台线程启动服务（测试/命令行用），返回 (server, 实际端口, token)。"""
    server, actual_port, token = start_review_server(
        manual_path, host=host, port=port, db=db,
        review_html_path=review_html_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, actual_port, token


__all__ = [
    "BV_RE",
    "ReviewHandler",
    "load_manual",
    "serve_in_background",
    "start_review_server",
    "update_manual",
    "write_manual_atomic",
]
