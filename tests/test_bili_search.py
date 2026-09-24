"""bili_search.py 单测（里程碑 M2）：WBI 签名测试向量来自 bilibili-API-collect 公开文档。

docs/misc/sign/wbi.md 公开向量：
- img_key=653657f524a547ac981ded72ea172057, sub_key=6e4909c702f846728e64f6007736a338
  → mixin_key=72136226c6a73669787ee4fd02a74c27
- params={bar:514, foo:114, zab:1919810}, wts=1684746387
  → w_rid=90efcab09403023875b8516f07e9f9de
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from bili_search import BiliSearchClient, get_mixin_key, sign_wbi

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
HOME_URL = "https://www.bilibili.com/"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

# ---- 公开测试向量 --------------------------------------------------

IMG_KEY = "653657f524a547ac981ded72ea172057"
SUB_KEY = "6e4909c702f846728e64f6007736a338"
EXPECTED_MIXIN_KEY = "72136226c6a73669787ee4fd02a74c27"
WTS = 1684746387
EXPECTED_W_RID = "90efcab09403023875b8516f07e9f9de"


def _nav_response(img_key: str = IMG_KEY, sub_key: str = SUB_KEY) -> dict:
    return {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{img_key}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{sub_key}.png",
            }
        },
    }


def _nav_unauthed_response(img_key: str = IMG_KEY, sub_key: str = SUB_KEY) -> dict:
    """nav 未登录（code=-101）但 data.wbi_img 照常返回（真实环境行为）。"""
    wbi_img = _nav_response(img_key, sub_key)["data"]["wbi_img"]
    return {"code": -101, "message": "账号未登录", "data": {"isLogin": False, "wbi_img": wbi_img}}


def _home_response(buvid3: str = "XUPPER-1234-ABCD-EFGH-567890") -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "set-cookie": (
                f"buvid3={buvid3}; Path=/; Domain=.bilibili.com; Expires=Wed, 21 Sep 2033 00:00:00 GMT; "
                "buvid4=other-value; Path=/"
            )
        },
    )


def _spi_response(b_3: str = "SPI-B3-000000000000000000000000", b_4: str = "SPI-B4-0000") -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "data": {"b_3": b_3, "b_4": b_4}})


# ---- 验收 1：mixin_key 生成向量 ------------------------------------


def test_mixin_key_matches_public_vector() -> None:
    """mixin_key = 按 MIXIN_KEY_ENC_TAB 重排 img_key+sub_key 后截取前 32 位。"""
    assert get_mixin_key(IMG_KEY + SUB_KEY) == EXPECTED_MIXIN_KEY


# ---- 验收 2：完整 w_rid 向量 ---------------------------------------


def test_full_wbi_sign_matches_public_vector() -> None:
    """完整签名：固定 wts 下 w_rid 与公开向量一致。"""
    signed = sign_wbi(
        {"bar": "514", "foo": "114", "zab": 1919810},
        IMG_KEY,
        SUB_KEY,
        wts=WTS,
    )
    assert signed["wts"] == str(WTS)  # 公开实现中所有参数值转为字符串
    assert signed["w_rid"] == EXPECTED_W_RID
    # 排序后 query 应等于文档第 4/5 步拼接结果
    assert (
        httpx.QueryParams({k: str(v) for k, v in signed.items() if k != "w_rid"}).__str__()
        == "bar=514&foo=114&wts=1684746387&zab=1919810"
    )


# ---- 验收 5：参数含 !'()* 时处理正确（公开实现为过滤） ----------------


def test_special_chars_filtered_in_sign() -> None:
    """值中的 !'()* 被过滤后参与签名，与不含这些字符的等价参数签名一致。"""
    a = sign_wbi({"keyword": "a!b'c(d)e*f", "page": 1}, IMG_KEY, SUB_KEY, wts=WTS)
    b = sign_wbi({"keyword": "abcdef", "page": 1}, IMG_KEY, SUB_KEY, wts=WTS)
    assert a["w_rid"] == b["w_rid"]
    assert a["keyword"] == "abcdef"


