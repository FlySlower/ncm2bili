"""fav.py 单测（里程碑 M5，文档 §5.3 / §9.3）。

覆盖：
- 幂等分支（已收藏 → DONE；视频不存在 → MANUAL；其他失败 → FAV_FAILED 记因，
  resume 只重跑阶段三）；
- 3000 首拆 4 夹；重复运行复用已有 media_id；
- -101 立即中止且不清断点；
- 建夹失败（-400）停止收藏阶段。
"""
from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx

from config import Config
from db import Database
from fav import (
    AuthExpiredError,
    BiliFavClient,
    FavError,
    FolderLimitError,
    fav_songs,
)

FOLDER_LIST_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
FOLDER_ADD_URL = "https://api.bilibili.com/x/v3/fav/folder/add"
RESOURCE_ADD_URL = "https://api.bilibili.com/x/v3/fav/resource/deal"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

COOKIE = "SESSDATA=s123; bili_jct=jct789; buvid3=b3; DedeUserID=1;"


def _ok(**data) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "data": data})


def _form_body(request: httpx.Request) -> dict[str, str]:
    """解析 httpx form 编码 POST body。"""
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def _song(key: str = "夜曲|周杰伦", bvid: str = "BV1xx", method: str = "SCORED") -> dict:
    name, artist = key.split("|")
    return {"name": name, "artist": artist, "bvid": bvid, "method": method}


def _client(db: Database, cfg: Config | None = None) -> BiliFavClient:
    return BiliFavClient(httpx.AsyncClient(), db, cfg or Config(), COOKIE)


def _mock_view(aid: int = 753394975) -> None:
    """mock 视频详情（bvid→aid 转换用，装饰器模式下用全局 respx router）。"""
    respx.get(VIEW_URL).mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"aid": aid}})
    )


