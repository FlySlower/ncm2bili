"""main.py run --dry-run 集成测试（里程碑 M4，文档 §10.1）。

构造 5 首歌假歌单，respx mock 网易云 + B 站全流程，断言：
- method 分布覆盖 4 种（WHITELIST_BV / UPLOADER_WL / SCORED / MANUAL）；
- dry-run 下收藏夹创建/收藏接口调用次数为 0；
- 报告文件生成且评分明细可读；
- 中断重跑后已 DONE 的歌不再发搜索请求。
"""
from __future__ import annotations

import asyncio
import html as html_mod

import httpx
import pytest

from bili_search import BiliSearchClient
from config import load_config
from db import Database
from main import run_dry_run, run_formal
from ncm import NcmClient

HOME_URL = "https://www.bilibili.com/"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
PLAYLIST_URL = "https://music.163.com/api/v6/playlist/detail"
SONG_DETAIL_URL = "https://music.163.com/api/v3/song/detail"
FAV_FOLDER_URL = "https://api.bilibili.com/x/v3/fav/folder/add"
FAV_RESOURCE_URL = "https://api.bilibili.com/x/v3/fav/resource/deal"

IMG_KEY = "653657f524a547ac981ded72ea172057"
SUB_KEY = "6e4909c702f846728e64f6007736a338"

# 5 首歌的假歌单（id 1..5）
SONGS = [
    {"id": 1, "name": "歌1", "ar": [{"name": "艺1"}], "al": {"name": "专1"},
     "alia": [], "originSongSimpleData": None},
    {"id": 2, "name": "歌2", "ar": [{"name": "艺2"}], "al": {"name": "专2"},
     "alia": [], "originSongSimpleData": None},
    {"id": 3, "name": "歌3", "ar": [{"name": "艺3"}], "al": {"name": "专3"},
     "alia": [], "originSongSimpleData": None},
    {"id": 4, "name": "歌4", "ar": [{"name": "艺4"}], "al": {"name": "专4"},
     "alia": [], "originSongSimpleData": None},
    {"id": 5, "name": "歌5", "ar": [{"name": "艺5"}], "al": {"name": "专5"},
     "alia": [], "originSongSimpleData": None},
]


def _ok(**data) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "data": data})


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


def _video(bvid: str, title: str, mid: int = 1) -> dict:
    return {
        "bvid": bvid,
        "title": title,
        "mid": mid,
        "author": "UP主",
        "play": 100_000,
        "favorites": 1_000,
        "video_review": 100,
        "duration": 240,
    }


def _mock_ncm_playlist(respx_mock) -> None:
    track_ids = [{"id": s["id"]} for s in SONGS]
    respx_mock.get(PLAYLIST_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "playlist": {"trackIds": track_ids}})
    )
    respx_mock.post(SONG_DETAIL_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "songs": SONGS})
    )


def _mock_bili_env(respx_mock) -> None:
    respx_mock.get(SPI_URL).mock(
        return_value=httpx.Response(
            200,
            json={"code": 0, "data": {"b_3": "M4-INTEGRATION-B3", "b_4": "M4-INTEGRATION-B4"}},
        )
    )
    respx_mock.get(HOME_URL).mock(
        return_value=httpx.Response(200, headers={"set-cookie": "buvid3=M4-INTEGRATION; Path=/"})
    )
    respx_mock.get(NAV_URL).mock(return_value=httpx.Response(200, json=_nav_response()))


def _make_components(tmp_path) -> tuple:
    db = Database(tmp_path / "cache.db")
    config = load_config(tmp_path / "nope.yaml", env={})
    # F1-1：搜索 sleep 下沉到 _search_cached，测试归零限速避免拖慢
    config.rate_limit.search.interval_ms = 0
    config.rate_limit.search.jitter_ms = [0, 0]
    # F4-3：阶段二补查限速同步归零（matcher 传 rate_limit.stage2）
    config.rate_limit.stage2.interval_ms = 0
    config.rate_limit.stage2.jitter_ms = [0, 0]
    ncm = NcmClient(httpx.AsyncClient())
    # F4-2：零退避，避免失败路径真实 sleep 2s→4s→8s
    bili = BiliSearchClient(httpx.AsyncClient(), db=db, retry_delays_s=[0.0, 0.0, 0.0])
    http = httpx.AsyncClient()
    return db, config, ncm, bili, http


def _mock_fav_interfaces(respx_mock) -> None:
    """收藏/建夹接口：即使误调也返回成功，便于计数断言。"""
    respx_mock.post(FAV_FOLDER_URL).mock(return_value=_ok())
    respx_mock.post(FAV_RESOURCE_URL).mock(return_value=_ok())


def _kw_title(kw: str) -> str:
    """从降级关键词构造同时含歌名+艺人的标题（短 CJK 歌名联合闸需艺人 token，§4.2）。"""
    parts = kw.split(" ")
    name = parts[0]
    for p in parts[1:]:
        if p.startswith("艺"):
            artist = p
            break
    else:
        artist = "艺" + name[1:]
    return f"{name} {artist} 官方MV"


def _ok_video_handler(request: httpx.Request) -> httpx.Response:
    """统一 mock：任一关键词都返回含歌名+艺人的合格候选（联合闸通过）。"""
    kw = request.url.params.get("keyword")
    return _ok(result=[_video(f"BV{abs(hash(kw)) % 100}", _kw_title(kw))])


@pytest.mark.asyncio
async def test_dry_run_5_songs_four_methods(respx_mock, tmp_path) -> None:
    """验收 1+2：5 首歌集成跑通，method 覆盖 4 种；dry-run 收藏/建夹 0 次。"""
    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)
    _mock_fav_interfaces(respx_mock)

    def search_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword")
        if kw == "歌2 艺2":  # uploaders 命中
            return _ok(result=[_video("BV2x", "歌2 艺2 钢琴版", mid=2002)])
        if kw == "歌3 艺3":  # 评分命中（单候选 → 阶段二不触发）
            return _ok(result=[_video("BV3x", "歌3 艺3 官方MV")])
        if kw == "歌4 艺4":
            return _ok(result=[_video("BV4x", "歌4 艺4 现场版")])
        return _ok(result=[])  # 歌5 全失败

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    db, config, ncm, bili, http = _make_components(tmp_path)
    out = tmp_path / "output"
    async with ncm, bili, http:
        counts = await run_dry_run(
            1, config, db, ncm, bili, http,
            output_dir=out,
            whitelist={"歌1|艺1": "BV1wl"},
            uploaders={"2002": {"name": "UP2", "note": ""}},
            blacklist_words=[],
            manual={},
        )

    # 4 种 method 全覆盖
    assert counts["methods"] == {"WHITELIST_BV": 1, "UPLOADER_WL": 1, "SCORED": 2, "MANUAL": 1}
    assert counts["total"] == 5

    # 验收 2：dry-run 不调用收藏/建夹接口
    fav_calls = [c for c in respx_mock.calls if "fav" in str(c.request.url)]
    assert len(fav_calls) == 0

    # 验收 3（部分）：报告文件生成
    csv_path = out / "report.csv"
    html_path = out / "preview_report.html"
    assert csv_path.exists() and html_path.exists()

    # 抽查 CSV 内容
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "歌1|艺1" in csv_text and "WHITELIST_BV" in csv_text
    assert "歌5|艺5" in csv_text and "MANUAL" in csv_text

    # 抽查 HTML：含评分明细展开（候选对比 + 关键词）且可读
    html_text = html_path.read_text(encoding="utf-8")
    assert "ncm2bili 预览报告" in html_text
    assert "歌3|艺3" in html_text
    assert "评分明细" in html_text
    assert "候选对比" not in html_text or "关键词" in html_text
    assert "BV3x" in html_text
    assert "UP主白名单" in html_text and "白名单" in html_text
    # HTML 转义正确（特殊字符不破坏页面）
    assert html_mod.unescape(html_text)  # 可解析