# ---- 验收 4：buvid3 获取（主路径 SPI + 降级主页 + 持久化复用）--------


@pytest.mark.asyncio
@respx.mock
async def test_buvid3_main_path_from_spi(tmp_path) -> None:
    """主路径：SPI 正常返回时取 data.b_3 正确，且不访问主页。"""
    respx.get(SPI_URL).mock(return_value=_spi_response(b_3="SPI-B3-AAA"))
    search_resp = {"code": 0, "data": {"result": []}}

    captured: dict[str, str] = {}

    def search_handler(request: httpx.Request) -> httpx.Response:
        captured["cookie"] = request.headers.get("cookie", "")
        return httpx.Response(200, json=search_resp)

    respx.get(SEARCH_URL).mock(side_effect=search_handler)
    respx.get(NAV_URL).mock(return_value=httpx.Response(200, json=_nav_response()))

    from db import Database

    db = Database(tmp_path / "test.db")
    client = BiliSearchClient(httpx.AsyncClient(), db=db)
    async with client:
        buvid3 = await client.ensure_buvid3()
        await client.search_videos("测试")

    assert buvid3 == "SPI-B3-AAA"
    assert "buvid3=SPI-B3-AAA" in captured["cookie"]
    # 主路径成功时不得访问主页
    home_calls = [c for c in respx.calls if str(c.request.url).startswith(HOME_URL)]
    assert home_calls == []
    # 持久化到 credentials 表，重跑可直接复用
    row = db.query_one("SELECT value FROM credentials WHERE key = 'buvid3'")
    assert row is not None and row["value"] == "SPI-B3-AAA"


@pytest.mark.asyncio
@respx.mock
async def test_buvid3_fallback_to_home_set_cookie() -> None:
    """降级路径：SPI 返回 code != 0 时回退主页 Set-Cookie。"""
    respx.get(SPI_URL).mock(
        return_value=httpx.Response(200, json={"code": -403, "message": "非法访问"})
    )
    respx.get(HOME_URL).mock(return_value=_home_response("HOME-FALLBACK-B3"))

    client = BiliSearchClient(httpx.AsyncClient())
    async with client:
        buvid3 = await client.ensure_buvid3()

    assert buvid3 == "HOME-FALLBACK-B3"


@pytest.mark.asyncio
@respx.mock
async def test_buvid3_all_paths_fail_raises() -> None:
    """两条路径都失败时抛带指引的异常，而非返回 None。"""
    respx.get(SPI_URL).mock(
        return_value=httpx.Response(200, json={"code": -403, "message": "非法访问"})
    )
    # 主页也不下发 buvid3
    respx.get(HOME_URL).mock(
        return_value=httpx.Response(200, headers={"set-cookie": "foo=bar; Path=/"})
    )

    client = BiliSearchClient(httpx.AsyncClient())
    async with client:
        with pytest.raises(Exception) as exc_info:
            await client.ensure_buvid3()

    msg = str(exc_info.value)
    assert "buvid3" in msg and "UA" in msg


@pytest.mark.asyncio
@respx.mock
async def test_buvid3_reused_from_persisted_db(tmp_path) -> None:
    """已有持久化 buvid3 时跳过获取，断言 SPI/主页零请求。"""
    from db import Database

    db = Database(tmp_path / "test.db")
    db.execute(
        "INSERT INTO credentials (key, value, updated_at) VALUES (?, ?, ?)",
        ("buvid3", "PERSISTED-B3-123", 123),
    )

    client = BiliSearchClient(httpx.AsyncClient(), db=db)
    async with client:
        buvid3 = await client.ensure_buvid3()

    assert buvid3 == "PERSISTED-B3-123"
    assert respx.calls == []  # 零请求


# ---- F4-4（§5.2 第 4 条）：buvid3 持久化到 credentials.db --------------


