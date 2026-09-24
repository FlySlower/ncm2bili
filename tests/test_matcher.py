"""matcher.py 单测（里程碑 M3，文档 §4.1/§4.3/§4.4）。

B 站/网易云客户端通过构造参数注入真实 client + respx mock，全程无真实网络。
"""
from __future__ import annotations

import asyncio
import json
import random
import time as _time

import httpx
import pytest

from bili_search import BiliSearchClient
from config import Config
from db import Database
from matcher import Matcher, sanitize_song_name
from ncm import NcmClient
from scorer import stage1_score

HOME_URL = "https://www.bilibili.com/"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
RELATION_URL = "https://api.bilibili.com/x/relation/stat"

IMG_KEY = "653657f524a547ac981ded72ea172057"
SUB_KEY = "6e4909c702f846728e64f6007736a338"

SONG = {
    "ncm_id": 1001,
    "name": "夜曲",
    "artist": "周杰伦",
    "album": "十一月的萧邦",
    "alia": [],
    "origin": {},
}


def _nav_response() -> dict:
    return {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
            }
        },
    }


def _ok(**data) -> httpx.Response:
    """构造 B 站 code=0 成功响应（缩短 mock 行长）。"""
    return httpx.Response(200, json={"code": 0, "data": data})


def _video(bvid: str, title: str, mid: int = 1, **kw) -> dict:
    base = {
        "bvid": bvid,
        "title": title,
        "mid": mid,
        "author": "UP主",
        "play": 100_000,
        "favorites": 1_000,
        "video_review": 100,
        "duration": 240,
    }
    base.update(kw)
    return base


def _mock_env(respx_mock) -> None:
    respx_mock.get(SPI_URL).mock(
        return_value=httpx.Response(
            200, json={"code": 0, "data": {"b_3": "MATCHER-B3", "b_4": "MATCHER-B4"}}
        )
    )
    respx_mock.get(HOME_URL).mock(
        return_value=httpx.Response(200, headers={"set-cookie": "buvid3=MATCHER-TEST; Path=/"})
    )
    respx_mock.get(NAV_URL).mock(return_value=httpx.Response(200, json=_nav_response()))


def _fast_cfg() -> Config:
    """测试用 Config：搜索限速归零，避免 F1-1 下沉的 sleep 拖慢用例。"""
    cfg = Config()
    cfg.rate_limit.search.interval_ms = 0
    cfg.rate_limit.search.jitter_ms = [0, 0]
    return cfg


def _make_matcher(db: Database, **kwargs) -> Matcher:
    cfg = _fast_cfg()
    bili = BiliSearchClient(httpx.AsyncClient(), db=db)
    ncm = NcmClient(httpx.AsyncClient())
    http = httpx.AsyncClient()
    return Matcher(cfg, db, bili, ncm, http, **kwargs)


# ---- 验收 1 & 2：四种 method 分支 + 优先级铁律 ----------------------