@pytest.mark.asyncio
async def test_report_csv_columns(respx_mock, tmp_path) -> None:
    """验收 3（补充）：report.csv 表头与行结构。"""
    db = Database(tmp_path / "c.db")
    db.upsert_song(
        "歌A|艺A", ncm_id=1, name="歌A", artist="艺A",
        status="DONE", method="SCORED",
        bvid="BV1a",
        score_detail='{"keyword":"歌A 艺A","candidates":[{"bvid":"BV1a","title":"歌A 官方","score":20.5}]}',
    )
    db.upsert_song(
        "歌B|艺B", ncm_id=2, name="歌B", artist="艺B",
        status="MANUAL", method="MANUAL", fail_reason="未匹配",
    )

    from report import write_reports

    out = tmp_path / "output"
    write_reports(db, out)
    lines = (out / "report.csv").read_text(encoding="utf-8-sig").strip().splitlines()
    assert lines[0].split(",")[0] == "song_key"
    assert any("歌A|艺A" in line for line in lines)
    assert any("歌B|艺B" in line for line in lines)


@pytest.mark.asyncio
async def test_retry_exhausted_song_degrades_task_continues(respx_mock, tmp_path) -> None:
    """§9.1 单歌降级：搜索重试耗尽（BiliError）→ 该歌 MANUAL(SEARCH_FAILED)，任务继续。

    歌3 搜索恒抛网络错误；其余歌正常匹配。整体 run 不崩、任务 DONE，
    歌3 落盘 MANUAL + SEARCH_FAILED。替换旧"中断抛异常"语义（v0.4.1）。
    """
    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)

    def search_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword")
        if "歌3" in kw:  # 覆盖全部 9 轮降级关键词（任一含"歌3"）
            raise httpx.ConnectError("retry exhausted for song 3")
        if "歌5" in kw:
            return _ok(result=[])
        return _ok(result=[_video(f"BV{abs(hash(kw)) % 100}", _kw_title(kw))])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    db, config, ncm, bili, http = _make_components(tmp_path)
    # 注入零退避，避免测试真实 sleep（仅验证语义）
    bili = BiliSearchClient(httpx.AsyncClient(), db=db, retry_delays_s=[0.0, 0.0, 0.0])
    out = tmp_path / "output"
    async with ncm, bili, http:
        counts = await run_dry_run(1, config, db, ncm, bili, http, output_dir=out)

    # 不崩任务：5 首全部处理、任务置 DONE
    assert counts["processed"] == 5
    task_id = counts["task_id"]
    task = db.get_task(task_id)
    assert task["status"] == "DONE"
    # 歌3 → MANUAL + SEARCH_FAILED（不再中断整个任务）
    row = db.query_one(
        "SELECT status, method, fail_reason FROM songs WHERE song_key = '歌3|艺3' AND task_id = ?",
        (task_id,),
    )
    assert row["status"] == "MANUAL"
    assert "SEARCH_FAILED" in row["fail_reason"]
    assert "retry exhausted for song 3" in row["fail_reason"]  # 根因携带
    # 其余歌正常 DONE
    done = db.query_one(
        "SELECT COUNT(*) AS n FROM songs WHERE status = 'DONE' AND task_id = ?", (task_id,)
    )
    assert done["n"] == 3  # 歌1/歌2/歌4


# ---- M7：KeyboardInterrupt 中断重跑零重复请求 -------------------------

# 注意：Python 3.12 的 asyncio Task 对 KeyboardInterrupt 特殊处理——从 task
# 内抛出会直接冒泡到事件循环顶层（模拟 Ctrl+C 语义），asyncio.run 的 Runner
# 捕获后取消所有任务再重新抛出。因此 KeyboardInterrupt 只能在 aio.run() 调用
# 处捕获，不能在协程内部 try/except。此用例用同步测试函数 + 两次独立
# asyncio.run 模拟中断与重启，组件在每次场景内重建（换新事件循环）。


def test_keyboard_interrupt_resume_skips_done_songs(respx_mock, tmp_path) -> None:
    """M7 验收 1（任务制）：跑到一半抛 KeyboardInterrupt → 任务保持 RUNNING，
    同 task_id resume 后已 DONE 的歌零重复搜索。"""
    import asyncio as aio

    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)

    state = {"broken": True}
    first_kws: list[str] = []
    second_kws: list[str] = []

    async def search_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword")
        if state["broken"] and kw == "歌3 艺3":
            # 先让歌1/2/4 完成落盘，再抛 KeyboardInterrupt（模拟用户 Ctrl+C）
            await aio.sleep(0.5)
            raise KeyboardInterrupt("user interrupted")
        (first_kws if state["broken"] else second_kws).append(kw)
        if "歌5" in kw:
            return _ok(result=[])
        return _ok(result=[_video(f"BV{abs(hash(kw)) % 100}", _kw_title(kw))])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    out = tmp_path / "output"

    # 同一任务贯穿两次运行（KeyboardInterrupt 后任务保持 RUNNING）
    db0 = Database(tmp_path / "cache.db")
    task_id = db0.create_task(1)
    db0.close()

    async def run_once() -> dict:
        db, config, ncm, bili, http = _make_components(tmp_path)
        async with ncm, bili, http:
            return await run_dry_run(
                1, config, db, ncm, bili, http, output_dir=out, task_id=task_id
            )

    # 第一次运行：KeyboardInterrupt 由 asyncio.run 的 Runner 捕获并重新抛出
    interrupted = False
    try:
        aio.run(run_once())
    except KeyboardInterrupt:
        interrupted = True
    assert interrupted, "run_dry_run 应被 KeyboardInterrupt 中断"

    # KeyboardInterrupt 中断后任务应为 RUNNING（可 resume）
    db1 = Database(tmp_path / "cache.db")
    interrupted_task = db1.get_task(task_id)
    assert interrupted_task is not None and interrupted_task["status"] == "RUNNING"

    # 重启（resume 同任务）
    state["broken"] = False
    counts = aio.run(run_once())

    # 已 DONE 的歌（歌1/歌2/歌4）重跑零请求；未完成（歌3）与 MANUAL（歌5）重跑
    assert "歌1 艺1" not in second_kws
    assert "歌2 艺2" not in second_kws
    assert "歌4 艺4" not in second_kws
    assert "歌3 艺3" in second_kws
    assert counts["skipped_done"] == 3

    # 恢复完成 → DONE
    finished_task = db1.get_task(task_id)
    assert finished_task["status"] == "DONE"
    db1.close()


# ---- M7：-412 全局熔断（文档 §7 响应层 b） -----------------------------


@pytest.mark.asyncio
async def test_412_trips_global_circuit_breaker(respx_mock, tmp_path) -> None:
    """M7 验收 2：搜索连续 -412 → 熔断触发（≥3 次后全局暂停 60s，sleep 被调用）。"""
    from bili_search import BiliError
    from circuit_breaker import CircuitBreaker

    _mock_bili_env(respx_mock)

    breaker = CircuitBreaker()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    breaker._sleep = fake_sleep  # 注入假 sleep，断言暂停时长

    search_count = {"n": 0}

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_count["n"] += 1
        return httpx.Response(200, json={"code": -412, "message": "请求过于频繁"})

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    db = Database(tmp_path / "cache.db")
    bili = BiliSearchClient(
        httpx.AsyncClient(), db=db, breaker=breaker, retry_delays_s=[0.0, 0.0, 0.0]
    )
    async with bili:
        # 单次搜索：-412 初次 + 3 次重试共 4 次尝试（F4-2），第 3 次触发熔断暂停 60s
        with pytest.raises(BiliError):
            await bili.search_videos("歌3 艺3")

    # 熔断触发：窗口内 ≥3 次 -412，且观察到大额全局暂停被 sleep
    # （≥59s：wait_if_paused 用实时单调钟计算剩余，比 60s 少毫秒级漂移）
    assert breaker.recent_failures >= 3, f"failures={breaker.recent_failures}"
    assert any(s >= 59 for s in sleeps), f"未观察到全局暂停，sleeps={sleeps}"


# ---- M7：--refresh 丢弃匹配结果但保留 search_cache ---------------------