@pytest.mark.asyncio
@respx.mock
async def test_buvid3_persisted_to_credentials_db_not_cache_db(tmp_path) -> None:
    """F4-4：注入独立 cred_db 后，buvid3 落 credentials.db，cache.db 不落 buvid3。

    wbi key 仍存 cache.db（kv_meta），两库职责按文档 §3.1/§5.2 分离。
    """
    from db import Database

    cache_db = Database(tmp_path / "cache.db")
    cred_db = Database(tmp_path / "credentials.db")

    respx.get(SPI_URL).mock(return_value=_spi_response(b_3="CRED-DB-B3"))
    respx.get(NAV_URL).mock(return_value=httpx.Response(200, json=_nav_response()))
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"result": []}})
    )

    client = BiliSearchClient(httpx.AsyncClient(), db=cache_db, cred_db=cred_db)
    async with client:
        buvid3 = await client.ensure_buvid3()
        await client.search_videos("测试")  # 触发 wbi key 落 cache.db

    assert buvid3 == "CRED-DB-B3"
    # buvid3 在 credentials.db
    cred_row = cred_db.query_one(
        "SELECT value FROM credentials WHERE key = 'buvid3'"
    )
    assert cred_row is not None and cred_row["value"] == "CRED-DB-B3"
    # 不在 cache.db
    assert cache_db.query_one(
        "SELECT value FROM credentials WHERE key = 'buvid3'"
    ) is None
    # wbi key 仍在 cache.db 的 kv_meta
    wbi_row = cache_db.query_one("SELECT value FROM kv_meta WHERE key = 'wbi_keys'")
    assert wbi_row is not None


@pytest.mark.asyncio
@respx.mock
async def test_buvid3_reused_from_credentials_db_zero_spi(tmp_path) -> None:
    """F4-4 复用：buvid3 已在 credentials.db 时，重跑零 SPI/主页请求。"""
    from db import Database

    cache_db = Database(tmp_path / "cache.db")
    cred_db = Database(tmp_path / "credentials.db")
    cred_db.execute(
        "INSERT INTO credentials (key, value, updated_at) VALUES (?, ?, ?)",
        ("buvid3", "CRED-PERSISTED-B3", 123),
    )

    spi_route = respx.get(SPI_URL).mock(return_value=_spi_response())
    respx.get(HOME_URL).mock(return_value=_home_response())

    client = BiliSearchClient(httpx.AsyncClient(), db=cache_db, cred_db=cred_db)
    async with client:
        buvid3 = await client.ensure_buvid3()

    assert buvid3 == "CRED-PERSISTED-B3"
    assert spi_route.call_count == 0  # 重跑零 SPI 请求
    home_calls = [c for c in respx.calls if str(c.request.url).startswith(HOME_URL)]
    assert home_calls == []  # 同样零主页请求


# ---- 验收 3：-403 时刷新 key 并重试一次 ----------------------------


@pytest.mark.asyncio
@respx.mock
async def test_403_refresh_keys_and_retry_once() -> None:
    """搜索返回 -403 → 强制刷新 nav key → 重试一次（nav 共调 2 次）。"""
    respx.get(SPI_URL).mock(return_value=_spi_response())

    nav_count = {"n": 0}
    new_img = "a" * 32
    new_sub = "b" * 32

    def nav_handler(request: httpx.Request) -> httpx.Response:
        nav_count["n"] += 1
        if nav_count["n"] == 1:
            return httpx.Response(200, json=_nav_response())  # 旧 key
        return httpx.Response(200, json=_nav_response(new_img, new_sub))  # 新 key

    respx.get(NAV_URL).mock(side_effect=nav_handler)

    search_count = {"n": 0}

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_count["n"] += 1
        if search_count["n"] == 1:
            return httpx.Response(200, json={"code": -403, "message": "非法访问"})
        return httpx.Response(200, json={"code": 0, "data": {"result": [{"bvid": "BV1xx"}]}})

    respx.get(SEARCH_URL).mock(side_effect=search_handler)

    client = BiliSearchClient(httpx.AsyncClient())
    async with client:
        data = await client.search_videos("测试")

    assert nav_count["n"] == 2          # 初始取 key + 强制刷新
    assert search_count["n"] == 2       # -403 + 重试成功
    assert data["result"][0]["bvid"] == "BV1xx"