@pytest.mark.asyncio
async def test_whitelist_bv_branch_skips_search(respx_mock, tmp_path) -> None:
    """WHITELIST_BV：命中白名单即 MATCHED（F2-1 新语义：已匹配待收藏，收藏成功才
    DONE），不发任何搜索/评分请求（跳过打分）。"""
    db = Database(tmp_path / "t.db")
    # 预置历史失败残留（如曾被降级链置 MANUAL），转 MATCHED 后必须清空
    db.upsert_song("夜曲|周杰伦", status="MANUAL", method="MANUAL",
                   fail_reason="匹配失败：降级链全部未命中")
    matcher = _make_matcher(db, whitelist={"夜曲|周杰伦": "BV1wl"})
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["method"] == "WHITELIST_BV"
    assert result["bvid"] == "BV1wl"
    assert len(respx_mock.calls) == 0  # 无任何网络请求
    row = db.query_one("SELECT * FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "MATCHED" and row["method"] == "WHITELIST_BV"
    assert row["fail_reason"] is None  # 成功态清因（文档 §6）


@pytest.mark.asyncio
async def test_manual_overrides_everything(respx_mock, tmp_path) -> None:
    """优先级铁律 a：manual.json 覆盖 whitelist.json（最高优先级）。"""
    db = Database(tmp_path / "t.db")
    matcher = _make_matcher(
        db,
        whitelist={"夜曲|周杰伦": "BV1whitelist"},
        manual={"夜曲|周杰伦": "BV1manual"},
    )
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["method"] == "MANUAL"
    assert result["bvid"] == "BV1manual"
    assert len(respx_mock.calls) == 0


@pytest.mark.asyncio
async def test_uploader_wl_branch_skips_scoring(respx_mock, tmp_path) -> None:
    """UPLOADER_WL：uploaders 命中且标题含歌名 → 直接 MATCHED（F2-1 新语义），
    不调 view/relation。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    search_keywords: list[str] = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_keywords.append(request.url.params.get("keyword"))
        return httpx.Response(
            200,
            json={"code": 0, "data": {"result": [_video("BV1up", "夜曲 钢琴版", mid=999)]}},
        )

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)
    respx_mock.get(VIEW_URL).mock(return_value=_ok())
    respx_mock.get(RELATION_URL).mock(return_value=_ok(follower=1))

    matcher = _make_matcher(db, uploaders={"999": {"name": "UP主", "note": ""}})
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["method"] == "UPLOADER_WL"
    assert result["bvid"] == "BV1up"
    assert search_keywords == ["夜曲 周杰伦"]  # 轮 1 关键词
    # 无 view/relation 请求（跳过打分）
    assert not [
        c for c in respx_mock.calls
        if "view" in str(c.request.url) or "relation" in str(c.request.url)
    ]
    row = db.query_one("SELECT status, method, fail_reason FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "MATCHED" and row["method"] == "UPLOADER_WL"
    assert row["fail_reason"] is None  # 成功态清因（文档 §6）


@pytest.mark.asyncio
async def test_scored_branch_when_no_whitelist(respx_mock, tmp_path) -> None:
    """SCORED：无任何白名单时走两阶段评分（差值明显 → 不发阶段二请求）。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "result": [
                        _video("BV1top", "夜曲 周杰伦 官方MV",
                               play=1_000_000, favorites=10_000, video_review=1_000),
                        _video("BV2low", "夜曲 周杰伦 现场", play=1_000, favorites=10, video_review=1),
                    ]
                },
            },
        )
    )
    respx_mock.get(VIEW_URL).mock(return_value=_ok())
    respx_mock.get(RELATION_URL).mock(return_value=_ok(follower=1))

    matcher = _make_matcher(db)
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["method"] == "SCORED"
    assert result["bvid"] == "BV1top"
    assert not [
        c for c in respx_mock.calls
        if "view" in str(c.request.url) or "relation" in str(c.request.url)
    ]
    row = db.query_one("SELECT * FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "MATCHED" and row["method"] == "SCORED"
    assert row["fail_reason"] is None  # 成功态清因（文档 §6）
    assert row["score_detail"] is not None


@pytest.mark.asyncio
async def test_dry_run_flag_writes_done_directly(respx_mock, tmp_path) -> None:
    """文档 §10.1（F2-1）：dry_run=True 无阶段三，匹配完成直接置 DONE（正式语义
    置 MATCHED，仅 fav_one() 收藏成功才 DONE）。"""
    db = Database(tmp_path / "t.db")
    matcher = _make_matcher(db, dry_run=True, whitelist={"夜曲|周杰伦": "BV1dry"})
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["status"] == "DONE"
    row = db.query_one("SELECT status, method, bvid FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "DONE"
    assert row["method"] == "WHITELIST_BV" and row["bvid"] == "BV1dry"


# ---- 验收 3：黑名单先于 UPLOADER_WL + 记 fail_reason -----------------


@pytest.mark.asyncio
async def test_blacklist_blocks_uploader_wl_and_records_reason(respx_mock, tmp_path) -> None:
    """候选 mid 在 uploaders 但标题含黑名单词 → 不被 UPLOADER_WL 命中，最终 MANUAL 并记因。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    # 每一轮都返回同一黑名单候选（降级链各轮均被黑名单过滤）
    respx_mock.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"code": 0, "data": {"result": [_video("BV1blk", "夜曲 精选串烧", mid=999)]}}
        )
    )

    matcher = _make_matcher(
        db, uploaders={"999": {"name": "UP主", "note": ""}}, blacklist_words=["串烧", "精选"]
    )
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["status"] == "MANUAL"  # 黑名单先于 uploaders，候选被淘汰
    assert "黑名单" in result["fail_reason"]
    row = db.query_one("SELECT * FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "MANUAL"
    assert "黑名单" in row["fail_reason"]


# ---- 验收 4：降级链 轮2/轮3 触发 ------------------------------------


@pytest.mark.asyncio
async def test_round2_fallback_when_round1_empty(respx_mock, tmp_path) -> None:
    """轮 1（歌名 歌手）无结果 → 轮 2（歌手 歌名）成功（文档 §4.3 新链）。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    search_keywords: list[str] = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword")
        search_keywords.append(kw)
        if kw == "夜曲 周杰伦":
            return httpx.Response(200, json={"code": 0, "data": {"result": []}})
        return _ok(result=[_video("BV1r2", "周杰伦 夜曲 完整版")])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    matcher = _make_matcher(db)
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["method"] == "SCORED"
    assert result["bvid"] == "BV1r2"
    assert search_keywords == ["夜曲 周杰伦", "周杰伦 夜曲"]


@pytest.mark.asyncio
async def test_round3_fallback_when_rounds1_2_empty(respx_mock, tmp_path) -> None:
    """轮 1、2 均无结果 → 轮 3（歌名）成功（文档 §4.3 新链）。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    search_keywords: list[str] = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword")
        search_keywords.append(kw)
        if kw == "夜曲":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"result": [_video("BV1r3", "周杰伦 夜曲 完整版")]}},
            )
        return httpx.Response(200, json={"code": 0, "data": {"result": []}})

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    matcher = _make_matcher(db)
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["method"] == "SCORED"
    assert search_keywords == ["夜曲 周杰伦", "周杰伦 夜曲", "夜曲"]


@pytest.mark.asyncio
async def test_round4_mv_hit_scored(respx_mock, tmp_path) -> None:
    """验收 1：第 1~3 轮无合格候选、第 4 轮（歌名 MV）返回合格 MV → SCORED。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    search_keywords: list[str] = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword")
        search_keywords.append(kw)
        if kw == "夜曲 MV":
            return _ok(result=[_video("BV1mv", "周杰伦 夜曲 官方 MV")])
        return _ok(result=[])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    matcher = _make_matcher(db)
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["method"] == "SCORED"
    assert result["bvid"] == "BV1mv"
    assert search_keywords == ["夜曲 周杰伦", "周杰伦 夜曲", "夜曲", "夜曲 MV"]


@pytest.mark.asyncio
async def test_all_degrade_rounds_fail_goes_manual(respx_mock, tmp_path) -> None:
    """验收 2：默认 9 轮全败 → MANUAL，总搜索请求数 = 9（文档 §4.3 config 驱动）。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)

    search_count = {"n": 0}

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_count["n"] += 1
        return httpx.Response(200, json={"code": 0, "data": {"result": []}})

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    matcher = _make_matcher(db)
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["status"] == "MANUAL"
    assert result["method"] == "MANUAL"
    assert search_count["n"] == 9  # 默认 9 轮降级关键词各搜索一次


@pytest.mark.asyncio
async def test_all_rounds_fail_goes_manual(respx_mock, tmp_path) -> None:
    """全链失败 → MANUAL（文档 §4.3 默认 9 轮链）。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"result": []}})
    )

    matcher = _make_matcher(db)
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["status"] == "MANUAL"
    assert result["method"] == "MANUAL"
    assert result["bvid"] is None