# ---- 验收 1：幂等三分支 -----------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fav_already_in_folder_is_done(tmp_path) -> None:
    """已收藏（code 0）→ 视为成功 DONE，不记 fail_reason。"""
    db = Database(tmp_path / "t.db")
    _mock_view()
    respx.post(RESOURCE_ADD_URL).mock(return_value=httpx.Response(200, json={"code": 0}))
    client = _client(db)
    async with client._client:
        result = await client.fav_one(1, _song())

    assert result["status"] == "DONE"
    row = db.query_one("SELECT status, fail_reason FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "DONE"
    assert row["fail_reason"] is None


@pytest.mark.asyncio
@respx.mock
async def test_fav_video_not_found_goes_manual(tmp_path) -> None:
    """视频不存在（-404）→ 回 MANUAL 并注明原因。"""
    db = Database(tmp_path / "t.db")
    _mock_view()
    respx.post(RESOURCE_ADD_URL).mock(return_value=httpx.Response(200, json={"code": -404}))
    client = _client(db)
    async with client._client:
        result = await client.fav_one(1, _song())

    assert result["status"] == "MANUAL"
    assert "视频不存在" in result["fail_reason"]
    row = db.query_one("SELECT status, fail_reason FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "MANUAL"
    assert "视频不存在" in row["fail_reason"]


@pytest.mark.asyncio
@respx.mock
async def test_fav_other_failure_records_reason_and_continues(tmp_path) -> None:
    """其他失败（如 -403）→ 置 FAV_FAILED 记 fail_reason，不回 DONE（resume 重试阶段三）。"""
    db = Database(tmp_path / "t.db")
    # 预置 DONE（收藏阶段入口状态），失败后应回退为 FAV_FAILED
    db.upsert_song("夜曲|周杰伦", status="DONE", bvid="BV1xx", ncm_id=1)
    _mock_view()
    respx.post(RESOURCE_ADD_URL).mock(return_value=httpx.Response(200, json={"code": -403}))
    client = _client(db)
    async with client._client:
        result = await client.fav_one(1, _song())

    assert result["status"] == "FAV_FAILED"  # 继续，不使任务失败
    assert "收藏失败" in result["fail_reason"]
    row = db.query_one("SELECT status, fail_reason FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "FAV_FAILED"  # 与内存返回值一致（消除 PENDING/DONE 分歧）
    assert "code -403" in row["fail_reason"]


@pytest.mark.asyncio
@respx.mock
async def test_fav_non404_failure_keeps_bvid_for_resume(tmp_path) -> None:
    """文档 §9.3/§4.4：deal 返回非 -404/62002 失败 → DB status=FAV_FAILED 且 fail_reason 非空。

    已存 bvid 保留，resume 直接用它重跑阶段三 deal，不再重新匹配。
    """
    db = Database(tmp_path / "t.db")
    db.upsert_song("夜曲|周杰伦", task_id="t1", status="DONE", bvid="BV1keep", ncm_id=1)
    _mock_view()
    respx.post(RESOURCE_ADD_URL).mock(return_value=httpx.Response(200, json={"code": -10086}))
    client = _client(db)
    async with client._client:
        result = await client.fav_one(
            1, {"name": "夜曲", "artist": "周杰伦", "bvid": "BV1keep", "task_id": "t1"}
        )

    assert result["status"] == "FAV_FAILED"
    assert result["fail_reason"]
    row = db.query_one("SELECT status, fail_reason, bvid FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row["status"] == "FAV_FAILED"
    assert row["fail_reason"]
    assert row["bvid"] == "BV1keep"  # 已存 bvid 保留供 resume 直接重试


# ---- 验收 2：拆夹 + 复用 media_id --------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_3000_songs_split_into_4_folders(tmp_path) -> None:
    """3000 首 → ⌈3000/900⌉ = 4 个收藏夹，命名 "(1)~(4)"。"""
    db = Database(tmp_path / "t.db")
    respx.get(FOLDER_LIST_URL).mock(return_value=_ok(list=[]))  # 无既有夹

    titles: list[str] = []

    def add_handler(request: httpx.Request) -> httpx.Response:
        titles.append(_form_body(request).get("title", ""))
        return _ok(id=len(titles))

    respx.post(FOLDER_ADD_URL).mock(side_effect=add_handler)

    client = _client(db)
    async with client._client:
        media_ids = await client.ensure_folders("我的歌单", 3000)

    assert len(media_ids) == 4
    assert titles == ["我的歌单 (1)", "我的歌单 (2)", "我的歌单 (3)", "我的歌单 (4)"]
    assert media_ids == [1, 2, 3, 4]


@pytest.mark.asyncio
@respx.mock
async def test_rerun_reuses_existing_folder_media_id(tmp_path) -> None:
    """重复运行：按名称查询复用已有 media_id，不为同一任务重复建夹。"""
    db = Database(tmp_path / "t.db")
    respx.get(FOLDER_LIST_URL).mock(
        return_value=_ok(
            list=[
                {"id": 777, "title": "我的歌单 (1)"},
                {"id": 888, "title": "我的歌单 (2)"},
            ]
        )
    )
    # 若误调建夹接口，立即失败暴露
    respx.post(FOLDER_ADD_URL).mock(return_value=_ok(id=999))

    client = _client(db)
    async with client._client:
        media_ids = await client.ensure_folders("我的歌单", 1500)

    assert media_ids == [777, 888]  # 复用既有夹，未新建
    add_calls = [c for c in respx.calls if "folder/add" in str(c.request.url)]
    assert add_calls == []


# ---- 验收 3：-101 立即中止且不清断点 ----------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_minus101_aborts_and_keeps_breakpoint(tmp_path) -> None:
    """-101（凭证失效）→ 抛 AuthExpiredError 立即中止，已 DONE 的歌保留断点。"""
    db = Database(tmp_path / "t.db")
    db.upsert_song("歌A|艺A", status="DONE", method="SCORED", bvid="BV1a")  # 已完成断点
    _mock_view()

    calls = {"n": 0}

    def add_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": -101, "message": "未登录"})

    respx.post(RESOURCE_ADD_URL).mock(side_effect=add_handler)
    client = _client(db)
    async with client._client:
        with pytest.raises(AuthExpiredError):
            await client.fav_one(1, _song("歌B|艺B"))

    # 断点未清：歌A 仍 DONE，歌B 未落盘任何终态
    row_a = db.query_one("SELECT status FROM songs WHERE song_key = '歌A|艺A'")
    assert row_a["status"] == "DONE"
    row_b = db.query_one("SELECT status FROM songs WHERE song_key = '歌B|艺B'")
    assert row_b is None


@pytest.mark.asyncio
@respx.mock
async def test_folder_limit_400_stops(tmp_path) -> None:
    """建夹失败（99 上限 -400）→ FolderLimitError，停止收藏阶段。"""
    db = Database(tmp_path / "t.db")
    respx.get(FOLDER_LIST_URL).mock(return_value=_ok(list=[]))
    respx.post(FOLDER_ADD_URL).mock(return_value=httpx.Response(200, json={"code": -400}))

    client = _client(db)
    async with client._client:
        with pytest.raises(FolderLimitError):
            await client.ensure_folders("歌单", 3000)


# ---- 批量收藏：并发 + jitter 可跑 -------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fav_songs_batch(tmp_path) -> None:
    """fav_songs：自动拆夹 + 并发收藏 + 状态汇总。"""
    db = Database(tmp_path / "t.db")
    _mock_view()
    respx.get(FOLDER_LIST_URL).mock(return_value=_ok(list=[]))
    respx.post(FOLDER_ADD_URL).mock(return_value=_ok(id=1))
    respx.post(RESOURCE_ADD_URL).mock(
        return_value=httpx.Response(200, json={"code": 0})
    )

    songs = [_song(f"歌{i}|艺{i}", f"BV1x{i}") for i in range(10)]
    client = _client(db)
    async with client._client:
        summary = await fav_songs(client, db, Config(), songs, "批量歌单")

    assert summary["total"] == 10
    assert summary["statuses"]["DONE"] == 10
    done = db.query_one("SELECT COUNT(*) AS n FROM songs WHERE status = 'DONE'")
    assert done["n"] == 10


# ---- F1-3：按序连续分段 + resume 稳定映射（文档 §5.3）-----------------


@pytest.mark.asyncio
@respx.mock
async def test_fav_songs_sequential_segmentation_no_roundrobin(tmp_path) -> None:
    """F1-3（§5.3）：5 首歌 → 3 夹（per_folder_limit=2），按序连续分段禁止轮转。

    song_key 字典序 "歌1|艺1" < ... < "歌5|艺5" 形成 ordinal 0-4，验证
    第 1,2 首 → 夹 1（media_id=10）；第 3,4 首 → 夹 2（media_id=20）；
    第 5 首 → 夹 3（media_id=30）。若为 round-robin 则每首进不同夹。
    """
    db = Database(tmp_path / "t.db")
    # 预置 5 首歌 DONE（含 bvid，供 ordinal 查询稳定定序）
    for i in range(1, 6):
        db.upsert_song(
            f"歌{i}|艺{i}", task_id="t1", status="DONE",
            bvid=f"BV1x{i}", ncm_id=i,
        )
    # VIEW：按 bvid 末位返回唯一 aid（aid=2000+i 便于反查）
    def view_handler(request: httpx.Request) -> httpx.Response:
        bvid = parse_qs(request.url.query.decode()).get("bvid", [""])[0]
        i = int(bvid[-1])
        return httpx.Response(200, json={"code": 0, "data": {"aid": 2000 + i}})
    respx.get(VIEW_URL).mock(side_effect=view_handler)
    # 建夹：3 个 media_id 10/20/30
    respx.get(FOLDER_LIST_URL).mock(return_value=_ok(list=[]))
    folder_seq = [0]

    def add_handler(request: httpx.Request) -> httpx.Response:
        folder_seq[0] += 1
        return _ok(id=folder_seq[0] * 10)  # 10, 20, 30

    respx.post(FOLDER_ADD_URL).mock(side_effect=add_handler)
    # RESOURCE deal：捕获 (media_id, aid) 供反查
    added: list[tuple[int, int]] = []

    def resource_handler(request: httpx.Request) -> httpx.Response:
        body = _form_body(request)
        added.append((int(body["add_media_ids"]), int(body["rid"])))
        return httpx.Response(200, json={"code": 0})

    respx.post(RESOURCE_ADD_URL).mock(side_effect=resource_handler)

    songs = [_song(f"歌{i}|艺{i}", f"BV1x{i}") for i in range(1, 6)]
    for s in songs:
        s["task_id"] = "t1"

    cfg = Config()
    cfg.fav.per_folder_limit = 2
    cfg.rate_limit.fav.interval_ms = 0
    cfg.rate_limit.fav.jitter_ms = [0, 0]
    client = _client(db, cfg)
    async with client._client:
        summary = await fav_songs(client, db, cfg, songs, "歌单")

    assert summary["total"] == 5
    assert summary["statuses"]["DONE"] == 5
    # 建夹数 = ceil(5/2) = 3
    assert folder_seq[0] == 3
    # 反查 aid → media_id，验证按序连续分段（非 round-robin）
    aid_to_media = {aid: mid for mid, aid in added}
    for i in range(1, 6):
        aid = 2000 + i
        expected_folder = (i - 1) // 2  # 0,0,1,1,2
        expected_media_id = [10, 20, 30][expected_folder]
        assert aid_to_media[aid] == expected_media_id, (
            f"song {i}: media_id={aid_to_media[aid]}, expected={expected_media_id}"
        )


@pytest.mark.asyncio
@respx.mock
async def test_fav_songs_resume_uses_total_matched_for_folders(tmp_path) -> None:
    """F1-3（§5.3）：resume 只传失败子集，建夹按任务总匹配数（非子集数）。

    5 首中 s3/s5 FAV_FAILED 需 resume，s1/s2/s4 DONE。传 s3/s5 子集：
    - 建夹数按 total_matched=5 → ceil(5/2)=3 夹（非 2 首的 1 夹）；
    - s3 ordinal=2 → 夹 2（media_id=20）、s5 ordinal=4 → 夹 3（media_id=30），
      按 global ordinal（非 round-robin 的 10/20）。
    """
    db = Database(tmp_path / "t.db")
    # 全 5 首入 DB：s1/s2/s4 DONE，s3/s5 FAV_FAILED（仍带 bvid，供 ordinal 查询）
    for i in range(1, 6):
        db.upsert_song(
            f"歌{i}|艺{i}", task_id="t1",
            status="DONE" if i in (1, 2, 4) else "FAV_FAILED",
            bvid=f"BV1x{i}", ncm_id=i,
        )

    def view_handler(request: httpx.Request) -> httpx.Response:
        bvid = parse_qs(request.url.query.decode()).get("bvid", [""])[0]
        i = int(bvid[-1])
        return httpx.Response(200, json={"code": 0, "data": {"aid": 2000 + i}})

    respx.get(VIEW_URL).mock(side_effect=view_handler)
    respx.get(FOLDER_LIST_URL).mock(return_value=_ok(list=[]))
    folder_seq = [0]

    def add_handler(request: httpx.Request) -> httpx.Response:
        folder_seq[0] += 1
        return _ok(id=folder_seq[0] * 10)

    respx.post(FOLDER_ADD_URL).mock(side_effect=add_handler)
    added: list[tuple[int, int]] = []

    def resource_handler(request: httpx.Request) -> httpx.Response:
        body = _form_body(request)
        added.append((int(body["add_media_ids"]), int(body["rid"])))
        return httpx.Response(200, json={"code": 0})

    respx.post(RESOURCE_ADD_URL).mock(side_effect=resource_handler)

    # resume 只传失败子集 s3, s5
    songs = [_song(f"歌{i}|艺{i}", f"BV1x{i}") for i in (3, 5)]
    for s in songs:
        s["task_id"] = "t1"

    cfg = Config()
    cfg.fav.per_folder_limit = 2
    cfg.rate_limit.fav.interval_ms = 0
    cfg.rate_limit.fav.jitter_ms = [0, 0]
    client = _client(db, cfg)
    async with client._client:
        summary = await fav_songs(client, db, cfg, songs, "歌单")

    assert summary["total"] == 2
    assert summary["statuses"]["DONE"] == 2
    # 建夹数：按 total_matched=5 → 3 夹（非 2 首的 1 夹）
    assert folder_seq[0] == 3
    # 反查 aid → media_id（按 global ordinal，非 round-robin）
    aid_to_media = {aid: mid for mid, aid in added}
    # s3 ordinal=2 → folder_idx=2//2=1 → media_id=20（非 round-robin 的 10）
    assert aid_to_media[2003] == 20
    # s5 ordinal=4 → folder_idx=4//2=2 → media_id=30（非 round-robin 的 20）
    assert aid_to_media[2005] == 30


# ---- 412 退避 + 熔断（文档 §7 响应层 a/b）------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_list_folders_412_retries_with_backoff(tmp_path) -> None:
    """验收 1：list_folders 返回 412 → 指数退避重试后成功，不抛异常。"""
    from circuit_breaker import CircuitBreaker

    db = Database(tmp_path / "t.db")
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    breaker = CircuitBreaker(sleep=fake_sleep)  # 复用熔断器，验证退避间隔

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, json={"code": -412, "message": "请求过于频繁"})
        return _ok(list=[])

    respx.get(FOLDER_LIST_URL).mock(side_effect=handler)

    client = BiliFavClient(
        httpx.AsyncClient(), db, Config(), COOKIE,
        retry_delays_s=[0.01, 0.01, 0.01], breaker=breaker,  # 退避小间隔（测试快）
    )
    async with client._client:
        folders = await client.list_folders()

    assert calls["n"] == 3  # 2 次 412 + 1 次成功
    assert folders == []
    # 熔断记录过 -412（复用现有 breaker 断言）
    assert breaker.recent_failures >= 1


@pytest.mark.asyncio
@respx.mock
async def test_resource_add_412_trips_breaker(tmp_path) -> None:
    """验收 2：连续 412 触发全局熔断 → 等待暂停，不裸抛 HTTPStatusError。"""
    from circuit_breaker import CircuitBreaker

    db = Database(tmp_path / "t.db")
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    breaker = CircuitBreaker(sleep=fake_sleep)
    breaker._sleep = fake_sleep
    _mock_view()

    respx.post(RESOURCE_ADD_URL).mock(
        return_value=httpx.Response(200, json={"code": -412, "message": "请求过于频繁"})
    )

    client = BiliFavClient(
        httpx.AsyncClient(), db, Config(), COOKIE,
        retry_delays_s=[0.01, 0.01, 0.01], breaker=breaker,
    )
    async with client._client:
        with pytest.raises(FavError):  # 重试耗尽后 FavError（非 HTTPStatusError 裸崩）
            await client.add_resource(1, "BV1xx")

    # 3 次 -412 触发 warn 熔断 → 至少一次 60s 暂停 sleep
    assert breaker.recent_failures >= 3
    assert any(s >= 60 for s in sleeps), f"未触发全局暂停，sleeps={sleeps}"


@pytest.mark.asyncio
@respx.mock
async def test_minus101_aborts_immediately_no_retry(tmp_path) -> None:
    """验收 3：-101 立即中止语义不回归（不重试、AuthExpiredError）。"""
    from fav import AuthExpiredError

    db = Database(tmp_path / "t.db")
    _mock_view()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": -101, "message": "账号未登录"})

    respx.post(RESOURCE_ADD_URL).mock(side_effect=handler)

    client = BiliFavClient(httpx.AsyncClient(), db, Config(), COOKIE)
    async with client._client:
        with pytest.raises(AuthExpiredError):
            await client.add_resource(1, "BV1xx")

    assert calls["n"] == 1  # 不重试，立即中止


# ---- list-all up_mid + header 断言（诊断 2026-09-22）------------------


@pytest.mark.asyncio
@respx.mock
async def test_list_folders_sends_up_mid_and_referer(tmp_path) -> None:
    """验收：list-all 请求参数含 up_mid（来自 cookie DedeUserID），header 含 Referer。"""
    db = Database(tmp_path / "t.db")

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        captured["referer"] = request.headers.get("Referer", "")
        captured["ua"] = request.headers.get("User-Agent", "")
        return _ok(list=[{"id": 777, "title": "歌单1 (1)"}])

    respx.get(FOLDER_LIST_URL).mock(side_effect=handler)

    client = BiliFavClient(httpx.AsyncClient(), db, Config(), COOKIE)  # DedeUserID=1
    async with client._client:
        folders = await client.list_folders()

    assert folders == [{"id": 777, "title": "歌单1 (1)"}]
    assert captured["params"].get("up_mid") == "1"  # 必填参数 up_mid
    assert captured["referer"].startswith("https://www.bilibili.com")
    assert captured["ua"]  # 与搜索一致的浏览器 UA
    assert "python" not in captured["ua"].lower()


@pytest.mark.asyncio
@respx.mock
async def test_list_folders_mid_fallback_to_nav(tmp_path) -> None:
    """DedeUserID 缺失时，nav 接口 data.mid 兜底（up_mid 仍正确携带）。"""
    db = Database(tmp_path / "t.db")
    cookie_no_mid = "SESSDATA=s123; bili_jct=jct789; buvid3=b3;"

    respx.get(NAV_URL).mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"mid": 888, "isLogin": True}})
    )

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return _ok(list=[])

    respx.get(FOLDER_LIST_URL).mock(side_effect=handler)

    client = BiliFavClient(httpx.AsyncClient(), db, Config(), cookie_no_mid)
    async with client._client:
        await client.list_folders()

    assert captured["params"].get("up_mid") == "888"  # nav 兜底 mid


