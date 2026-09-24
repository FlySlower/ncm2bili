"""scorer.py 单测（里程碑 M3，文档 §4.2）。"""
from __future__ import annotations

import httpx
import pytest

from config import Config
from scorer import fetch_follower_cached, rank_candidates, stage1_score

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
RELATION_URL = "https://api.bilibili.com/x/relation/stat"


def _ok(**data) -> httpx.Response:
    """构造 B 站 code=0 成功响应（缩短 mock 行长）。"""
    return httpx.Response(200, json={"code": 0, "data": data})


def _video(**kw) -> dict:
    base = {
        "bvid": "BV1test",
        "mid": 1001,
        "title": "夜曲 周杰伦 官方MV",
        "author": "UP主",
        "play": 10000,
        "favorites": 500,
        "reply": 200,
        "duration": 240,  # 4 分钟，落在 1~8min 区间
    }
    base.update(kw)
    return base


def test_stage1_formula() -> None:
    """公式逐项：播放/收藏/评论 log10 + 标题含歌名 +10 + 关键词(官方) +3 + 时长 +5。"""
    cfg = Config().scoring
    v = _video(play=9999, favorites=99, reply=9, title="夜曲 官方", duration=240)
    score = stage1_score(v, "夜曲", cfg)
    expected = 1.0 * 4 + 1.0 * 2 + 0.5 * 1 + 10 + 3 + 5  # log10(10000)+log10(100)+0.5*log10(10)
    assert score == pytest.approx(expected)


def test_stage1_title_bonus_normalized_case_and_punct() -> None:
    """F3-8 验收（§4.2）：大写歌名 + 全小写标题 → title_bonus_name 仍 +10；
    小写 "mv" 仍命中关键词 +3；标点/空白差异不影响（与 MatchGate 闸门 1 同源）。"""
    cfg = Config().scoring
    # 修复前："Hello" 原文不在全小写标题、"MV" 原文不匹配 "mv" → 两个 bonus 全漏
    hit = stage1_score(
        _video(play=0, favorites=0, reply=0, duration=240, title="hello adele mv"),
        "Hello",
        cfg,
    )
    # 对照组：标题不含歌名与任何关键词，其余维度一致
    miss = stage1_score(
        _video(play=0, favorites=0, reply=0, duration=240, title="无关现场"),
        "Hello",
        cfg,
    )
    assert hit - miss == pytest.approx(cfg.title_bonus_name + cfg.title_bonus_keyword)

    # 标点/空白：歌名 "Hello, World" vs 标题 "hello world ..."
    punct_hit = stage1_score(
        _video(play=0, favorites=0, reply=0, duration=240, title="hello world 现场"),
        "Hello, World",
        cfg,
    )
    assert punct_hit - miss == pytest.approx(cfg.title_bonus_name)

    # 反向不误伤：不含归一化歌名的标题不得 +10
    assert stage1_score(
        _video(play=0, favorites=0, reply=0, duration=240, title="hel lo"),
        "Hello",
        cfg,
    ) == pytest.approx(miss)


def test_stage1_duration_mmss_string() -> None:
    """B 站搜索接口 duration 为 "mm:ss" 字符串，须正确换算为秒（真实环境 bug 回归）。"""
    cfg = Config().scoring
    a = stage1_score(_video(duration="4:57", title="夜曲"), "夜曲", cfg)
    b = stage1_score(_video(duration=297, title="夜曲"), "夜曲", cfg)  # 4*60+57
    assert a == pytest.approx(b)
    h = stage1_score(_video(duration="1:02:03", title="夜曲"), "夜曲", cfg)
    assert h == pytest.approx(stage1_score(_video(duration=3723, title="夜曲"), "夜曲", cfg))