# ---- 验收 6：状态跃迁落盘（中断后从 db 读状态） ----------------------


@pytest.mark.asyncio
async def test_search_failure_degrades_single_song(respx_mock, tmp_path) -> None:
    """文档 §9.1：搜索重试耗尽（BiliError）单歌降级——MANUAL(SEARCH_FAILED)，不崩任务。

    网络错误经 _request_json 重试耗尽 → BiliError → matcher 捕获置 MANUAL，
    fail_reason 记 SEARCH_FAILED 并携带关键词与根因；db 落盘状态为 MANUAL。
    """
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(side_effect=httpx.ConnectError("network down"))

    matcher = _make_matcher(db)
    async with matcher:
        result = await matcher.match_song(SONG)  # 不再抛异常（§9.1 单歌降级）

    assert result["status"] == "MANUAL"
    assert result["bvid"] is None
    assert "SEARCH_FAILED" in result["fail_reason"]
    assert "夜曲 周杰伦" in result["fail_reason"]      # 携带失败关键词
    assert "network down" in result["fail_reason"]     # 携带根因摘要
    row = db.query_one("SELECT * FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row is not None
    assert row["status"] == "MANUAL"
    assert "SEARCH_FAILED" in row["fail_reason"]
    assert row["name"] == "夜曲" and row["artist"] == "周杰伦"
    assert row["ncm_id"] == 1001


# ---- M7 P0：search_cache 接通（文档 §4.4）-----------------------------


@pytest.mark.asyncio
async def test_search_cache_hit_skips_network(respx_mock, tmp_path) -> None:
    """验收 1：同一关键词第二次匹配命中缓存，搜索接口调用次数为 0。"""
    import time as _time

    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)

    search_count = {"n": 0}

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_count["n"] += 1
        return _ok(result=[_video("BV1c", "夜曲 周杰伦 官方MV")])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    # 预置未过期缓存（文档 §4.4：以关键词为 key）
    cached_results = (
        '[{"bvid": "BV1c", "title": "夜曲 周杰伦 官方MV", "mid": 1, "play": 100, '
        '"favorites": 10, "video_review": 1, "duration": 240}]'
    )
    db.execute(
        "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
        ("夜曲 周杰伦", cached_results, int(_time.time())),
    )

    matcher = _make_matcher(db)
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["status"] == "MATCHED"  # F2-1：正式语义匹配成功置 MATCHED
    assert search_count["n"] == 0  # 缓存命中，零搜索请求


@pytest.mark.asyncio
async def test_search_cache_expired_refetches(respx_mock, tmp_path) -> None:
    """验收 2：TTL（7 天）过期后重新发起搜索。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)

    search_count = {"n": 0}

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_count["n"] += 1
        return _ok(result=[_video("BV1e", "夜曲 周杰伦 官方MV")])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    # 过期缓存（fetched_at 远早于 TTL）
    db.execute(
        "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
        ("夜曲 周杰伦", '[]', 1),
    )

    matcher = _make_matcher(db)
    async with matcher:
        await matcher.match_song(SONG)

    assert search_count["n"] >= 1  # 过期 → 重新搜索


@pytest.mark.asyncio
async def test_search_results_written_to_cache(respx_mock, tmp_path) -> None:
    """搜索完成后写入 search_cache，后续可直接复用。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_video("BV1w", "夜曲 周杰伦 官方MV")])
    )

    matcher = _make_matcher(db)
    async with matcher:
        await matcher.match_song(SONG)

    row = db.query_one("SELECT results, fetched_at FROM search_cache WHERE keyword = '夜曲 周杰伦'")
    assert row is not None
    assert "BV1w" in row["results"]
    assert row["fetched_at"] > 0


# ---- 验收 3：降级词从 config 读取（改 config 不改代码即生效）----------


@pytest.mark.asyncio
async def test_degrade_keywords_from_config(respx_mock, tmp_path) -> None:
    """自定义 config.degrade.keywords：只改配置即生效（轮序/内容跟随）。"""
    from config import Config as _Config

    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    search_keywords: list[str] = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword")
        search_keywords.append(kw)
        if kw == "周杰伦演唱会版 夜曲":
            return _ok(result=[_video("BV1cfg", "夜曲 周杰伦 演唱会版")])
        return _ok(result=[])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    # 自定义降级链（替换默认 9 条）
    cfg = _fast_cfg()
    cfg.degrade.keywords = ["{name} {artist}", "周杰伦演唱会版 {name}"]

    matcher = Matcher(cfg, db, BiliSearchClient(httpx.AsyncClient(), db=db),
                      NcmClient(httpx.AsyncClient()), httpx.AsyncClient())
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["method"] == "SCORED"
    assert result["bvid"] == "BV1cfg"
    assert search_keywords == ["夜曲 周杰伦", "周杰伦演唱会版 夜曲"]


# ===================================================================
# v0.4.1：P0 search_cache 缓存正确性（文档 §4.4）
# ===================================================================