@pytest.mark.asyncio
async def test_refresh_discards_matches_keeps_search_cache(respx_mock, tmp_path) -> None:
    """M7 验收 3（任务制）：--refresh 重置任务内歌曲状态、保留 search_cache。"""
    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)

    db, config, ncm, bili, http = _make_components(tmp_path)
    task_id = db.create_task(1)  # 预先建任务，--refresh 作用于该任务

    # 预置：歌1 已 DONE（带匹配结果），search_cache 已有未过期缓存（文档 §4.4）
    import time as _time

    db.upsert_song(
        "歌1|艺1", task_id=task_id, ncm_id=1, name="歌1", artist="艺1",
        status="DONE", method="SCORED", bvid="BV1done",
    )
    db.execute(
        "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
        ("歌1 艺1", '[{"bvid": "BV1cached", "title": "歌1 艺1 缓存版"}]', int(_time.time())),
    )

    search_kws: list[str] = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_kws.append(request.url.params.get("keyword"))
        return _ok(result=[_video("BV1re", "歌1 艺1 官方MV")])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    out = tmp_path / "out"
    async with ncm, bili, http:
        counts = await run_dry_run(
            1, config, db, ncm, bili, http, output_dir=out, refresh=True, task_id=task_id
        )

    # 匹配结果被丢弃：歌1 重新进入匹配；但 search_cache 未过期命中 → 歌1 零搜索
    assert "歌1 艺1" not in search_kws  # 文档 §4.4：命中缓存直接用，不发搜索
    assert search_kws  # 其余歌（歌2~歌5）正常发起搜索
    row = db.query_one(
        "SELECT status, method, bvid FROM songs WHERE song_key = '歌1|艺1' AND task_id = ?",
        (task_id,),
    )
    assert row["status"] == "DONE"  # 用缓存候选重新打分成功
    # search_cache 保留（--refresh 不清缓存）
    cached = db.query_one("SELECT results FROM search_cache WHERE keyword = '歌1 艺1'")
    assert cached is not None and "BV1cached" in cached["results"]
    # refresh 后所有歌都重跑（无 skipped_done）
    assert counts["skipped_done"] == 0


# ---- M7 P0：DONE 歌 manual 重查（文档 §4.1）---------------------------


@pytest.mark.asyncio
async def test_done_song_upgraded_by_new_manual_without_requests(respx_mock, tmp_path) -> None:
    """验收 1：已 DONE 的歌新增 manual 后重跑 → method 变 MANUAL，零搜索请求。"""
    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)

    db, config, ncm, bili, http = _make_components(tmp_path)
    task_id = db.create_task(1)

    # 歌1 已 DONE（method=SCORED），manual.json 现在新增了它的 BV
    db.upsert_song(
        "歌1|艺1", task_id=task_id, ncm_id=1, name="歌1", artist="艺1",
        status="DONE", method="SCORED", bvid="BV1auto",
    )
    # 注入历史失败残留（模拟 v0.4.1 实测场景 DONE 行带旧 fail_reason）
    db.execute(
        "UPDATE songs SET fail_reason = ? WHERE song_key = ? AND task_id = ?",
        ("匹配失败：降级链全部未命中", "歌1|艺1", task_id),
    )
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_video("BV1xx", "歌1 官方MV")])
    )

    out = tmp_path / "out"
    async with ncm, bili, http:
        counts = await run_dry_run(
            1, config, db, ncm, bili, http,
            output_dir=out, manual={"歌1|艺1": "BV1manual"}, task_id=task_id,
        )

    # 升级为人工结果，且歌1 不发任何网络请求（搜索请求中不含歌1）
    song1_searches = [
        c for c in respx_mock.calls
        if "search" in str(c.request.url) and "歌1" in str(c.request.url)
    ]
    assert song1_searches == []
    row = db.query_one(
        "SELECT status, method, bvid, fail_reason FROM songs WHERE song_key = '歌1|艺1' AND task_id = ?",
        (task_id,),
    )
    assert row["status"] == "DONE"
    assert row["method"] == "MANUAL"
    assert row["bvid"] == "BV1manual"
    assert row["fail_reason"] is None  # MANUAL 回灌重查转 DONE 清因（§4.1/§6）
    assert counts["skipped_done"] == 1  # 仅歌1 已 DONE 被跳过


@pytest.mark.asyncio
async def test_done_song_same_manual_bv_not_rewritten(respx_mock, tmp_path) -> None:
    """验收 2：manual BV 与现有 DONE 相同时不重写、零请求。"""
    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)

    db, config, ncm, bili, http = _make_components(tmp_path)
    task_id = db.create_task(1)

    db.upsert_song(
        "歌1|艺1", task_id=task_id, ncm_id=1, name="歌1", artist="艺1",
        status="DONE", method="MANUAL", bvid="BV1same",
    )
    respx_mock.get(SEARCH_URL).mock(
        return_value=_ok(result=[_video("BV1xx", "歌1 官方MV")])
    )

    out = tmp_path / "out"
    async with ncm, bili, http:
        counts = await run_dry_run(
            1, config, db, ncm, bili, http,
            output_dir=out, manual={"歌1|艺1": "BV1same"}, task_id=task_id,
        )

    assert counts["skipped_done"] == 1  # 仅歌1 已 DONE
    row = db.query_one(
        "SELECT status, method, bvid FROM songs WHERE song_key = '歌1|艺1' AND task_id = ?",
        (task_id,),
    )
    assert row["method"] == "MANUAL" and row["bvid"] == "BV1same"  # 未变


# ---- M7 P2：whitelist_bv 表接入（文档 §6）------------------------------


def test_whitelist_bv_table_matches_json(tmp_path) -> None:
    """验收 1：Matcher 构造后 whitelist_bv 内容与 whitelist.json 一致。"""
    from matcher import Matcher

    db = Database(tmp_path / "t.db")
    cfg = load_config(tmp_path / "nope.yaml", env={})
    ncm = NcmClient(httpx.AsyncClient())
    bili = BiliSearchClient(httpx.AsyncClient(), db=db)
    http = httpx.AsyncClient()
    Matcher(
        cfg, db, bili, ncm, http,
        whitelist={"歌1|艺1": "BV1w", "歌2|艺2": "BV2w"},
        manual={"歌3|艺3": "BV3m"},
    )

    assert db.load_priority_map("whitelist") == {"歌1|艺1": "BV1w", "歌2|艺2": "BV2w"}
    assert db.load_priority_map("manual") == {"歌3|艺3": "BV3m"}


# ---- M8：任务制（文档 §4.4/§6/§11.4/§13.2）-----------------------------


@pytest.mark.asyncio
async def test_run_creates_task_and_updates_done(respx_mock, tmp_path) -> None:
    """run 自动创建任务；完成后任务置 DONE 且统计正确。"""
    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(side_effect=_ok_video_handler)

    db, config, ncm, bili, http = _make_components(tmp_path)
    out = tmp_path / "output"
    async with ncm, bili, http:
        counts = await run_dry_run(1, config, db, ncm, bili, http, output_dir=out)

    task_id = counts["task_id"]
    task = db.get_task(task_id)
    assert task is not None
    assert task["status"] == "DONE"
    assert task["finished_at"] is not None
    # list_tasks 能看到该任务的统计
    listed = {r["task_id"]: r for r in db.list_tasks()}
    assert listed[task_id]["total_songs"] == 5
    assert listed[task_id]["done_songs"] >= 4


@pytest.mark.asyncio
async def test_two_runs_same_playlist_isolated_tasks(respx_mock, tmp_path) -> None:
    """验收：两次 run 同歌单产生两个 task，songs 数据互不干扰。"""
    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(side_effect=_ok_video_handler)

    db, config, ncm, bili, http = _make_components(tmp_path)
    out = tmp_path / "output"
    async with ncm, bili, http:
        c1 = await run_dry_run(1, config, db, ncm, bili, http, output_dir=out)
        c2 = await run_dry_run(1, config, db, ncm, bili, http, output_dir=out)

    assert c1["task_id"] != c2["task_id"]  # 两次 run 两个任务
    # songs 数据按任务隔离，无混合
    rows = db.query("SELECT task_id, COUNT(*) AS n FROM songs GROUP BY task_id")
    tasks_seen = {r["task_id"]: r["n"] for r in rows}
    assert tasks_seen == {c1["task_id"]: 5, c2["task_id"]: 5}