def test_stage1_duration_boundaries() -> None:
    """时长阈值：<30s 与 >10min 为 -10；1~8min 为 +5。"""
    cfg = Config().scoring
    short = stage1_score(_video(duration=29, title="夜曲"), "夜曲", cfg)
    normal = stage1_score(_video(duration=240, title="夜曲"), "夜曲", cfg)
    long_ = stage1_score(_video(duration=601, title="夜曲"), "夜曲", cfg)
    assert short == pytest.approx(normal - 5 - 10)  # normal 含 +5，short 含 -10
    assert long_ == pytest.approx(normal - 5 - 10)


def test_stage1_follower_field_has_no_effect_v5p2_8() -> None:
    """V5-P2-8：author_penalty 死分支删除——搜索结果带不带 follower 阶段一分数
    完全一致（follower 仅阶段二补查后用于沉底，不进阶段一公式）。"""
    cfg = Config().scoring
    assert not hasattr(cfg, "author_penalty")  # 配置项已删
    spam_like = _video(follower=10, play=500_000, title="夜曲", duration=240)
    normal = _video(follower=50_000, play=500_000, title="夜曲", duration=240)
    no_follower = _video(play=500_000, title="夜曲", duration=240)
    assert stage1_score(spam_like, "夜曲", cfg) == stage1_score(normal, "夜曲", cfg)
    assert stage1_score(no_follower, "夜曲", cfg) == stage1_score(normal, "夜曲", cfg)


@pytest.mark.asyncio
async def test_stage2_gap_clear_no_extra_requests(respx_mock) -> None:
    """验收：阶段二差值明显时，不发 view/relation 请求（0 次）。"""
    respx_mock.get(VIEW_URL).mock(return_value=_ok())
    respx_mock.get(RELATION_URL).mock(return_value=_ok(follower=1000))

    candidates = [
        _video(bvid="BV1top", play=1_000_000, favorites=10_000, reply=1_000, title="夜曲 官方MV"),
        _video(bvid="BV2low", play=1_000, favorites=10, reply=1, title="夜曲 现场"),
    ]
    ranked = await rank_candidates(candidates, "夜曲", Config().scoring, httpx.AsyncClient(), None)
    assert ranked[0]["bvid"] == "BV1top"
    assert len(respx_mock.calls) == 0  # 无任何额外请求


@pytest.mark.asyncio
async def test_stage2_gap_close_calls_view_relation(respx_mock) -> None:
    """差值接近时补调 view/relation 精排。"""
    respx_mock.get(VIEW_URL).mock(return_value=_ok(like=1))
    respx_mock.get(RELATION_URL).mock(return_value=_ok(follower=1000))

    # 播放/收藏接近且标题 bonus 相同 → top1/top2 差值 <10%，触发阶段二
    candidates = [
        _video(bvid="BV1a", play=1_000, favorites=100, reply=10, title="夜曲 官方版", mid=1),
        _video(bvid="BV2b", play=980, favorites=98, reply=9, title="夜曲 官方现场", mid=2),
    ]
    ranked = await rank_candidates(candidates, "夜曲", Config().scoring, httpx.AsyncClient(), None)
    assert len(ranked) == 2
    assert len(respx_mock.calls) == 4  # 2 候选 × (view + relation)
    assert ranked[0]["view_detail"] == {"like": 1}
    assert ranked[0]["follower"] == 1000