@pytest.mark.asyncio
async def test_cache_key_is_full_keyword(respx_mock, tmp_path) -> None:
    """§4.4 约束 1：缓存 key = 完整（sanitize 后）关键词，禁止截断/哈希/归一化。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_video("BV1k", "夜曲 周杰伦 官方MV")])
    )

    matcher = _make_matcher(db)
    song = {"ncm_id": 1, "name": "夜曲", "artist": "周杰伦"}
    async with matcher:
        await matcher._search_cached(song, "夜曲 周杰伦")

    rows = db.query("SELECT keyword FROM search_cache")
    assert rows and rows[0]["keyword"] == "夜曲 周杰伦"  # 完整关键词入库，非截断/哈希


@pytest.mark.asyncio
async def test_cache_results_isolated_across_keys(respx_mock, tmp_path) -> None:
    """§4.4 约束 2：两个不同关键词构造缓存，结果对象独立（读路径各自 json.loads）。"""
    db = Database(tmp_path / "t.db")
    now = int(_time.time())
    db.execute(
        "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
        ("夜曲 周杰伦", json.dumps([{"bvid": "BV1a", "title": "夜曲 官方"}]), now),
    )
    db.execute(
        "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
        ("周杰伦 夜曲", json.dumps([{"bvid": "BV2b", "title": "夜曲 现场"}]), now),
    )

    matcher = _make_matcher(db)
    song = {"name": "夜曲", "artist": "周杰伦"}
    async with matcher:
        a = await matcher._search_cached(song, "夜曲 周杰伦")
        b = await matcher._search_cached(song, "周杰伦 夜曲")

    assert a is not b  # 两个 key 命中不同对象实例
    assert a[0]["bvid"] == "BV1a" and b[0]["bvid"] == "BV2b"
    a[0]["bvid"] = "MUTATED"  # 原地修改 A 对象
    assert b[0]["bvid"] == "BV2b"  # B 对象不受影响（跨 key 隔离）
    row_b = db.query_one("SELECT results FROM search_cache WHERE keyword = '周杰伦 夜曲'")
    assert "BV2b" in row_b["results"]  # 数据库内容亦未被污染


@pytest.mark.asyncio
async def test_cache_empty_results_not_stored_no_fallback(respx_mock, tmp_path) -> None:
    """§4.4 约束 3：空结果不写缓存、不回退复用其他关键词的结果。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    now = int(_time.time())
    db.execute(
        "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
        ("别人 歌手", json.dumps([{"bvid": "BV9", "title": "别人的歌"}]), now),
    )
    kws: list[str] = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        kws.append(request.url.params.get("keyword"))
        return _ok(result=[])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    matcher = _make_matcher(db)
    song = {"name": "夜曲", "artist": "周杰伦"}
    async with matcher:
        res = await matcher._search_cached(song, "夜曲 周杰伦")

    assert res == []  # 空结果如实返回，未回退成"别人 歌手"的缓存
    assert kws == ["夜曲 周杰伦"]  # 真正发起了搜索
    row = db.query_one("SELECT results FROM search_cache WHERE keyword = '夜曲 周杰伦'")
    assert row is None  # 空结果不写入缓存


@pytest.mark.asyncio
async def test_cache_dirty_result_invalidated_and_refetched(respx_mock, tmp_path) -> None:
    """§4.4 结果归属校验（P0 根因）：预置异关键词脏缓存 → 作废重取。

    对应实测：'沙龙 陈奕迅' 缓存被上游 stale 结果污染（标题全是"十年"高亮）。
    归属校验失败 → 删除脏行 + 重新搜索，新结果归属通过后落库。
    """
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    now = int(_time.time())
    dirty = json.dumps([
        {"bvid": "BVpolluted", "title": "在百万豪装录音棚大声听陈奕迅《十年》"}
    ])
    db.execute(
        "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
        ("沙龙 陈奕迅", dirty, now),
    )
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_video("BVclean", "沙龙 官方MV")])
    )

    matcher = _make_matcher(db)
    song = {"name": "沙龙", "artist": "陈奕迅"}
    async with matcher:
        res = await matcher._search_cached(song, "沙龙 陈奕迅")

    assert res and res[0]["bvid"] == "BVclean"  # 脏缓存作废，重新搜索
    row = db.query_one("SELECT results FROM search_cache WHERE keyword = '沙龙 陈奕迅'")
    assert row is not None and "BVclean" in row["results"]


@pytest.mark.asyncio
async def test_cache_unowned_results_not_written(respx_mock, tmp_path) -> None:
    """§4.4 结果归属校验：新搜索结果不含歌名 token → 拒绝写入缓存（防污染）。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[{"bvid": "BVx", "title": "十年 官方MV"}])
    )

    matcher = _make_matcher(db)
    song = {"name": "沙龙", "artist": "陈奕迅"}
    async with matcher:
        res = await matcher._search_cached(song, "沙龙 陈奕迅")

    # F1-2（§4.4）：逐条过滤后无归属候选 → 视为该关键词未命中（返回 []）
    assert res == []
    row = db.query_one("SELECT results FROM search_cache WHERE keyword = '沙龙 陈奕迅'")
    assert row is None  # 未通过归属校验 → 不写入缓存


# ===================================================================
# F1-2：归属校验逐条过滤候选（§4.4）
# ===================================================================


@pytest.mark.asyncio
async def test_filter_owned_keeps_only_matching_candidates(respx_mock, tmp_path) -> None:
    """F1-2：搜索返回混合候选（含/不含歌名 token），仅保留归属通过项并写入缓存。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[
            _video("BVowned", "夜曲 周杰伦 官方MV"),
            _video("BVjunk", "十年 官方MV"),          # 不含歌名 token
            _video("BVowned2", "夜曲 现场"),
        ])
    )

    matcher = _make_matcher(db)
    song = {"name": "夜曲", "artist": "周杰伦"}
    async with matcher:
        res = await matcher._search_cached(song, "夜曲 周杰伦")

    # 仅保留标题含「夜曲」的候选（逐条过滤，非整体判断）
    assert len(res) == 2
    assert {r["bvid"] for r in res} == {"BVowned", "BVowned2"}
    # 缓存仅写过滤后的归属候选（不含 BVjunk）
    row = db.query_one("SELECT results FROM search_cache WHERE keyword = '夜曲 周杰伦'")
    cached = json.loads(row["results"])
    assert {r["bvid"] for r in cached} == {"BVowned", "BVowned2"}