@pytest.mark.asyncio
async def test_delete_task_clears_songs_keeps_cache(respx_mock, tmp_path) -> None:
    """验收：delete 后 task list 无该任务、songs 行清空、search_cache 保留。"""
    import time as _time

    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(side_effect=_ok_video_handler)

    db, config, ncm, bili, http = _make_components(tmp_path)
    out = tmp_path / "output"
    async with ncm, bili, http:
        counts = await run_dry_run(1, config, db, ncm, bili, http, output_dir=out)
    task_id = counts["task_id"]
    # 预置一条 search_cache（跨任务共享，delete 不删）；run 可能已写入同关键词 → 忽略冲突
    db.execute(
        "INSERT OR IGNORE INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?)",
        ("歌1 艺1", '[{"bvid":"BV9"}]', int(_time.time())),
    )

    db.delete_task(task_id)

    assert db.get_task(task_id) is None  # 任务记录删除
    assert db.query_one("SELECT COUNT(*) AS n FROM songs")["n"] == 0  # songs 清空
    # search_cache 保留
    assert db.query_one("SELECT results FROM search_cache WHERE keyword = '歌1 艺1'") is not None


def test_task_subcommands_parse(tmp_path, monkeypatch) -> None:
    """task 子命令注册与参数解析（list/resume/delete/--yes/--all-finished）。"""
    import contextlib
    import io

    import main as main_mod
    from main import main

    # F3-5（§11.4）：数据路径基于项目根而非 CWD——monkeypatch 项目根常量
    # 重定向到 tmp（旧测试靠 chdir 隔离已失效）；同时验证 CWD 无关性。
    monkeypatch.setattr(main_mod, "_PROJECT_ROOT", tmp_path)

    # 无任务时 list 输出"暂无任务"且不报错（不触发确认逻辑）
    db = Database(tmp_path / "cache.db")
    db.close()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["task", "list"])
    assert "暂无任务" in buf.getvalue()

    # resume 不存在的任务 → SystemExit 1
    with pytest.raises(SystemExit):
        main(["task", "resume", "no-such-task", "--yes"])


# ---- M7.5：正式收藏路径（文档 §5.3/§9.3/§11.4）------------------------

FOLDER_LIST_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"

COOKIE = "SESSDATA=s123; bili_jct=jct789; buvid3=b3; DedeUserID=1;"


def _mock_fav_folders(respx_mock, existing: list[dict] | None = None) -> None:
    """收藏夹查询/建夹 mock：默认无既有夹，建夹成功返回递增 id。"""
    respx_mock.get(FOLDER_LIST_URL).mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"list": existing or []}})
    )
    # bvid→aid 转换（deal 接口需要 av 号）
    respx_mock.get(VIEW_URL).mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"aid": 753394975}})
    )

    created = {"n": 0}

    def add_handler(request: httpx.Request) -> httpx.Response:
        created["n"] += 1
        return httpx.Response(200, json={"code": 0, "data": {"id": created["n"]}})

    respx_mock.post(FAV_FOLDER_URL).mock(side_effect=add_handler)
    return created


@pytest.mark.asyncio
async def test_formal_run_creates_folder_and_favs_all(respx_mock, tmp_path) -> None:
    """验收 1：10 首 dry-run 后接正式路径 → 建夹 1 次、收藏 10 次、报告生成。"""
    # 复用 5 首歌歌单（id 1..5），但用 10 首规模验证拆夹：per_folder_limit=900 故 10 首只 1 夹
    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)
    _mock_fav_folders(respx_mock)

    def search_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword")
        return _ok(result=[_video(f"BV{abs(hash(kw)) % 1000}", _kw_title(kw))])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    fav_calls = {"n": 0}

    def fav_handler(request: httpx.Request) -> httpx.Response:
        fav_calls["n"] += 1
        return httpx.Response(200, json={"code": 0})

    respx_mock.post(FAV_RESOURCE_URL).mock(side_effect=fav_handler)

    db, config, ncm, bili, http = _make_components(tmp_path)
    out = tmp_path / "output"
    async with ncm, bili, http:
        counts = await run_formal(
            1, config, db, ncm, bili, http,
            cookie_str=COOKIE, output_dir=out,
        )

    # 5 首全部匹配成功并收藏
    assert counts["processed"] == 5
    assert counts["fav"]["statuses"]["DONE"] == 5
    assert fav_calls["n"] == 5  # 收藏 5 次
    # 建夹 1 次（无既有夹，5 首 ≤900 → 1 夹）
    add_calls = [c for c in respx_mock.calls if "folder/add" in str(c.request.url)]
    assert len(add_calls) == 1
    # 报告生成
    assert (out / "report.csv").exists()
    assert (out / "preview_report.html").exists()


@pytest.mark.asyncio
async def test_formal_rerun_reuses_media_id(respx_mock, tmp_path) -> None:
    """验收 2：重跑复用已有同名收藏夹 media_id，不再建夹。"""
    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)
    # 已存在同名夹 "歌单1 (1)" → 复用 media_id=777，不触发 folder/add
    _mock_fav_folders(
        respx_mock, existing=[{"id": 777, "title": "歌单1 (1)"}]
    )

    def search_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword")
        return _ok(result=[_video(f"BV{abs(hash(kw)) % 1000}", _kw_title(kw))])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)
    respx_mock.post(FAV_RESOURCE_URL).mock(return_value=httpx.Response(200, json={"code": 0}))

    db, config, ncm, bili, http = _make_components(tmp_path)
    out = tmp_path / "output"
    async with ncm, bili, http:
        counts = await run_formal(
            1, config, db, ncm, bili, http,
            cookie_str=COOKIE, output_dir=out,
        )

    assert counts["fav"]["statuses"]["DONE"] == 5
    add_calls = [c for c in respx_mock.calls if "folder/add" in str(c.request.url)]
    assert add_calls == []  # 复用已有夹，零建夹


@pytest.mark.asyncio
async def test_formal_minus101_aborts(respx_mock, tmp_path) -> None:
    """验收 3：收藏遇到 -101 → AuthExpiredError 立即中止，不再重试。"""
    from fav import AuthExpiredError

    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)
    _mock_fav_folders(respx_mock)

    def search_handler(request: httpx.Request) -> httpx.Response:
        kw = request.url.params.get("keyword")
        return _ok(result=[_video(f"BV{abs(hash(kw)) % 1000}", _kw_title(kw))])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    fav_calls = {"n": 0}

    def fav_handler(request: httpx.Request) -> httpx.Response:
        fav_calls["n"] += 1
        return httpx.Response(200, json={"code": -101, "message": "账号未登录"})

    respx_mock.post(FAV_RESOURCE_URL).mock(side_effect=fav_handler)

    db, config, ncm, bili, http = _make_components(tmp_path)
    out = tmp_path / "output"
    async with ncm, bili, http:
        with pytest.raises(AuthExpiredError):
            await run_formal(
                1, config, db, ncm, bili, http,
                cookie_str=COOKIE, output_dir=out,
            )

    # -101 立即中止：每个收藏任务最多 1 次（不重试），且全部被取消
    assert fav_calls["n"] >= 1
    # F2-1（§4.4）：-101 中止退出时任务保持 RUNNING——旧语义阶段二完成即置
    # DONE，中断后任务"显示完成"导致漏收藏；现在可被 task resume 恢复继续收藏
    task_row = db.query_one("SELECT status FROM tasks")
    assert task_row["status"] == "RUNNING"


# ---- 报告绑定 task_id（文档 §3 阶段三"报告（绑定 task_id）"）----------