@pytest.mark.asyncio
@respx.mock
async def test_list_folders_mid_unresolved_raises(tmp_path) -> None:
    """DedeUserID 与 nav 均无法得到 mid → AuthExpiredError（提示重新 auth）。"""
    from fav import AuthExpiredError

    db = Database(tmp_path / "t.db")
    cookie_no_mid = "SESSDATA=s123; bili_jct=jct789; buvid3=b3;"

    respx.get(NAV_URL).mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"isLogin": False}})
    )

    client = BiliFavClient(httpx.AsyncClient(), db, Config(), cookie_no_mid)
    async with client._client:
        with pytest.raises(AuthExpiredError):
            await client.list_folders()


# ---- -702 目标账号限流（2026-09-22 补充）-------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_minus702_first_hit_backoff_then_success(tmp_path, monkeypatch) -> None:
    """验收 1：首次 -702 → 退避序列 [4,8,16]（2×initial）、熔断计数 +1、最终 DONE。"""
    from circuit_breaker import CircuitBreaker
    from fav import RATE_LIMIT_CODE

    db = Database(tmp_path / "t.db")
    _mock_view()

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("fav.sleep_before_retry", fake_sleep)

    breaker = CircuitBreaker(sleep=fake_sleep)
    breaker._sleep = fake_sleep

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # 首次 deal 返回 -702（HTTP 200）
            return httpx.Response(200, json={"code": RATE_LIMIT_CODE, "message": "请求频率过高"})
        return httpx.Response(200, json={"code": 0})

    respx.post(RESOURCE_ADD_URL).mock(side_effect=handler)

    client = BiliFavClient(
        httpx.AsyncClient(), db, Config(), COOKIE, breaker=breaker,
    )
    async with client._client:
        result = await client.fav_one(1, _song())

    assert result["status"] == "DONE"  # 退避后成功
    assert calls["n"] == 2  # 1 次 -702 + 1 次成功
    # 注：第 1 次退避在循环 attempt=1 时发生，rate 序列第 1 项为 2*initial=4
    rate_delays = [d for d in sleeps if d >= 1]
    assert rate_delays == [4.0], f"期望退避 [4]，实际 {rate_delays}"
    assert breaker.recent_failures >= 1  # -702 计入熔断计数