@pytest.mark.asyncio
async def test_filter_owned_cache_hit_returns_only_owned(respx_mock, tmp_path) -> None:
    """F1-2：缓存命中含混合候选 → 读时同样逐条过滤，仅返回归属通过项。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    now = int(_time.time())
    db.execute(
        "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
        ("夜曲 周杰伦", json.dumps([
            {"bvid": "BVkeep", "title": "夜曲 官方"},
            {"bvid": "BVdrop", "title": "十年 官方"},   # 不含「夜曲」
        ]), now),
    )

    matcher = _make_matcher(db)
    song = {"name": "夜曲", "artist": "周杰伦"}
    async with matcher:
        res = await matcher._search_cached(song, "夜曲 周杰伦")

    # 缓存命中时逐条过滤，仅返回 BVkeep（不发搜索请求）
    assert len(res) == 1
    assert res[0]["bvid"] == "BVkeep"
    assert len(respx_mock.calls) == 0  # 未发搜索（缓存命中）


# ===================================================================
# F1-1：搜索间隔 sleep 下沉到每次请求（§7 预防层）
# ===================================================================


@pytest.mark.asyncio
async def test_search_sleep_formula_covers_cache_hit_and_miss(
    respx_mock, tmp_path, monkeypatch,
) -> None:
    """F1-1（§7 预防层）：每次搜索请求前 sleep = interval_ms + uniform(jitter)。

    sleep 下沉到 _search_cached 入口，覆盖缓存命中与未命中两条路径（F1-1 明确）。
    """
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    # 预置未过期缓存（命中路径）
    cached = json.dumps([{"bvid": "BV1c", "title": "夜曲 周杰伦 官方MV"}])
    db.execute(
        "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
        ("夜曲 周杰伦", cached, int(_time.time())),
    )
    # 未命中路径也需要 mock 搜索（返回归属通过的结果）
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_video("BV1m", "夜曲 周杰伦 官方MV")])
    )

    cfg = Config()
    cfg.rate_limit.search.interval_ms = 100
    cfg.rate_limit.search.jitter_ms = [50, 50]  # 固定 jitter 区间

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(random, "uniform", lambda a, b: 50)  # jitter 固定 50

    matcher = Matcher(
        cfg, db, BiliSearchClient(httpx.AsyncClient(), db=db),
        NcmClient(httpx.AsyncClient()), httpx.AsyncClient(),
    )
    async with matcher:
        await matcher._search_cached(SONG, "夜曲 周杰伦")        # 缓存命中
        await matcher._search_cached(SONG, "夜曲 周杰伦 现场")    # 缓存未命中

    # 每次请求都 sleep（含缓存命中），值 = (100 + 50)/1000 = 0.15
    assert sleeps == [0.15, 0.15]


# ===================================================================
# P2：sanitize 关键词卫生（文档 §4.3）
# ===================================================================


KEEP_TOKENS = Config().degrade.keep_tokens


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Constriction2.0（压迫感）", "Constriction2.0"),           # 全角注释剥离
        ("Constriction2.0(压迫感)", "Constriction2.0"),            # 半角注释剥离
        ("你好（炙热）", "你好"),
        ("倾城(Live)", "倾城(Live)"),                              # 版本 token 保留
        ("反高潮(Live)", "反高潮(Live)"),
        ("一生中最爱(Live)", "一生中最爱(Live)"),
        ("MiyaGi-ТАМАДА（Nurselim Boy / DruGTRaffickerS remix）",
         "MiyaGi-ТАМАДА（Nurselim Boy / DruGTRaffickerS remix）"),  # remix 保留
        ("", ""),                                                   # 空歌名回退
    ],
)
def test_sanitize_song_name_cases(raw: str, expected: str) -> None:
    assert sanitize_song_name(raw, KEEP_TOKENS) == expected


def test_sanitize_never_appends_hardcoded_words() -> None:
    """§4.3 规则 2：sanitize 只清洗不追加修饰词（追加词仅出自 degrade.keywords）。"""
    assert "纯享" not in sanitize_song_name("一生中最爱(Live)", KEEP_TOKENS)
    assert "MV" not in sanitize_song_name("一生中最爱(Live)", KEEP_TOKENS)


@pytest.mark.asyncio
async def test_degrade_keywords_go_through_sanitize_and_templates(respx_mock, tmp_path) -> None:
    """sanitize 无旁路：搜索关键词全部来自 sanitize + degrade 模板，无硬编码追加词。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    search_kws: list[str] = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_kws.append(request.url.params.get("keyword"))
        return _ok(result=[])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    cfg = _fast_cfg()
    cfg.degrade.keywords = ["{name} {artist}", "{name}"]  # 不含纯享等追加词

    matcher = Matcher(cfg, db, BiliSearchClient(httpx.AsyncClient(), db=db),
                      NcmClient(httpx.AsyncClient()), httpx.AsyncClient())
    song = {"name": "一生中最爱(Live)", "artist": "陈奕迅"}
    async with matcher:
        await matcher.match_song(song)

    # 传输层注意：sign_wbi 对参数值过滤 "!'()*"（§5.2 公开实现），括号在 URL 中被剥离，
    # 故 respx 收到的 keyword 无括号；sanitize 保留的 (Live) 体现在缓存 key 与打分阶段。
    assert search_kws == ["一生中最爱Live 陈奕迅", "一生中最爱Live"]
    assert not any("纯享" in kw for kw in search_kws)


# ===================================================================
# P2：MatchGate 三闸门（文档 §4.2，三批次真实病例回归，A 类）
# ===================================================================


def _cand(bvid: str, title: str, *, play: int = 100_000, fav: int = 1_000,
          reply: int = 100, dur: int = 240, mid: int = 1) -> dict:
    return _video(bvid, title, mid=mid, play=play, favorites=fav,
                  video_review=reply, duration=dur)


# 候选对公共参数：top1/top2 分差 <2（margin），标题同时含歌名与艺人（通过前两闸门）
_MARGIN_TOP1 = dict(play=10_000_000, fav=10_000, reply=1_000)
_MARGIN_TOP2 = dict(play=100_278, fav=10_000, reply=1_000)  # top1-top2 ≈ 1.9988