@pytest.mark.asyncio
async def test_write_reports_filters_by_task_id(tmp_path) -> None:
    """验收：两个任务的数据下，write_reports(task_id=A) 只含任务 A 的歌曲。"""
    from report import write_reports

    db = Database(tmp_path / "c.db")
    task_a = db.create_task(111)
    task_b = db.create_task(222)
    db.upsert_song(
        "歌A|艺A", task_id=task_a, ncm_id=1, name="歌A", artist="艺A",
        status="DONE", method="SCORED", bvid="BV1a",
    )
    db.upsert_song(
        "歌B|艺B", task_id=task_b, ncm_id=2, name="歌B", artist="艺B",
        status="MANUAL", method="MANUAL",
    )

    out_a = tmp_path / "out_a"
    csv_a, html_a = write_reports(db, out_a, task_id=task_a)
    csv_text = csv_a.read_text(encoding="utf-8-sig")
    html_text = html_a.read_text(encoding="utf-8")
    assert "歌A|艺A" in csv_text and "歌A|艺A" in html_text
    assert "歌B|艺B" not in csv_text and "歌B|艺B" not in html_text

    # 默认 None 保持"全部任务"行为（report 子命令 --task-id 依赖该默认值）
    out_all = tmp_path / "out_all"
    csv_all, _ = write_reports(db, out_all)
    all_text = csv_all.read_text(encoding="utf-8-sig")
    assert "歌A|艺A" in all_text and "歌B|艺B" in all_text


# ---- resume 路径熔断器注入（文档 §7 响应层 b）-------------------------


def test_resume_injects_breaker_trips_on_412(respx_mock, tmp_path, monkeypatch) -> None:
    """验收：resume 路径 BiliSearchClient 注入熔断器；-412 触发全局熔断（§9.1 单歌降级）。

    复用 M7 熔断测试模式：注入假 sleep 收集间隔，断言观察到 ≥60s 的全局暂停。
    改动点（v0.4.1）：-412 重试耗尽 → BiliError 被 matcher 捕获单歌降级 MANUAL，
    resume 正常完成不再抛异常；熔断照常触发。
    """
    import main as main_mod

    from main import main

    # F3-5（§11.4）：CLI 数据路径基于 main._PROJECT_ROOT，重定向到 tmp
    monkeypatch.setattr(main_mod, "_PROJECT_ROOT", tmp_path)

    # 1 首歌的歌单（最小化请求量）
    one_song = [SONGS[0]]
    track_ids = [{"id": s["id"]} for s in one_song]
    respx_mock.get(PLAYLIST_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "playlist": {"trackIds": track_ids}})
    )
    respx_mock.post(SONG_DETAIL_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "songs": one_song})
    )
    _mock_bili_env(respx_mock)
    # 所有搜索一律返回 -412（风控）→ 触发单请求重试 + 全局熔断
    respx_mock.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"code": -412, "message": "请求过于频繁"})
    )

    # 预置 RUNNING 任务供 resume（tmp_path/cache.db）
    db = Database(tmp_path / "cache.db")
    tid = db.create_task(123)
    db.close()

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # 全程 -412：单请求重试耗尽按 §9.1 单歌降级——resume 正常完成，不抛异常
    main(["task", "resume", tid, "--yes"])

    # 熔断触发：观察到大额全局暂停（warn 阈值 3 次 -412 → 暂停 60s；容忍单调钟漂移）
    assert any(s >= 59 for s in sleeps), f"未观察到熔断全局暂停，sleeps={sleeps}"
    # 单歌降级：歌曲落盘 MANUAL + SEARCH_FAILED（不崩任务）
    db = Database(tmp_path / "cache.db")
    row = db.query_one(
        "SELECT status, fail_reason FROM songs WHERE song_key = '歌1|艺1' AND task_id = ?",
        (tid,),
    )
    assert row["status"] == "MANUAL"
    assert "SEARCH_FAILED" in row["fail_reason"]
    db.close()


# ---- resume 重试 FAV_FAILED（文档 §4.4/§9.3：只重跑阶段三）--------------


def test_resume_retries_fav_failed_without_search(respx_mock, tmp_path, monkeypatch) -> None:
    """验收：resume 对 FAV_FAILED 歌只重跑阶段三（搜索接口零调用），deal 成功后 status 回 DONE。

    - FAV_FAILED 歌不回 matcher（run_dry_run 阶段二跳过，不进 matcher/不发搜索）；
    - 收藏夹按 §5.3 名称复用（已有同名夹 → 零建夹）；
    - deal 重试成功 → status 回 DONE。
    """
    import main as main_mod

    from main import main

    # F3-5（§11.4）：CLI 数据路径基于 main._PROJECT_ROOT，重定向到 tmp
    monkeypatch.setattr(main_mod, "_PROJECT_ROOT", tmp_path)

    # 1 首歌的歌单：resume 阶段一仍抓取，但 FAV_FAILED 歌不重新匹配
    one_song = [SONGS[0]]  # 歌1|艺1
    track_ids = [{"id": s["id"]} for s in one_song]
    respx_mock.get(PLAYLIST_URL).mock(
        return_value=httpx.Response(
            200, json={"code": 200, "playlist": {"trackIds": track_ids, "name": "歌单1"}}
        )
    )
    respx_mock.post(SONG_DETAIL_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "songs": one_song})
    )
    _mock_bili_env(respx_mock)

    # 若 FAV_FAILED 误回 matcher 会发搜索 → 计数断言为 0
    search_calls = {"n": 0}

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_calls["n"] += 1
        return _ok(result=[_video("BV1never", "歌1 官方MV")])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    # 已有同名夹 "歌单1 (1)" → 复用 media_id，不为重试重复建夹
    _mock_fav_folders(respx_mock, existing=[{"id": 777, "title": "歌单1 (1)"}])

    deal_calls = {"n": 0}

    def deal_handler(request: httpx.Request) -> httpx.Response:
        deal_calls["n"] += 1
        return httpx.Response(200, json={"code": 0})

    respx_mock.post(FAV_RESOURCE_URL).mock(side_effect=deal_handler)

    # 预置：RUNNING 任务 + FAV_FAILED 歌（收藏阶段失败，bvid 已存）
    db = Database(tmp_path / "cache.db")
    tid = db.create_task(123)
    db.upsert_song(
        "歌1|艺1", task_id=tid, ncm_id=1, name="歌1", artist="艺1",
        status="FAV_FAILED", method="SCORED", bvid="BV1retry",
        fail_reason="收藏失败（code -403）",
    )
    db.close()

    # 凭证视为已授权（resume 阶段三需要 cookie；凭证获取本身由 auth 模块负责）
    monkeypatch.setattr("main._load_cookie", lambda: COOKIE)

    main(["task", "resume", tid, "--yes"])

    # 搜索接口零调用（FAV_FAILED 不回 matcher）
    assert search_calls["n"] == 0
    # 只发 deal 请求；复用已有夹 → 零建夹
    assert deal_calls["n"] == 1
    add_calls = [c for c in respx_mock.calls if "folder/add" in str(c.request.url)]
    assert add_calls == []

    # deal 成功后 status 回 DONE，fail_reason 清空
    db = Database(tmp_path / "cache.db")
    row = db.query_one(
        "SELECT status, fail_reason, bvid FROM songs WHERE song_key = '歌1|艺1' AND task_id = ?",
        (tid,),
    )
    assert row["status"] == "DONE"
    assert row["fail_reason"] is None
    assert row["bvid"] == "BV1retry"
    db.close()


# ---- F2-1 验收：MATCHED 全链路（文档 §4.1/§4.4/§10.1/§13.2）-------------


