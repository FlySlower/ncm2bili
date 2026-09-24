"""review_server.py / review.html 单测（里程碑 M6，文档 §10.2/§10.3）。

覆盖：
- POST 合法 BV → manual.json 更新正确；两次连续 POST 文件不损坏；
- 非法 BV 返回 400 且不写文件；
- 服务仅监听 127.0.0.1（断言绑定地址）；
- F1-4（§10.3 写入侧安全）：一次性 token + Origin 同源 + song_key 长度上限；
- F1-4（§10.3 前端传参）：review.html 用 data-* 属性 + 事件委托，废除 onclick；
- review.html 生成：仅 MANUAL 歌曲、含搜索链接与输入框、不内嵌敏感信息；
- 端到端：保存到 manual.json 后，重跑 matcher 按优先级铁律生效。
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from db import Database
from matcher import Matcher
from report import write_review_html
from review_server import BV_RE, load_manual, serve_in_background, update_manual, write_manual_atomic

VALID_BV = "BV1AbCdEfGHi"  # 12 位：BV1 + 9 位字母数字
INVALID_BV = "BV123"        # 长度不足


def _post_json(
    server, path: str, payload: dict, *, token: str = "", origin: str = ""
) -> httpx.Response:
    """向本地测试服务发 JSON POST（httpx 同步即可）。

    F1-4（§10.3）：token/origin 非空时分别注入 X-Token / Origin 头。
    """
    port = server.server_address[1]
    headers: dict[str, str] = {}
    if token:
        headers["X-Token"] = token
    if origin:
        headers["Origin"] = origin
    with httpx.Client() as client:
        return client.post(
            f"http://127.0.0.1:{port}{path}", json=payload, headers=headers, timeout=5,
        )


# ---- 验收 1：合法 BV 保存 + 连续 POST 不损坏 ---------------------------


def test_server_saves_valid_bv_and_json_intact(tmp_path) -> None:
    manual_path = tmp_path / "manual.json"
    server, port, token = serve_in_background(manual_path)
    origin = f"http://127.0.0.1:{port}"
    try:
        r1 = _post_json(server, "/save", {"song_key": "夜曲|周杰伦", "bvid": VALID_BV},
                        token=token, origin=origin)
        r2 = _post_json(server, "/save", {"song_key": "晴天|周杰伦", "bvid": "BV1XxYyZz123"},
                        token=token, origin=origin)
    finally:
        server.shutdown()
        server.server_close()

    assert r1.status_code == 200 and r1.json()["ok"] is True
    assert r2.status_code == 200

    # 两次连续 POST 后 manual.json 仍为完整合法 JSON，两条记录都在
    data = json.loads(manual_path.read_text(encoding="utf-8"))
    assert data == {
        "夜曲|周杰伦": VALID_BV,
        "晴天|周杰伦": "BV1XxYyZz123",
    }


def test_server_preserves_existing_manual(tmp_path) -> None:
    """POST 前已有 manual.json 内容时，读-改-写不覆盖旧条目。"""
    manual_path = tmp_path / "manual.json"
    write_manual_atomic(manual_path, {"旧歌|旧艺人": "BV1Old"})

    server, port, token = serve_in_background(manual_path)
    origin = f"http://127.0.0.1:{port}"
    try:
        r = _post_json(server, "/save", {"song_key": "新歌|新艺人", "bvid": VALID_BV},
                       token=token, origin=origin)
    finally:
        server.shutdown()
        server.server_close()

    assert r.status_code == 200
    data = load_manual(manual_path)
    assert data["旧歌|旧艺人"] == "BV1Old"
    assert data["新歌|新艺人"] == VALID_BV


# ---- 验收 2：非法 BV 返回 400 且不写文件 ------------------------------


def test_server_rejects_invalid_bv(tmp_path) -> None:
    manual_path = tmp_path / "manual.json"
    server, port, token = serve_in_background(manual_path)
    origin = f"http://127.0.0.1:{port}"
    try:
        r = _post_json(server, "/save", {"song_key": "夜曲|周杰伦", "bvid": INVALID_BV},
                       token=token, origin=origin)
    finally:
        server.shutdown()
        server.server_close()

    assert r.status_code == 400
    assert not manual_path.exists()  # 未写文件


def test_server_rejects_missing_fields(tmp_path) -> None:
    manual_path = tmp_path / "manual.json"
    server, port, token = serve_in_background(manual_path)
    origin = f"http://127.0.0.1:{port}"
    try:
        r = _post_json(server, "/save", {"bvid": VALID_BV},  # 缺 song_key
                       token=token, origin=origin)
    finally:
        server.shutdown()
        server.server_close()

    assert r.status_code == 400
    assert not manual_path.exists()


def test_bv_regex() -> None:
    assert BV_RE.match("BV1AbCdEfGHi")
    assert not BV_RE.match("BV123")
    assert not BV_RE.match("av123456")
    assert not BV_RE.match("BV1AbCdEfGHiJ")  # 多一位


# ---- 验收 3：仅监听 127.0.0.1 -----------------------------------------


def test_server_binds_loopback_only(tmp_path) -> None:
    server, port, token = serve_in_background(tmp_path / "manual.json")
    try:
        host = server.server_address[0]
        assert host == "127.0.0.1"
    finally:
        server.shutdown()
        server.server_close()


def test_server_rejects_non_save_path(tmp_path) -> None:
    server, port, token = serve_in_background(tmp_path / "manual.json")
    try:
        r = _post_json(server, "/other", {"song_key": "a|b", "bvid": VALID_BV})
        assert r.status_code == 404
    finally:
        server.shutdown()
        server.server_close()


# ---- F1-4：写入侧安全（一次性 token + Origin + song_key 上限）----------


def test_server_rejects_missing_token(tmp_path) -> None:
    """F1-4（§10.3）：缺少 X-Token 头 → 403 且不写文件。"""
    manual_path = tmp_path / "manual.json"
    server, port, token = serve_in_background(manual_path)
    try:
        r = _post_json(
            server, "/save", {"song_key": "夜曲|周杰伦", "bvid": VALID_BV},
            origin=f"http://127.0.0.1:{port}",  # 有 Origin 但缺 token
        )
    finally:
        server.shutdown()
        server.server_close()

    assert r.status_code == 403
    assert not manual_path.exists()


def test_server_rejects_wrong_token(tmp_path) -> None:
    """F1-4（§10.3）：X-Token 不匹配 → 403 且不写文件。"""
    manual_path = tmp_path / "manual.json"
    server, port, token = serve_in_background(manual_path)
    try:
        r = _post_json(
            server, "/save", {"song_key": "夜曲|周杰伦", "bvid": VALID_BV},
            token="wrongtoken", origin=f"http://127.0.0.1:{port}",
        )
    finally:
        server.shutdown()
        server.server_close()

    assert r.status_code == 403
    assert not manual_path.exists()


def test_server_rejects_wrong_origin(tmp_path) -> None:
    """F1-4（§10.3）：Origin 非同源 → 403 且不写文件。"""
    manual_path = tmp_path / "manual.json"
    server, port, token = serve_in_background(manual_path)
    try:
        r = _post_json(
            server, "/save", {"song_key": "夜曲|周杰伦", "bvid": VALID_BV},
            token=token, origin="http://evil.example.com",
        )
    finally:
        server.shutdown()
        server.server_close()

    assert r.status_code == 403
    assert not manual_path.exists()


def test_server_rejects_missing_origin(tmp_path) -> None:
    """F1-4（§10.3）：无 Origin 头 → 403 且不写文件。"""
    manual_path = tmp_path / "manual.json"
    server, port, token = serve_in_background(manual_path)
    try:
        r = _post_json(
            server, "/save", {"song_key": "夜曲|周杰伦", "bvid": VALID_BV},
            token=token,  # 有 token 但缺 Origin
        )
    finally:
        server.shutdown()
        server.server_close()

    assert r.status_code == 403
    assert not manual_path.exists()


def test_server_rejects_oversized_song_key(tmp_path) -> None:
    """F1-4（§10.3）：song_key 超过 300 字符 → 400 且不写文件。"""
    manual_path = tmp_path / "manual.json"
    server, port, token = serve_in_background(manual_path)
    origin = f"http://127.0.0.1:{port}"
    try:
        r = _post_json(
            server, "/save", {"song_key": "A" * 301, "bvid": VALID_BV},
            token=token, origin=origin,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert r.status_code == 400
    assert not manual_path.exists()


# ---- 验收 4：review.html 生成 -----------------------------------------


def _manual_song(song_key: str, name: str, artist: str) -> dict:
    return {
        "song_key": song_key,
        "name": name,
        "artist": artist,
        "status": "MANUAL",
        "method": "MANUAL",
        "bvid": None,
        "fail_reason": "匹配失败",
    }


def test_review_html_only_manual_songs(tmp_path) -> None:
    """仅 MANUAL 歌曲入列表；DONE 歌曲不出现。"""
    rows = [
        _manual_song("夜曲|周杰伦", "夜曲", "周杰伦"),
        {"song_key": "晴天|周杰伦", "name": "晴天", "artist": "周杰伦",
         "status": "DONE", "method": "SCORED", "bvid": "BV1done", "fail_reason": None},
    ]
    path = write_review_html(
        rows, tmp_path / "review.html",
        save_url="http://127.0.0.1:8080/save", token="mytoken123",
    )

    text = path.read_text(encoding="utf-8")
    assert "夜曲|周杰伦" in text
    assert "晴天|周杰伦" not in text  # DONE 不进回灌页
    assert "B 站搜索" in text
    expected_url = "https://search.bilibili.com/all?keyword=%E5%A4%9C%E6%9B%B2%20%E5%91%A8%E6%9D%B0%E4%BC%A6"
    assert f'href="{expected_url}"' in text
    # F1-4（§10.3）：data-* 属性 + 事件委托，废除 onclick
    assert 'data-song-key=' in text
    assert 'onclick=' not in text  # 无 onclick HTML 属性拼接
    assert "addEventListener" in text  # 事件委托
    # token 注入页面 JS
    assert "mytoken123" in text
    assert "X-Token" in text
    assert "http://127.0.0.1:8080/save" in text


def test_review_html_shows_fav_failed_songs(tmp_path) -> None:
    """文档 §10.2：FAV_FAILED 歌曲与 MANUAL 同列入回灌页，行内展示 fail_reason。"""
    rows = [
        _manual_song("夜曲|周杰伦", "夜曲", "周杰伦"),
        {"song_key": "晴天|周杰伦", "name": "晴天", "artist": "周杰伦",
         "status": "FAV_FAILED", "method": "SCORED", "bvid": "BV1ff",
         "fail_reason": "收藏失败（code -403）"},
    ]
    path = write_review_html(rows, tmp_path / "review.html", save_url=None)

    text = path.read_text(encoding="utf-8")
    assert "晴天|周杰伦" in text  # FAV_FAILED 入列
    assert "收藏失败（code -403）" in text  # 行内展示 fail_reason
    assert "夜曲|周杰伦" in text  # MANUAL 仍然入列


def test_review_html_no_sensitive_data(tmp_path) -> None:
    """review.html 不内嵌 cookie/凭证（文档 §10.3：不暴露任何歌单/cookie 数据）。"""
    rows = [_manual_song("夜曲|周杰伦", "夜曲", "周杰伦")]
    path = write_review_html(rows, tmp_path / "review.html", save_url=None, token="sometoken")

    text = path.read_text(encoding="utf-8")
    for secret in ("SESSDATA", "bili_jct", "buvid3", "Cookie", "credentials"):
        assert secret not in text
    assert "未启动" in text  # save_url=None 时提示服务未启动


def test_review_html_static_open_save_unavailable_hint(tmp_path) -> None:
    """B2 补测（F1-4）：不传 save_url 生成的页面可静态打开——

    1) 页面含"保存不可用"提示文案；
    2) 内嵌 script 保留 fetch 失败的 catch 分支（静态文件双击打开时
       fetch 必然失败，用户看到"无法连接保存服务"而非无响应）。静态断言，
       不起浏览器。
    """
    rows = [_manual_song("夜曲|周杰伦", "夜曲", "周杰伦")]
    path = write_review_html(rows, tmp_path / "review.html")  # save_url=None

    text = path.read_text(encoding="utf-8")
    assert "保存不可用" in text
    # catch 分支与用户可见的失败提示均在
    assert "} catch (e) {" in text
    assert "无法连接保存服务" in text
    # SAVE_URL 为空串（fetch("") 走 catch，而非误以为已配置服务）
    assert 'const SAVE_URL = "";' in text


# ---- 验收 5：端到端 —— manual 按优先级铁律生效 -------------------------


@pytest.mark.asyncio
@respx.mock
async def test_manual_saved_via_server_applies_on_rerun(tmp_path) -> None:
    """保存到 manual.json 后，重跑 matcher：manual 最高优先级，直接命中不搜索。"""
    manual_path = tmp_path / "manual.json"

    # 模拟用户在 review 页面保存 BV
    update_manual(manual_path, "夜曲|周杰伦", VALID_BV)

    db = Database(tmp_path / "cache.db")
    config = _make_config()
    bili, ncm, http = _make_clients()

    # 若 manual 优先级失效误发搜索，会打到未 mock 的 SEARCH_URL → 测试失败
    matcher = Matcher(
        config, db, bili, ncm, http,
        manual=_load_manual(manual_path),
    )
    async with matcher:
        song = {"ncm_id": 1, "name": "夜曲", "artist": "周杰伦",
                "alia": [], "origin": None}
        result = await matcher.match_song(song)

    assert result["method"] == "MANUAL"
    assert result["bvid"] == VALID_BV
    row = db.query_one("SELECT status, method, bvid FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "MATCHED"  # F2-1：matcher 正式语义置 MATCHED（收藏成功才 DONE）
    assert row["method"] == "MANUAL"
    assert row["bvid"] == VALID_BV
    # 无任何搜索请求（manual 最高优先级，不触发搜索）
    assert respx.calls == []


def _make_config():
    from config import Config

    cfg = Config()
    # F1-1：搜索 sleep 下沉到 _search_cached，测试归零限速避免拖慢
    cfg.rate_limit.search.interval_ms = 0
    cfg.rate_limit.search.jitter_ms = [0, 0]
    return cfg


def _make_clients():
    import httpx as hx

    from bili_search import BiliSearchClient
    from ncm import NcmClient

    return BiliSearchClient(hx.AsyncClient()), NcmClient(hx.AsyncClient()), hx.AsyncClient()


def _load_manual(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