# A 类·闸门应拦：(歌名, 艺人, 候选列表, 期望 fail_reason 子串)
MATCH_GATE_A_CASES = [
    # ---- margin 闸门（头部不分伯仲，LOW_CONFIDENCE）----
    ("Graveyard Phonk", "Gravestone",
     [_cand("BVg1", "Graveyard Phonk 官方MV", **_MARGIN_TOP1),
      _cand("BVg2", "Graveyard Phonk 官方MV", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    ("约定(Live)", "陈奕迅",
     [_cand("BVy1", "约定(Live) 演唱会 陈奕迅", **_MARGIN_TOP1),
      _cand("BVy2", "约定(Live) 演唱会 陈奕迅", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    ("喜帖街(live)", "谢安琪",
     [_cand("BVx1", "喜帖街(live) 演唱会 官方MV", **_MARGIN_TOP1),
      _cand("BVx2", "喜帖街(live) 演唱会 官方MV", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    ("黄金时代2007", "陈奕迅",
     [_cand("BVh1", "黄金时代2007 高清修复 官方", **_MARGIN_TOP1),
      _cand("BVh2", "黄金时代2007 高清修复 官方", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    ("那一夜有没有说(Live)", "陈奕迅",
     [_cand("BVn1", "那一夜有没有说(Live) 演唱会 陈奕迅", **_MARGIN_TOP1),
      _cand("BVn2", "那一夜有没有说(Live) 演唱会 陈奕迅", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    ("十面埋伏(Live)", "陈奕迅",
     [_cand("BVs1", "十面埋伏(Live) 演唱会 陈奕迅", **_MARGIN_TOP1),
      _cand("BVs2", "十面埋伏(Live) 演唱会 陈奕迅", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    ("单车(Live)", "陈奕迅",
     [_cand("BVd1", "单车(Live) 演唱会 陈奕迅", **_MARGIN_TOP1),
      _cand("BVd2", "单车(Live) 演唱会 陈奕迅", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    ("等(Live)", "陈奕迅",
     [_cand("BVdd1", "等(Live) 演唱会 陈奕迅", **_MARGIN_TOP1),
      _cand("BVdd2", "等(Live) 演唱会 陈奕迅", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    ("沙龙", "陈奕迅",
     [_cand("BVsl1", "沙龙 演唱会 陈奕迅", **_MARGIN_TOP1),
      _cand("BVsl2", "沙龙 演唱会 陈奕迅", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    ("阿牛", "陈奕迅",
     [_cand("BVan1", "阿牛 演唱会 陈奕迅", **_MARGIN_TOP1),
      _cand("BVan2", "阿牛 演唱会 陈奕迅", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    ("白玫瑰", "陈奕迅",
     [_cand("BVb1", "白玫瑰 演唱会 陈奕迅", **_MARGIN_TOP1),
      _cand("BVb2", "白玫瑰 演唱会 陈奕迅", **_MARGIN_TOP2)],
     "LOW_CONFIDENCE"),
    # ---- 低分闸门（score < min_score=16；fav/reply 清零避免人气项抬分）----
    ("in heat.", "XXX",
     [_cand("BVi", "in heat. 现场 录播", play=0, fav=0, reply=0)], "LOW_CONFIDENCE"),
    ("Vai Toma", "YYY",
     [_cand("BVv", "Vai Toma 现场 录播", play=1, fav=0, reply=0)], "LOW_CONFIDENCE"),
    ("Sweet Sensation", "ZZZ",
     [_cand("BVsw", "Sweet Sensation 现场 录播", play=5, fav=0, reply=0)], "LOW_CONFIDENCE"),
    ("16月6日晴", "陈奕迅",
     [_cand("BV16", "16月6日晴 现场 录播", play=0, fav=0, reply=0)], "LOW_CONFIDENCE"),
    # ---- 短歌名联合闸（NO_TITLE_MATCH: artist 缺失）----
    ("我们", "陈奕迅",
     [_cand("BVw", "我们走在大路上 官方MV", play=1_000_000, fav=100_000, reply=10_000)],
     "NO_TITLE_MATCH"),
    ("海胆", "陈奕迅",
     [_cand("BVh", "海胆 科普视频 官方MV", play=1_000_000, fav=100_000, reply=10_000)],
     "NO_TITLE_MATCH"),
    ("黑洞", "陈奕迅",
     [_cand("BVbl", "黑洞 科普视频 官方MV", play=1_000_000, fav=100_000, reply=10_000)],
     "NO_TITLE_MATCH"),
    ("四季", "陈奕迅",
     [_cand("BVsj", "四季 风景视频 官方MV", play=1_000_000, fav=100_000, reply=10_000)],
     "NO_TITLE_MATCH"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,artist,cands,expected_sub",
                         MATCH_GATE_A_CASES,
                         ids=[c[0] for c in MATCH_GATE_A_CASES])
async def test_match_gate_a_cases_blocked(
    name: str, artist: str, cands: list[dict], expected_sub: str,
    respx_mock, tmp_path,
) -> None:
    """三批次真实病例：闸门应拦 → MANUAL + 具体 fail_reason（A 类）。"""
    # fixture 前提自证：候选确实满足对应闸门条件
    cfg = Config()

    def _stage(v: dict) -> float:  # 对齐 _candidate：video_review → reply 后打分
        return stage1_score(
            {"play": v.get("play", 0), "favorites": v.get("favorites", 0),
             "reply": v.get("video_review", 0), "duration": v.get("duration", 0),
             "title": v.get("title", "")},
            name, cfg.scoring,
        )

    scores = [_stage(c) for c in cands]
    if expected_sub == "NO_TITLE_MATCH":
        # 短歌名联合闸前提：标题含歌名 token 但不含艺人 token（分数达标）
        from matcher import _count_cjk, _name_tokens, _title_contains_all, _title_contains_any
        assert 0 < _count_cjk(name) <= 2
        assert _title_contains_all(cands[0]["title"], _name_tokens(name))
        assert not _title_contains_any(cands[0]["title"], _name_tokens(artist))
        assert scores[0] >= cfg.match.min_score  # 高分的泛词错配（恰恰是闸门要拦的）
    elif len(cands) >= 2:
        assert 0 <= scores[0] - scores[1] < cfg.match.min_margin  # margin 闸门前提
    else:
        assert scores[0] < cfg.match.min_score  # 低分闸门前提

    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    # margin 候选对会导致阶段二触发（分差 <10%），mock 精排接口
    respx_mock.get(VIEW_URL).mock(return_value=_ok())
    respx_mock.get(RELATION_URL).mock(return_value=_ok(follower=100))
    respx_mock.get(SEARCH_URL).mock(return_value=_ok(result=cands))

    matcher = _make_matcher(db)
    song = {"ncm_id": 1, "name": name, "artist": artist}
    async with matcher:
        result = await matcher.match_song(song)

    assert result["status"] == "MANUAL"
    assert result["bvid"] is None
    assert expected_sub in result["fail_reason"], result["fail_reason"]
    if expected_sub == "NO_TITLE_MATCH":
        assert "artist" in result["fail_reason"]  # 短歌名联合闸：艺人 token 缺失
    # 落盘与 reason 一致
    row = db.query_one(
        "SELECT status, fail_reason FROM songs WHERE song_key = ?", (f"{name}|{artist}",)
    )
    assert row["status"] == "MANUAL"
    assert expected_sub in row["fail_reason"]


# ---- C 类：闸门够不着（人气压艺人，归 v0.4.2）→ 正常 SCORED，score_detail 完整落库 ----


MATCH_GATE_C_CASES = [
    ("Lake Arrowhead", "Lorenzo"),
    ("Let Me Think About It", "XXX"),
    ("九万进行曲", "陈奕迅"),
    ("你给我听好", "陈奕迅"),
    ("热带雨林", "陈奕迅"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,artist", MATCH_GATE_C_CASES,
                         ids=[c[0] for c in MATCH_GATE_C_CASES])
async def test_match_gate_c_cases_scored_with_detail(
    name: str, artist: str, respx_mock, tmp_path,
) -> None:
    """C 类：候选合格 → SCORED；score_detail（含 top3 候选）完整落库。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_cand("BVc", f"{name} 官方MV", play=1_000_000, fav=10_000)])
    )

    matcher = _make_matcher(db)
    song = {"ncm_id": 1, "name": name, "artist": artist}
    async with matcher:
        result = await matcher.match_song(song)

    assert result["status"] == "MATCHED"  # F2-1：正式语义置 MATCHED（待收藏）
    assert result["method"] == "SCORED"
    row = db.query_one(
        "SELECT score_detail FROM songs WHERE song_key = ?", (f"{name}|{artist}",)
    )
    detail = json.loads(row["score_detail"])
    assert detail["candidates"], "score_detail 必须含候选对比"
    assert detail["bvid"] == "BVc"


# ---- 正向：识别正确匹配不被误伤（仍匹配成功）----

MATCH_GATE_POSITIVE_CASES = [
    ("1-800", "陈奕迅"),
    ("审判时刻", "陈奕迅"),
    ("2001太空漫游", "陈奕迅"),
    ("你的背包", "陈奕迅"),
    ("全世界失眠", "陈奕迅"),
    ("不如这样", "陈奕迅"),
    ("Katrina", "陈奕迅"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,artist", MATCH_GATE_POSITIVE_CASES,
                         ids=[c[0] for c in MATCH_GATE_POSITIVE_CASES])
async def test_match_gate_positive_cases_not_blocked(
    name: str, artist: str, respx_mock, tmp_path,
) -> None:
    """正向病例：标题含歌名且达标 → MATCHED（SCORED），闸门不误伤。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_cand("BVpos", f"{name} 官方MV", play=1_000_000, fav=10_000)])
    )

    matcher = _make_matcher(db)
    song = {"ncm_id": 1, "name": name, "artist": artist}
    async with matcher:
        result = await matcher.match_song(song)

    assert result["status"] == "MATCHED"  # F2-1：正式语义置 MATCHED（待收藏）
    assert result["method"] == "SCORED"
    assert result["bvid"] == "BVpos"


# ---- A/B 混合：倾城(Live) 不再命中 live2d 类无关视频 ----

LIVE2D_JUNK = {"bvid": "BVlive2d", "title": "【Live2D模型展示】初音未来可爱唱歌"}


@pytest.mark.asyncio
async def test_live2d_junk_not_adopted_for_live_song(respx_mock, tmp_path) -> None:
    """B 类：倾城(Live) 搜索返回 live2d 无关视频 → 过滤后未命中，置 MANUAL。

    F1-2（§4.4）：归属校验逐条过滤——live2d 标题不含歌名 token「倾城」，
    被过滤掉，该关键词视为未命中，进入降级链；全轮均未命中 → MANUAL
    （降级链全部未命中）。
    """
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(return_value=_ok(result=[LIVE2D_JUNK]))

    matcher = _make_matcher(db)
    song = {"ncm_id": 1, "name": "倾城(Live)", "artist": "陈奕迅"}
    async with matcher:
        result = await matcher.match_song(song)

    assert result["status"] == "MANUAL"
    assert "降级链全部未命中" in result["fail_reason"]


# ---- MatchGate 阈值 config 驱动（无硬编码） ----

@pytest.mark.asyncio
async def test_match_gate_thresholds_from_config(respx_mock, tmp_path) -> None:
    """min_score/min_margin 从 config 读取：调高 min_score 后高分单候选也被拦。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_cand("BVh", "夜曲 周杰伦 官方MV", play=1_000_000, fav=10_000)])
    )

    cfg = _fast_cfg()
    cfg.match.min_score = 100.0  # 提高门槛 → 原本高分候选被拦

    matcher = Matcher(cfg, db, BiliSearchClient(httpx.AsyncClient(), db=db),
                      NcmClient(httpx.AsyncClient()), httpx.AsyncClient())
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["status"] == "MANUAL"
    assert "LOW_CONFIDENCE" in result["fail_reason"]


# ===================================================================
# F3-2（§4.2/§6）：闸门拒绝时显式 bvid=None，禁止沿用历史 BV
# ===================================================================


@pytest.mark.asyncio
async def test_gate_reject_clears_legacy_bvid_then_done_clears_reason(
    respx_mock, tmp_path,
) -> None:
    """F3-2 验收：上一轮 MATCHED 带 bvid，本轮 MatchGate 拒绝 → MANUAL 且
    bvid IS NULL（不得沿用历史值）；后续白名单救回 DONE，fail_reason 不回退。"""
    db = Database(tmp_path / "t.db")
    # 预置上一轮残留：MATCHED + 历史 BV + 评分明细
    # （task_id 与 _make_matcher 默认 "default" 对齐）
    db.upsert_song(
        "夜曲|周杰伦", ncm_id=1001, name="夜曲", artist="周杰伦",
        status="MATCHED", method="SCORED", bvid="BV1old",
        score_detail='{"bvid": "BV1old"}',
    )
    _mock_env(respx_mock)
    # 唯一候选标题含歌名+艺人（通过前两闸门）但人气为零：
    # 10(歌名)+5(时长)=15 < min_score=16 → 闸门 3 低分拒绝
    respx_mock.get(VIEW_URL).mock(return_value=_ok())
    respx_mock.get(RELATION_URL).mock(return_value=_ok(follower=100))
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_video("BVlow", "夜曲 周杰伦 现场",
                                        play=0, favorites=0, video_review=0)])
    )

    matcher = _make_matcher(db)
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["status"] == "MANUAL"
    assert result["bvid"] is None
    row = db.query_one("SELECT * FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "MANUAL"
    assert row["bvid"] is None  # 历史 BV 被显式清空，不会进阶段三收藏夹
    assert "LOW_CONFIDENCE" in (row["fail_reason"] or "")

    # 救回：白名单落 BV（dry_run 直接 DONE）→ bvid 为新值且 fail_reason 清空不回退
    matcher2 = _make_matcher(db, dry_run=True, whitelist={"夜曲|周杰伦": "BVwl"})
    async with matcher2:
        saved = await matcher2.match_song(SONG)
    assert saved["status"] == "DONE"
    row2 = db.query_one("SELECT * FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row2["bvid"] == "BVwl"
    assert row2["fail_reason"] is None


# ===================================================================
# F3-3（§4.1 ④）：UPLOADER_WL 双侧归一化
# ===================================================================


@pytest.mark.asyncio
async def test_uploader_wl_normalizes_both_sides(respx_mock, tmp_path) -> None:
    """F3-3 验收：大写歌名 + 命中 UP 主 + 全小写标题 → UPLOADER_WL 仍生效。

    修复前 `name in _normalize(title)` 左侧未归一化，"HELLO" 匹配不到小写标题。
    """
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_video("BV1up", "hello adele 官方mv", mid=999)])
    )

    song = {"ncm_id": 9, "name": "HELLO", "artist": "Adele"}
    matcher = _make_matcher(db, uploaders={"999": {"name": "AdeleOfficial", "note": ""}})
    async with matcher:
        result = await matcher.match_song(song)

    assert result["method"] == "UPLOADER_WL"
    assert result["bvid"] == "BV1up"
    row = db.query_one("SELECT status, method FROM songs WHERE song_key = 'HELLO|Adele'")
    assert row["status"] == "MATCHED" and row["method"] == "UPLOADER_WL"


# ===================================================================
# B1（F1-1 补测）：降级链 9 轮 sleep 计数与时长
# ===================================================================


@pytest.mark.asyncio
async def test_degrade_chain_nine_rounds_sleep_count_and_duration(
    respx_mock, tmp_path, monkeypatch,
) -> None:
    """B1 补测：9 轮降级全部未命中 → 恰好 9 次 asyncio.sleep，单次时长
    ∈ [(interval+jitter_lo)/1000, (interval+jitter_hi)/1000]。"""
    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"result": []}})
    )

    cfg = Config()
    cfg.rate_limit.search.interval_ms = 120
    cfg.rate_limit.search.jitter_ms = [30, 80]

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    matcher = Matcher(
        cfg, db, BiliSearchClient(httpx.AsyncClient(), db=db),
        NcmClient(httpx.AsyncClient()), httpx.AsyncClient(),
    )
    async with matcher:
        result = await matcher.match_song(SONG)

    assert result["status"] == "MANUAL"
    assert len(sleeps) == 9  # 每轮入口一次 sleep，恰好 9 次
    lo = (120 + 30) / 1000
    hi = (120 + 80) / 1000
    assert all(lo <= s <= hi for s in sleeps), f"sleep 时长越界: {sleeps}"


# ===================================================================
# F3-7（§7 响应层 b）：搜索 worker 接入熔断降并发倍率
# ===================================================================


@pytest.mark.asyncio
async def test_search_sleep_doubled_under_critical_breaker(
    respx_mock, tmp_path, monkeypatch,
) -> None:
    """F3-7 搜索侧：critical 熔断 multiplier=0.5 → 搜索 sleep 间隔翻倍补偿。"""
    from circuit_breaker import CircuitBreaker

    db = Database(tmp_path / "t.db")
    _mock_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_video("BV1m", "夜曲 周杰伦 官方MV")])
    )

    cfg = Config()
    cfg.rate_limit.search.interval_ms = 100
    cfg.rate_limit.search.jitter_ms = [0, 0]

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    breaker = CircuitBreaker()
    breaker._concurrency_cut = True  # critical 已触发（不构造暂停）
    assert breaker.concurrency_multiplier == 0.5

    matcher = Matcher(
        cfg, db,
        BiliSearchClient(httpx.AsyncClient(), db=db, breaker=breaker),
        NcmClient(httpx.AsyncClient()), httpx.AsyncClient(),
    )
    async with matcher:
        await matcher._search_cached(SONG, "夜曲 周杰伦")

    # base=100ms / 0.5 = 200ms
    assert sleeps == [0.2], f"critical 后搜索间隔应翻倍为 0.2s，实际 {sleeps}"