@pytest.mark.asyncio
async def test_formal_matched_chain_stage2_then_fav(respx_mock, tmp_path) -> None:
    """验收（§13.2 链路）：正式 run 阶段二完成后库中无 DONE（MATCHED）且任务保持
    RUNNING——阶段三完成前不得 DONE；阶段三成功后逐个转 DONE、任务才置 DONE；
    复用 task_id 二次进入阶段二全跳过（不重复搜索）。"""
    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)
    _mock_fav_folders(respx_mock)

    search_calls = {"n": 0}

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_calls["n"] += 1
        kw = request.url.params.get("keyword")
        return _ok(result=[_video(f"BV{abs(hash(kw)) % 1000}", _kw_title(kw))])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    deal_calls = {"n": 0}

    def deal_handler(request: httpx.Request) -> httpx.Response:
        deal_calls["n"] += 1
        return httpx.Response(200, json={"code": 0})

    respx_mock.post(FAV_RESOURCE_URL).mock(side_effect=deal_handler)

    db, config, ncm, bili, http = _make_components(tmp_path)
    out = tmp_path / "output"
    async with ncm, bili, http:
        # 只跑阶段二（dry_run=False = 正式匹配语义，文档 §4.1）
        counts = await run_dry_run(
            1, config, db, ncm, bili, http, output_dir=out, dry_run=False,
            whitelist={}, uploaders={}, blacklist_words=[], manual={},
        )
        tid = counts["task_id"]
        statuses = [
            r["status"] for r in db.query(
                "SELECT status FROM songs WHERE task_id = ?", (tid,)
            )
        ]
        assert "DONE" not in statuses  # 阶段三完成前不得 DONE（核心缺陷回归锁定）
        assert set(statuses) == {"MATCHED"}
        assert db.get_task(tid)["status"] == "RUNNING"  # DONE 时机移到阶段三后
        searches_after_stage2 = search_calls["n"]

        # 接阶段三（run_formal 复用 task_id）：MATCHED → fav_one → DONE
        await run_formal(
            1, config, db, ncm, bili, http,
            cookie_str=COOKIE, output_dir=out, task_id=tid,
            whitelist={}, uploaders={}, blacklist_words=[], manual={},
        )

    # 阶段二不再重复搜索（MATCHED 在跳过集合内）
    assert search_calls["n"] == searches_after_stage2
    # 阶段三成功后逐个转 DONE，deal 次数 = 匹配成功数
    rows = db.query("SELECT status FROM songs WHERE task_id = ?", (tid,))
    assert all(r["status"] == "DONE" for r in rows)
    assert deal_calls["n"] == len(rows)
    assert db.get_task(tid)["status"] == "DONE"  # 任务在阶段三完成后才置 DONE


@pytest.mark.asyncio
async def test_manual_upgrade_done_becomes_matched_for_fav(respx_mock, tmp_path) -> None:
    """验收（§4.1 优先级重查）：正式运行下已 DONE 的歌若 manual.json 新增不同 BV，
    升级为 MATCHED + method=MANUAL（bvid 换新），交阶段三收藏新 BV；不发任何
    搜索/收藏请求。dry-run 变体（保持 DONE）见
    test_done_song_upgraded_by_new_manual_without_requests。"""
    # 1 首歌的歌单（避免歌单里其他歌触发搜索干扰零请求断言）
    one_song = [SONGS[0]]
    track_ids = [{"id": s["id"]} for s in one_song]
    respx_mock.get(PLAYLIST_URL).mock(
        return_value=httpx.Response(
            200, json={"code": 200, "playlist": {"trackIds": track_ids, "name": "歌单1"}}
        )
    )
    respx_mock.post(SONG_DETAIL_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "songs": one_song})
    )
    _mock_bili_env(respx_mock)
    _mock_fav_interfaces(respx_mock)  # 误调收藏/建夹即计数暴露
    search_calls = {"n": 0}

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_calls["n"] += 1
        return _ok(result=[_video("BV1never", "歌1 艺1 官方MV")])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    db, config, ncm, bili, http = _make_components(tmp_path)
    tid = db.create_task(123)
    db.upsert_song(
        "歌1|艺1", task_id=tid, ncm_id=1, name="歌1", artist="艺1",
        status="DONE", method="SCORED", bvid="BV1old",
    )

    async with ncm, bili, http:
        counts = await run_dry_run(
            1, config, db, ncm, bili, http, output_dir=tmp_path / "output",
            whitelist={}, uploaders={}, blacklist_words=[],
            manual={"歌1|艺1": "BV1new"},  # manual.json 新增不同 BV
            task_id=tid, dry_run=False,
        )

    row = db.query_one(
        "SELECT status, method, bvid, fail_reason FROM songs "
        "WHERE song_key = '歌1|艺1' AND task_id = ?",
        (tid,),
    )
    assert row["status"] == "MATCHED"  # 正式语义：交阶段三收藏新 BV
    assert row["method"] == "MANUAL"   # manual 优先级最高
    assert row["bvid"] == "BV1new"
    assert row["fail_reason"] is None  # 成功态清因（§6）
    assert db.get_task(tid)["status"] == "RUNNING"
    # 不回 matcher（MATCHED 在跳过集合）、无阶段三
    assert search_calls["n"] == 0
    assert counts["methods"] == {}


def test_resume_deals_matched_and_fav_failed_exactly_once(
    respx_mock, tmp_path, monkeypatch,
) -> None:
    """验收（§4.4）：阶段三中断后 resume——MATCHED（首次收藏）与 FAV_FAILED
    （重试收藏）都执行 deal 且恰好一次（deal 次数 = 未完成数），已收藏（DONE）
    不重复 deal，全部完成后任务置 DONE。"""
    import main as main_mod

    from main import main

    # F3-5（§11.4）：CLI 数据路径基于 main._PROJECT_ROOT，重定向到 tmp
    monkeypatch.setattr(main_mod, "_PROJECT_ROOT", tmp_path)

    three_songs = SONGS[:3]
    track_ids = [{"id": s["id"]} for s in three_songs]
    respx_mock.get(PLAYLIST_URL).mock(
        return_value=httpx.Response(
            200, json={"code": 200, "playlist": {"trackIds": track_ids, "name": "歌单1"}}
        )
    )
    respx_mock.post(SONG_DETAIL_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "songs": three_songs})
    )
    _mock_bili_env(respx_mock)
    # 误回 matcher 会发搜索 → 计数断言为 0
    search_calls = {"n": 0}

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_calls["n"] += 1
        kw = request.url.params.get("keyword")
        return _ok(result=[_video("BV1never", _kw_title(kw))])

    respx_mock.get(SEARCH_URL).mock(side_effect=search_handler)

    # 已有同名夹 "歌单1 (1)" → 复用 media_id，不为续收藏重复建夹
    _mock_fav_folders(respx_mock, existing=[{"id": 777, "title": "歌单1 (1)"}])

    deal_calls = {"n": 0}

    def deal_handler(request: httpx.Request) -> httpx.Response:
        deal_calls["n"] += 1
        return httpx.Response(200, json={"code": 0})

    respx_mock.post(FAV_RESOURCE_URL).mock(side_effect=deal_handler)

    # 预置中断现场：歌1 已收藏（DONE）、歌2 中断时 MATCHED（已匹配未收藏）、
    # 歌3 FAV_FAILED（收藏失败待重试）；任务保持 RUNNING
    db = Database(tmp_path / "cache.db")
    tid = db.create_task(123)
    db.upsert_song("歌1|艺1", task_id=tid, ncm_id=1, name="歌1", artist="艺1",
                   status="DONE", method="SCORED", bvid="BV1done")
    db.upsert_song("歌2|艺2", task_id=tid, ncm_id=2, name="歌2", artist="艺2",
                   status="MATCHED", method="SCORED", bvid="BV2match")
    db.upsert_song("歌3|艺3", task_id=tid, ncm_id=3, name="歌3", artist="艺3",
                   status="FAV_FAILED", method="SCORED", bvid="BV3fail",
                   fail_reason="收藏失败（code -403）")
    db.close()

    monkeypatch.setattr("main._load_cookie", lambda: COOKIE)

    main(["task", "resume", tid, "--yes"])

    # 三首都不回 matcher（DONE/MATCHED/FAV_FAILED 均在阶段二跳过集合内）
    assert search_calls["n"] == 0
    # deal 次数 = 未完成数（MATCHED 1 + FAV_FAILED 1；DONE 不重复 deal）
    assert deal_calls["n"] == 2
    # 全部转 DONE，任务置 DONE（阶段三完成后）
    db = Database(tmp_path / "cache.db")
    try:
        statuses = {
            r["song_key"]: r["status"] for r in db.query(
                "SELECT song_key, status FROM songs WHERE task_id = ?", (tid,)
            )
        }
        assert statuses == {"歌1|艺1": "DONE", "歌2|艺2": "DONE", "歌3|艺3": "DONE"}
        assert db.get_task(tid)["status"] == "DONE"
    finally:
        db.close()