# ---- WBI key 缓存：内存 + kv_meta 落盘 ------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_wbi_keys_cached_in_memory_and_db(tmp_path) -> None:
    """img_key/sub_key 先存 kv_meta，新实例从库恢复不再调 nav。"""
    from db import Database

    db = Database(tmp_path / "test.db")
    respx.get(HOME_URL).mock(return_value=_home_response())

    nav_count = {"n": 0}

    def nav_handler(request: httpx.Request) -> httpx.Response:
        nav_count["n"] += 1
        return httpx.Response(200, json=_nav_response())

    respx.get(NAV_URL).mock(side_effect=nav_handler)

    client = BiliSearchClient(httpx.AsyncClient(), db=db)
    async with client:
        img_key, sub_key = await client._ensure_wbi_keys()
    assert (img_key, sub_key) == (IMG_KEY, SUB_KEY)

    row = db.query_one("SELECT value, fetched_at FROM kv_meta WHERE key = 'wbi_keys'")
    assert row is not None
    assert json.loads(row["value"]) == {"img_key": IMG_KEY, "sub_key": SUB_KEY}
    assert row["fetched_at"] > 0

    # 新实例从 kv_meta 恢复，不触发 nav
    client2 = BiliSearchClient(httpx.AsyncClient(), db=db)
    async with client2:
        img2, sub2 = await client2._ensure_wbi_keys()
    assert (img2, sub2) == (IMG_KEY, SUB_KEY)
    assert nav_count["n"] == 1


# ---- nav 未登录（code=-101）仍可取 wbi_img ---------------------------


@pytest.mark.asyncio
@respx.mock
async def test_wbi_keys_ok_when_nav_unauthed() -> None:
    """真实环境：nav 未登录返回 -101 但 data.wbi_img 照常，签名仍可用。"""
    respx.get(SPI_URL).mock(return_value=_spi_response())
    respx.get(NAV_URL).mock(
        return_value=httpx.Response(200, json=_nav_unauthed_response())
    )
    search_resp = {"code": 0, "data": {"result": [{"bvid": "BV1xx"}]}}
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=search_resp))

    client = BiliSearchClient(httpx.AsyncClient())
    async with client:
        data = await client.search_videos("测试")

    assert data["result"][0]["bvid"] == "BV1xx"


# ===================================================================
# v0.4.1 P1a：熔断器兼容 WAF 412（文档 §7 响应层 b）
# ===================================================================


@pytest.mark.asyncio
@respx.mock
async def test_waf_html_412_counts_toward_breaker() -> None:
    """HTTP 412 + text/html（openresty，无 JSON body）→ 按 -412 计入熔断，×3 触发全局暂停。

    修复前：熔断器只认 body 的 API code -412，WAF 412 永不计数（16:12 实录连发无效）。
    """
    from bili_search import BiliError
    from circuit_breaker import CircuitBreaker

    respx.get(SPI_URL).mock(return_value=_spi_response())
    respx.get(NAV_URL).mock(return_value=httpx.Response(200, json=_nav_response()))
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            412, text="<html>openresty deny</html>",
            headers={"content-type": "text/html", "server": "openresty"},
        )
    )

    breaker = CircuitBreaker()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    breaker._sleep = fake_sleep

    client = BiliSearchClient(
        httpx.AsyncClient(), breaker=breaker, retry_delays_s=[0.0, 0.0, 0.0]
    )
    async with client:
        with pytest.raises(BiliError):
            await client.search_videos("测试")

    # F4-2：搜索侧与收藏侧统一为初次 + 3 次重试 = 共 4 次尝试
    assert breaker.recent_failures == 4  # 每次 WAF 412（含重试）都计数
    assert breaker.is_paused  # 达 warn 阈值 3 次 → 全局暂停
    assert any(s >= 60 for s in sleeps), f"未观察到 60s 全局暂停，sleeps={sleeps}"