@pytest.mark.asyncio
@respx.mock
async def test_minus702_consecutive_trips_slowdown(tmp_path, monkeypatch) -> None:
    """验收 2：连续 -702 达到 2 次 → 自适应降速 interval 翻倍（客户端乘数）。"""
    from fav import RATE_LIMIT_CODE, RATE_LIMIT_MAX_MULTIPLIER

    db = Database(tmp_path / "t.db")
    _mock_view()

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("fav.sleep_before_retry", fake_sleep)

    # 前 2 次请求都 -702 → 触发降速标记；后续成功
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(200, json={"code": RATE_LIMIT_CODE, "message": "请求频率过高"})
        return httpx.Response(200, json={"code": 0})

    respx.post(RESOURCE_ADD_URL).mock(side_effect=handler)

    client = BiliFavClient(httpx.AsyncClient(), db, Config(), COOKIE)
    async with client._client:
        result = await client.fav_one(1, _song())

    assert result["status"] == "DONE"
    # 连续 2 次 -702 → interval 乘数 x2（上限 x4）
    assert client._slowdown_multiplier == 2.0
    assert client._slowdown_multiplier <= RATE_LIMIT_MAX_MULTIPLIER


@pytest.mark.asyncio
@respx.mock
async def test_minus702_retry_exhausted_records_reason(tmp_path, monkeypatch) -> None:
    """验收 3：-702 一直存在、退避耗尽 → 落 fail_reason（非中断整体）。"""
    from fav import RATE_LIMIT_CODE

    db = Database(tmp_path / "t.db")
    _mock_view()

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("fav.sleep_before_retry", fake_sleep)

    reserved = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        reserved["n"] += 1
        return httpx.Response(200, json={"code": RATE_LIMIT_CODE, "message": "请求频率过高"})

    respx.post(RESOURCE_ADD_URL).mock(side_effect=handler)

    client = BiliFavClient(httpx.AsyncClient(), db, Config(), COOKIE)
    async with client._client:
        result = await client.fav_one(1, _song())

    assert result["status"] == "FAV_FAILED"  # 限流耗尽 → FAV_FAILED，非中断整体
    assert "限流" in result["fail_reason"] or "请求失败" in result["fail_reason"]
    row = db.query_one("SELECT status, fail_reason FROM songs WHERE song_key = '夜曲|周杰伦'")
    assert row is not None and row["fail_reason"]  # fail_reason 已落
    assert row["status"] == "FAV_FAILED"  # 与内存返回值一致
    # 连续 -702 已触发自适应降速（重试耗尽后计数重置，但乘数保留）
    assert client._slowdown_multiplier > 1.0