@pytest.mark.asyncio
async def test_stage2_rate_limit_sleeps_and_follows_config(
    respx_mock, monkeypatch,
) -> None:
    """F4-3（§7 预防层）：阶段二补查前按 rate_limit.stage2 sleep（interval+jitter），
    时长随 config 变化；补查请求照常发出（非裸发：每候选先限速）。"""
    import scorer
    from config import RateLimitSection

    respx_mock.get(VIEW_URL).mock(return_value=_ok(like=1))
    respx_mock.get(RELATION_URL).mock(return_value=_ok(follower=1000))

    candidates_factory = lambda: [  # noqa: E731 - 测试夹具：两次运行各用一份（带 score 缓存）
        _video(bvid="BV1a", play=1_000, favorites=100, reply=10, title="夜曲 官方版", mid=1),
        _video(bvid="BV2b", play=980, favorites=98, reply=9, title="夜曲 官方现场", mid=2),
    ]

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(scorer.asyncio, "sleep", fake_sleep)

    client = httpx.AsyncClient()
    async with client:
        # interval=500ms + jitter 固定 0 → 每候选补查前睡 0.5s
        rl = RateLimitSection(concurrency=4, interval_ms=500, jitter_ms=[0, 0])
        ranked = await rank_candidates(
            candidates_factory(), "夜曲", Config().scoring, client, None, rate_limit=rl
        )
        assert len(ranked) == 2
        assert len(respx_mock.calls) == 4  # 2 候选 × (view + relation) 仍照常补查
        assert sorted(sleeps) == [0.5, 0.5]  # 每个候选补查前 sleep 一次

        # config 变化 → sleep 时长跟随（interval=1000ms）
        sleeps.clear()
        respx_mock.calls.clear()
        rl2 = RateLimitSection(concurrency=1, interval_ms=1000, jitter_ms=[0, 0])
        await rank_candidates(
            candidates_factory(), "夜曲", Config().scoring, client, None, rate_limit=rl2
        )
        assert len(respx_mock.calls) == 4
        assert sorted(sleeps) == [1.0, 1.0]


@pytest.mark.asyncio
async def test_stage2_breaker_multiplier_lengthens_interval(
    respx_mock, monkeypatch,
) -> None:
    """V5-P2-9（§7 响应层 b）：critical 熔断后 concurrency_multiplier=0.5，
    阶段二补查（view/relation 统一的一次前置 sleep）间隔翻倍（0.5s → 1.0s），
    写法对齐 fav.py（base / multiplier）；补查请求照常发出。"""
    import scorer
    from config import RateLimitSection

    respx_mock.get(VIEW_URL).mock(return_value=_ok(like=1))
    respx_mock.get(RELATION_URL).mock(return_value=_ok(follower=1000))

    candidates = [
        _video(bvid="BV1a", play=1_000, favorites=100, reply=10, title="夜曲 官方版", mid=1),
        _video(bvid="BV2b", play=980, favorites=98, reply=9, title="夜曲 官方现场", mid=2),
    ]

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(scorer.asyncio, "sleep", fake_sleep)

    rl = RateLimitSection(concurrency=4, interval_ms=500, jitter_ms=[0, 0])
    client = httpx.AsyncClient()
    async with client:
        # 无熔断（multiplier=1.0）：基准 0.5s
        await rank_candidates(
            [dict(c) for c in candidates], "夜曲", Config().scoring, client, None,
            rate_limit=rl, concurrency_multiplier=1.0,
        )
        assert sorted(sleeps) == [0.5, 0.5]

        # critical 熔断（multiplier=0.5）：间隔翻倍到 1.0s
        sleeps.clear()
        respx_mock.calls.clear()
        await rank_candidates(
            [dict(c) for c in candidates], "夜曲", Config().scoring, client, None,
            rate_limit=rl, concurrency_multiplier=0.5,
        )
        assert sorted(sleeps) == [1.0, 1.0]
        assert len(respx_mock.calls) == 4  # view/relation 两处补查照常发出


@pytest.mark.asyncio
async def test_stage2_follower_cached_by_mid(respx_mock, tmp_path) -> None:
    """同一 mid 的粉丝数只查一次 relation（uploader_cache 缓存）。"""
    from db import Database

    db = Database(tmp_path / "test.db")
    respx_mock.get(RELATION_URL).mock(return_value=_ok(follower=42))
    client = httpx.AsyncClient()
    async with client:
        assert await fetch_follower_cached(client, db, 7) == 42
        assert await fetch_follower_cached(client, db, 7) == 42  # 缓存命中
    assert len(respx_mock.calls) == 1

    row = db.query_one("SELECT followers FROM uploader_cache WHERE mid = 7")
    assert row is not None and row["followers"] == 42