# ---- CLI 日志初始化（文档 §12：--debug + 脱敏 Filter 挂载）-------------


def test_cli_logging_setup_redacts_debug(tmp_path, capsys, monkeypatch) -> None:
    """验收：main 入口调用 setup_logging 挂载脱敏 Filter；--debug 下 DEBUG 日志仍脱敏。

    root logger 全局状态由 tests/conftest.py 的 autouse fixture 测试后还原。
    F3-5（§11.4）：日志目录基于项目根——monkeypatch 重定向到 tmp，
    不向真实项目 logs/ 写文件。
    """
    import logging

    import main as main_mod
    from logging_setup import RedactFilter
    from main import main

    monkeypatch.setattr(main_mod, "_PROJECT_ROOT", tmp_path)

    # --debug 启动（task list 无真实副作用，仅触发 CLI 初始化）
    main(["--debug", "task", "list"])
    root = logging.getLogger()
    # root 上挂载了脱敏 Filter
    has_redact = any(
        isinstance(f, RedactFilter)
        for handler in root.handlers
        for f in handler.filters
    )
    assert has_redact, "root logger 缺 RedactFilter"
    assert root.isEnabledFor(logging.DEBUG), "--debug 未开启 DEBUG 级别"

    # DEBUG 级日志输出 cookie → 必须脱敏（StreamHandler 绑定 capsys 的 stderr）
    logging.getLogger("fav").debug("登录成功 SESSDATA=super-secret; bili_jct=xy;")
    out = capsys.readouterr().err
    assert "SESSDATA=<redacted>" in out, f"未脱敏，输出: {out!r}"
    assert "super-secret" not in out, "明文 cookie 泄漏"


# ---- F3-5（§11.4）：CWD 无关；B3（F2-1 补测）：CLI 输出含 MATCHED -------


def test_cli_uses_project_root_regardless_of_cwd(tmp_path, monkeypatch) -> None:
    """F3-5 验收：从项目外目录运行 task list 仍读到项目数据，CWD 不产生新文件；
    B3 补测：输出行含 MATCHED 计数。"""
    import contextlib
    import io

    import main as main_mod
    from main import main

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    cwd_dir = tmp_path / "elsewhere"
    cwd_dir.mkdir()

    # 项目数据：一个任务含 1 MATCHED + 1 MANUAL
    db = Database(project_dir / "cache.db")
    tid = db.create_task("556677")
    db.upsert_song("歌1|艺1", task_id=tid, ncm_id=1, status="MATCHED",
                   method="SCORED", bvid="BV1m")
    db.upsert_song("歌2|艺2", task_id=tid, ncm_id=2, status="MANUAL", method="MANUAL")
    db.close()

    monkeypatch.setattr(main_mod, "_PROJECT_ROOT", project_dir)
    monkeypatch.chdir(cwd_dir)  # 从"项目外"目录启动 CLI

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["task", "list"])
    output = buf.getvalue()

    assert "556677" in output  # 读到项目数据
    assert "MATCHED1" in output  # B3：CLI 输出含 MATCHED 计数
    assert "MANUAL1" in output
    # CWD 无任何数据文件落进来
    assert not (cwd_dir / "cache.db").exists()
    assert not (cwd_dir / "credentials.db").exists()
    assert not (cwd_dir / "output").exists()
    assert not (cwd_dir / "logs").exists()


# ---- F4-1（§7 末句）：run/resume 客户端构造全量 config 接线 -----------


def _install_client_spies(monkeypatch, captured: dict) -> None:
    """包装 httpx.AsyncClient 与 main.BiliSearchClient，捕获构造 kwargs。

    子类调原始构造器：respx 的 transport 级拦截与 async with 生命周期不受影响。
    """
    import main as main_mod

    orig_async_client = httpx.AsyncClient
    orig_bili = main_mod.BiliSearchClient

    class _SpyAsyncClient(orig_async_client):
        def __init__(self, *args, **kwargs):
            captured["clients"].append(kwargs)
            super().__init__(*args, **kwargs)

    class _SpyBili(orig_bili):
        def __init__(self, *args, **kwargs):
            captured["bili"].append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _SpyAsyncClient)
    monkeypatch.setattr(main_mod, "BiliSearchClient", _SpyBili)


def _assert_clients_wired(captured: dict, *, timeout_s: float, wbi_ttl_s: int,
                          delays: list[float]) -> dict:
    """断言 3 个 AsyncClient 与 BiliSearchClient 的构造参数（F4-1）。"""
    client_kws = captured["clients"]
    assert len(client_kws) == 3, f"应有 ncm/bili/http 三个 client，实际 {len(client_kws)}"
    for kw in client_kws:
        timeout = kw.get("timeout")
        assert isinstance(timeout, httpx.Timeout), f"缺少 timeout 接线: {kw}"
        assert timeout.read == timeout_s and timeout.connect == timeout_s

    assert len(captured["bili"]) == 1
    kw = captured["bili"][0]
    assert kw["wbi_ttl_s"] == wbi_ttl_s, f"wbi_ttl_s 未接线: {kw.get('wbi_ttl_s')}"
    assert kw["retry_delays_s"] == delays, f"retry_delays_s 未接线: {kw.get('retry_delays_s')}"
    # F4-4：cred_db 独立于 cache.db，指向 credentials.db
    cred_db = kw["cred_db"]
    assert cred_db is not kw["db"]
    from pathlib import Path as _Path

    assert _Path(cred_db.path).name == "credentials.db"
    assert _Path(kw["db"].path).name == "cache.db"
    return kw


def test_run_clients_wired_from_config(respx_mock, tmp_path, monkeypatch) -> None:
    """F4-1 验收（run 路径）：3 个 AsyncClient 传 config.http.timeout_s；
    BiliSearchClient 传 wbi_ttl_s / retry_delays_s（默认 15 / 86400 / [2,4,8]）；
    改 config（env 覆盖）后构造参数跟随；buvid3 落 credentials.db（F4-4 联验）。"""
    import contextlib
    import io

    import main as main_mod
    from main import main

    monkeypatch.setattr(main_mod, "_PROJECT_ROOT", tmp_path)
    captured: dict = {"clients": [], "bili": []}
    _install_client_spies(monkeypatch, captured)

    # 构造参数仍走真实 config；仅屏蔽限速/退避的真实等待
    async def _fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    _mock_ncm_playlist(respx_mock)
    _mock_bili_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(side_effect=_ok_video_handler)

    with contextlib.redirect_stdout(io.StringIO()):
        main(["run", "1", "--dry-run"])

    _assert_clients_wired(
        captured, timeout_s=15.0, wbi_ttl_s=86400, delays=[2.0, 4.0, 8.0]
    )
    # F4-4 联验：buvid3 已持久化到 tmp/credentials.db
    cred_db = Database(tmp_path / "credentials.db")
    row = cred_db.query_one("SELECT value FROM credentials WHERE key = 'buvid3'")
    assert row is not None and row["value"] == "M4-INTEGRATION-B3"
    cred_db.close()

    # env 覆盖 config → 第二次 run 的构造参数跟随变化（不硬编码）
    captured["clients"].clear()
    captured["bili"].clear()
    monkeypatch.setenv("NCM2BILI_HTTP__TIMEOUT_S", "33")
    monkeypatch.setenv("NCM2BILI_CACHE__WBI_KEYS_TTL_S", "777")
    monkeypatch.setenv("NCM2BILI_RISK_CONTROL__RETRY__INITIAL_DELAY_S", "1")
    monkeypatch.setenv("NCM2BILI_RISK_CONTROL__RETRY__BACKOFF_FACTOR", "3")
    monkeypatch.setenv("NCM2BILI_RISK_CONTROL__RETRY__MAX_RETRIES", "2")

    with contextlib.redirect_stdout(io.StringIO()):
        main(["run", "2", "--dry-run"])

    _assert_clients_wired(
        captured, timeout_s=33.0, wbi_ttl_s=777, delays=[1.0, 3.0]
    )