# ---- 收藏阶段接入全局熔断器（文档 §7 响应层 b）-------------------------


@pytest.mark.asyncio
@respx.mock
async def test_minus412_counts_into_global_circuit_breaker(tmp_path, monkeypatch) -> None:
    """验收：收藏阶段 -412 → 计入全局熔断滑动窗口，达阈值触发暂停+降并发。"""
    from circuit_breaker import CircuitBreaker

    db = Database(tmp_path / "t.db")
    respx.get(VIEW_URL).mock(  # bvid→aid 转换
        return_value=httpx.Response(200, json={"code": 0, "data": {"aid": 753394975}})
    )

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("fav.sleep_before_retry", fake_sleep)

    breaker = CircuitBreaker(sleep=fake_sleep)
    breaker._sleep = fake_sleep

    respx.post(RESOURCE_ADD_URL).mock(
        return_value=httpx.Response(200, json={"code": -412, "message": "请求过于频繁"})
    )

    # 注入 breaker 的收藏客户端（与搜索同款接线，main.py 构造路径）
    client = BiliFavClient(
        httpx.AsyncClient(), db, Config(), COOKIE, breaker=breaker,
    )
    async with client._client:
        result = await client.fav_one(1, _song())

    # -412 重试耗尽 → 收藏失败置 FAV_FAILED（fav_one 吞掉 FavError，不中断整体）
    assert result["status"] == "FAV_FAILED"
    assert breaker.recent_failures >= 3  # 每次 -412 计入滑动窗口
    assert breaker.is_paused  # 达到 warn 阈值 3 次 → 已触发全局暂停