# ===================================================================
# v0.4.1 P1b：重试重新签名 + 退避生效（文档 §9.1）
# ===================================================================


@pytest.mark.asyncio
@respx.mock
async def test_retry_recomputes_wbi_signature(monkeypatch) -> None:
    """重试每次重新生成 wts/w_rid（签名绑死 wts，原样重发对 412 无效，§9.1）。"""
    import bili_search

    tick = {"t": 1_000_000}
    monkeypatch.setattr(bili_search.time, "time", lambda: tick["t"])

    respx.get(SPI_URL).mock(return_value=_spi_response())
    respx.get(NAV_URL).mock(return_value=httpx.Response(200, json=_nav_response()))

    sigs: list[str] = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        sigs.append(request.url.params["w_rid"])
        if len(sigs) == 1:
            tick["t"] += 5  # 时间推进 → 下次尝试 wts 变化 → w_rid 必然不同
            return httpx.Response(500, text="boom")  # HTTP 错误触发重试
        return httpx.Response(200, json={"code": 0, "data": {"result": [{"bvid": "BV1x"}]}})

    respx.get(SEARCH_URL).mock(side_effect=search_handler)

    client = BiliSearchClient(httpx.AsyncClient(), retry_delays_s=[0.0, 0.0])
    async with client:
        data = await client.search_videos("测试")

    assert data["result"][0]["bvid"] == "BV1x"
    assert len(sigs) == 2
    assert sigs[0] != sigs[1]  # 两次尝试 w_rid 不同（重新签名）


@pytest.mark.asyncio
@respx.mock
async def test_waf_412_backoff_delays_applied(monkeypatch) -> None:
    """F4-2（§7 响应层 a）：搜索侧重试耗尽共 4 次尝试，退避 2s→4s→8s 三档全可达。

    尝试次数按 retry_delays_s 长度推导（初次 + len(delays) 次重试），
    消除旧实现 retries=2 时"8s 永不使用"的死档；实录曾 ~1s 无效退避。
    """
    from bili_search import BiliError

    respx.get(SPI_URL).mock(return_value=_spi_response())
    respx.get(NAV_URL).mock(return_value=httpx.Response(200, json=_nav_response()))
    search_route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(412, text="<html>waf</html>",
                                    headers={"content-type": "text/html"})
    )

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("bili_search.sleep_before_retry", fake_sleep)

    client = BiliSearchClient(httpx.AsyncClient(), retry_delays_s=[2.0, 4.0, 8.0])
    async with client:
        with pytest.raises(BiliError):
            await client.search_videos("测试")

    assert search_route.call_count == 4  # 初次 + 3 次重试
    assert sleeps == [2.0, 4.0, 8.0]  # 三档退避全部可达


@pytest.mark.asyncio
@respx.mock
async def test_bili_error_message_carries_root_cause() -> None:
    """BiliError 消息携带根因（BiliError 带 last_error repr + 最后 HTTP 状态码，§9.1）。"""
    from bili_search import BiliError

    respx.get(SPI_URL).mock(return_value=_spi_response())
    respx.get(NAV_URL).mock(return_value=httpx.Response(200, json=_nav_response()))
    respx.get(SEARCH_URL).mock(
        side_effect=httpx.ConnectError("connection refused by test")
    )

    client = BiliSearchClient(httpx.AsyncClient(), retry_delays_s=[0.0, 0.0])
    async with client:
        with pytest.raises(BiliError) as exc_info:
            await client.search_videos("测试")

    msg = str(exc_info.value)
    assert "ConnectError" in msg and "connection refused by test" in msg  # 根因摘要
    assert "status" in msg.lower() or "HTTP" in msg  # 携带最后状态码语境