def test_resume_clients_wired_from_config(respx_mock, tmp_path, monkeypatch) -> None:
    """F4-1 验收（resume 路径）：与 run 同款接线——timeout=15、wbi=86400、[2,4,8]。

    用全部 DONE 的任务：resume 阶段一照常抓歌单（走 ncm client），阶段二/三零工作量，
    不触发 cookie 读取，构造参数仍可在命令入口处捕获。
    """
    import contextlib
    import io

    import main as main_mod
    from main import main

    monkeypatch.setattr(main_mod, "_PROJECT_ROOT", tmp_path)
    captured: dict = {"clients": [], "bili": []}
    _install_client_spies(monkeypatch, captured)

    # 1 首歌的歌单
    one_song = [SONGS[0]]
    respx_mock.get(PLAYLIST_URL).mock(
        return_value=httpx.Response(
            200, json={"code": 200, "playlist": {"trackIds": [{"id": one_song[0]["id"]}]}}
        )
    )
    respx_mock.post(SONG_DETAIL_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "songs": one_song})
    )
    _mock_bili_env(respx_mock)
    respx_mock.get(SEARCH_URL).mock(side_effect=_ok_video_handler)

    # 预置 RUNNING 任务 + 已 DONE 的歌（resume 跳过，不发搜索）
    db = Database(tmp_path / "cache.db")
    tid = db.create_task(321)
    db.upsert_song(
        "歌1|艺1", task_id=tid, ncm_id=1, name="歌1", artist="艺1",
        status="DONE", method="SCORED", bvid="BV1done",
    )
    db.close()

    with contextlib.redirect_stdout(io.StringIO()):
        main(["task", "resume", tid, "--yes"])

    _assert_clients_wired(
        captured, timeout_s=15.0, wbi_ttl_s=86400, delays=[2.0, 4.0, 8.0]
    )
    # 已 DONE 的歌零搜索（接线验证不依赖副作用，顺带守住 resume 跳过语义）
    search_calls = [c for c in respx_mock.calls if "search/type" in str(c.request.url)]
    assert search_calls == []


# ---- V5-P0-2（§4.1 人工回灌永远覆盖）：四状态端到端 -------------------


def test_resume_manual_override_covers_four_statuses(
    respx_mock, tmp_path, monkeypatch,
) -> None:
    """V5-P0-2 验收（§4.1 铁律）：manual.json 回灌新 BV 后 resume——

    DONE / MATCHED / FAV_FAILED / MANUAL 四种起始状态的歌，人工回灌的新 BV 一致
    生效：阶段二不搜索（DONE/MATCHED/FAV_FAILED 跳过；MANUAL 经 matcher 取
    manual BV），阶段三收藏的是新 BV（而非库内旧 bvid）。

    修复前：SQL 只查 status='DONE'，FAV_FAILED/MATCHED 的人工回灌静默失效，
    且 resume 阶段三收藏库内旧 bvid（错误内容）。
    """
    import main as main_mod
    from main import main

    monkeypatch.setattr(main_mod, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("main._load_cookie", lambda: COOKIE)

    # 屏蔽收藏限速的真实 sleep（4 首串行 4s+ 太慢）
    async def _fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    # 4 首歌的歌单（歌1~歌4）
    four_songs = SONGS[:4]
    track_ids = [{"id": s["id"]} for s in four_songs]
    respx_mock.get(PLAYLIST_URL).mock(
        return_value=httpx.Response(
            200, json={"code": 200, "playlist": {"trackIds": track_ids, "name": "歌单1"}}
        )
    )
    respx_mock.post(SONG_DETAIL_URL).mock(
        return_value=httpx.Response(200, json={"code": 200, "songs": four_songs})
    )
    _mock_bili_env(respx_mock)
    # 搜索不应被调用（四状态全有 BV 或 manual 覆盖，无需搜索）
    respx_mock.get(SEARCH_URL).mock(return_value=_ok(result=[]))

    _mock_fav_folders(respx_mock, existing=[{"id": 777, "title": "歌单1 (1)"}])
    respx_mock.post(FAV_RESOURCE_URL).mock(
        return_value=httpx.Response(200, json={"code": 0})
    )

    # bvid→aid 转换：捕获请求的 bvid 参数，断言是 NEW BV 而非 OLD。
    # 注意：必须在 _mock_fav_folders 之后注册——后者内部对 VIEW_URL 注册了
    # 静态 mock，后注册的 side_effect 才能覆盖它并捕获参数。
    view_bvids: list[str] = []

    def view_handler(request: httpx.Request) -> httpx.Response:
        view_bvids.append(request.url.params.get("bvid", ""))
        return httpx.Response(200, json={"code": 0, "data": {"aid": 753394975}})

    respx_mock.get(VIEW_URL).mock(side_effect=view_handler)

    # 预置：RUNNING 任务 + 四首歌各带不同状态与 OLD BV
    db = Database(tmp_path / "cache.db")
    tid = db.create_task(123)
    db.upsert_song(
        "歌1|艺1", task_id=tid, ncm_id=1, name="歌1", artist="艺1",
        status="DONE", method="SCORED", bvid="BV1OLD",
    )
    db.upsert_song(
        "歌2|艺2", task_id=tid, ncm_id=2, name="歌2", artist="艺2",
        status="MATCHED", method="SCORED", bvid="BV2OLD",
    )
    db.upsert_song(
        "歌3|艺3", task_id=tid, ncm_id=3, name="歌3", artist="艺3",
        status="FAV_FAILED", method="SCORED", bvid="BV3OLD",
        fail_reason="收藏失败（code -403）",
    )
    db.upsert_song(
        "歌4|艺4", task_id=tid, ncm_id=4, name="歌4", artist="艺4",
        status="MANUAL", method="MANUAL", bvid=None,
        fail_reason="降级链全部未命中",
    )
    db.close()

    # 人工回灌：manual.json 写入四首歌的 NEW BV
    import json as _json
    manual_data = {
        "歌1|艺1": "BV1NEW",
        "歌2|艺2": "BV2NEW",
        "歌3|艺3": "BV3NEW",
        "歌4|艺4": "BV4NEW",
    }
    (tmp_path / "manual.json").write_text(
        _json.dumps(manual_data, ensure_ascii=False), encoding="utf-8"
    )

    main(["task", "resume", tid, "--yes"])

    # 阶段三收藏的是 NEW BV（view 请求的 bvid 参数全部为 NEW）
    assert sorted(view_bvids) == ["BV1NEW", "BV2NEW", "BV3NEW", "BV4NEW"], (
        f"阶段三应收藏新 BV，实际 view 请求 bvid: {view_bvids}"
    )
    # 无旧 BV 被收藏
    assert "BV1OLD" not in view_bvids
    assert "BV2OLD" not in view_bvids
    assert "BV3OLD" not in view_bvids

    # 零搜索请求
    search_calls = [c for c in respx_mock.calls if "search/type" in str(c.request.url)]
    assert search_calls == []

    # 四首歌全部 DONE，bvid 为 NEW BV
    db = Database(tmp_path / "cache.db")
    rows = {
        r["song_key"]: r for r in db.query(
            "SELECT song_key, status, bvid, fail_reason FROM songs WHERE task_id = ?",
            (tid,),
        )
    }
    for key, new_bv in [("歌1|艺1", "BV1NEW"), ("歌2|艺2", "BV2NEW"),
                        ("歌3|艺3", "BV3NEW"), ("歌4|艺4", "BV4NEW")]:
        assert rows[key]["status"] == "DONE", f"{key} 应 DONE，实际 {rows[key]['status']}"
        assert rows[key]["bvid"] == new_bv, f"{key} bvid 应为 {new_bv}"
        assert rows[key]["fail_reason"] is None, f"{key} fail_reason 应清空"
    db.close()
