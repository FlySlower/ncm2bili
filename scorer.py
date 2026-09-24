"""两阶段评分（里程碑 M3，对应文档 §4.2）。

阶段一：零额外请求，仅用搜索返回字段计算基础分；
阶段二：仅当 top1/top2 差值小于阈值（config.stage2_diff_threshold）时，
       补调 view（三连细分）与 relation（UP 主粉丝数，按 mid 缓存）精排；
       差值明显时直接用阶段一结果，不发任何额外请求。
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from contextlib import suppress
from typing import Any

import httpx

from config import RateLimitSection, ScoringConfig
from db import Database

# 文档 §5.2 阶段二接口
_VIDEO_DETAIL_URL = "https://api.bilibili.com/x/web-interface/view"
_RELATION_STAT_URL = "https://api.bilibili.com/x/relation/stat"

# 文档 §4.2：标题关键词 bonus 集合
_TITLE_KEYWORDS = ("官方", "原唱", "MV", "音频", "歌词", "完整版")

# 营销号特征阈值（阶段二沉底：粉丝极少）
_SPAM_FOLLOWER_THRESHOLD = 100


def _norm(text: str) -> str:
    """F3-8（§4.2）：标题/歌名归一化，与 MatchGate 闸门 1 同源（matcher._normalize：
    去 HTML/实体、小写、去标点、空白折叠）。函数级导入规避 matcher↔scorer 循环
    导入（matcher 顶部导入 scorer）；sys.modules 缓存后无额外开销。"""
    from matcher import _normalize
    return _normalize(text)


def _parse_duration(raw: Any) -> float:
    """解析 B 站搜索结果的 duration 为秒。

    B 站搜索接口返回 "mm:ss"（如 "4:57"）或 "h:mm:ss" 字符串；
    个别字段也可能是秒数。统一转为 float 秒。
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return 0.0
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0.0
    if len(nums) == 2:
        return float(nums[0] * 60 + nums[1])
    if len(nums) == 3:
        return float(nums[0] * 3600 + nums[1] * 60 + nums[2])
    return float(text) if len(nums) == 1 else 0.0


def stage1_score(video: dict[str, Any], song_name: str, config: ScoringConfig) -> float:
    """文档 §4.2 阶段一评分：零额外请求，仅用搜索返回字段。

    公式：w1·log10(播放+1) + w2·log10(收藏+1) + w3·log10(评论+1)
          + title_bonus（标题含歌名 +10；含 官方/原唱/MV/音频/歌词/完整版 每项 +3）
          + duration_bonus（1~8 分钟 +5；<30s 或 >10min −10）

    V5-P2-8：原 − author_penalty 分支依赖 follower 字段，但搜索 _candidate()
    不产出 follower，该分支在生产链路恒不触发（死配置），随配置项一并删除；
    营销号识别仅保留在阶段二沉底（补查 follower 后，见 rank_candidates）。
    """
    play = float(video.get("play") or 0)
    fav = float(video.get("favorites") or 0)
    reply = float(video.get("reply") or 0)
    title = str(video.get("title") or "")
    duration = _parse_duration(video.get("duration"))

    score = (
        config.w1_play * math.log10(play + 1)
        + config.w2_fav * math.log10(fav + 1)
        + config.w3_reply * math.log10(reply + 1)
    )
    # F3-8（§4.2）：title_bonus 统一经归一化比较（大小写/全半角/标点不敏感），
    # 与 MatchGate 闸门 1 同源 _normalize；修复前为原文子串匹配，大写歌名/小写
    # 标题等差异会漏发 +10/+3。
    norm_title = _norm(title)
    if song_name and _norm(song_name) in norm_title:
        score += config.title_bonus_name
    for kw in _TITLE_KEYWORDS:
        if _norm(kw) in norm_title:
            score += config.title_bonus_keyword
    if 60 <= duration <= 8 * 60:
        score += config.duration_bonus
    elif duration < 30:
        score += config.duration_short_penalty
    elif duration > 10 * 60:
        score += config.duration_long_penalty
    return round(score, 4)


async def _fetch_view(client: httpx.AsyncClient, bvid: str) -> dict:
    """阶段二：视频详情（赞/币/收藏细分）。"""
    resp = await client.get(_VIDEO_DETAIL_URL, params={"bvid": bvid})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise httpx.HTTPStatusError("view API code != 0", request=resp.request, response=resp)
    return data.get("data") or {}


async def fetch_follower_cached(
    client: httpx.AsyncClient, db: Database | None, mid: int
) -> int:
    """文档 §4.2：UP 主粉丝数按 mid 缓存（uploader_cache 表），同一 mid 全程序只查一次。"""
    if db is not None:
        row = db.query_one("SELECT followers FROM uploader_cache WHERE mid = ?", (mid,))
        if row is not None:
            return row["followers"]
    resp = await client.get(_RELATION_STAT_URL, params={"vmid": mid})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise httpx.HTTPStatusError("relation API code != 0", request=resp.request, response=resp)
    follower = int(data["data"]["follower"])
    if db is not None:
        db.execute(
            "INSERT INTO uploader_cache (mid, followers, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(mid) DO UPDATE SET followers = excluded.followers, fetched_at = excluded.fetched_at",
            (mid, follower, int(time.time())),
        )
    return follower


async def rank_candidates(
    candidates: list[dict],
    song_name: str,
    config: ScoringConfig,
    http_client: httpx.AsyncClient | None,
    db: Database | None,
    *,
    rate_limit: RateLimitSection | None = None,
    concurrency_multiplier: float = 1.0,
) -> list[dict]:
    """文档 §4.2 两阶段排序。

    - 先阶段一打分取 top3（config.top_n_stage2）；
    - top1 与 top2 差值比例 >= stage2_diff_threshold → 差值明显，直接返回（0 额外请求）；
    - 差值接近 → 补调 view/relation，营销号沉底后按分数排序。

    F4-3（文档 §7 预防层）：rate_limit 传入时阶段二补查不再裸发请求——每个
    候选补查前 sleep = interval_ms + uniform(jitter_ms)，并以 Semaphore
    (concurrency) 封顶并发；None 时不限速（保留直接调用的测试/兼容路径）。

    V5-P2-9（§7 响应层 b）：concurrency_multiplier 接入熔断降速补偿——critical
    熔断后 multiplier=0.5，阶段二两处补查（view/relation，统一在 _fetch_pair 前
    一次 sleep）间隔翻倍，写法对齐 fav.py（base / multiplier；风控感知请求频率
    而非协程数）。由 matcher 经 self._bili.concurrency_multiplier 传入。
    """
    for cand in candidates:
        cand["score"] = stage1_score(cand, song_name, config)
    candidates.sort(key=lambda c: c["score"], reverse=True)
    top = candidates[: config.top_n_stage2]
    if len(top) < 2:
        return top
    gap_ratio = (top[0]["score"] - top[1]["score"]) / max(top[0]["score"], 1e-9)
    if gap_ratio >= config.stage2_diff_threshold:
        return top  # 差值明显：不发 view/relation 请求
    if http_client is None:
        return top

    # F4-3（§7 预防层）：阶段二补查独立限速——每候选补查前 interval+jitter，
    # 并发上限 rate_limit.concurrency（config.rate_limit.stage2 由 matcher 传入）
    sem = asyncio.Semaphore(rate_limit.concurrency) if rate_limit is not None else None

    async def _enrich(cand: dict) -> None:
        if sem is not None:
            async with sem:
                # V5-P2-9（§7 响应层 b）：熔断 critical 后 multiplier=0.5，
                # 间隔 = (interval+jitter) / multiplier 翻倍（对齐 fav.py）
                await asyncio.sleep(
                    (rate_limit.interval_ms + random.uniform(*rate_limit.jitter_ms))
                    / 1000
                    / concurrency_multiplier
                )
                await _fetch_pair(cand)
        else:
            await _fetch_pair(cand)

    async def _fetch_pair(cand: dict) -> None:
        with suppress(httpx.HTTPError):
            cand["view_detail"] = await _fetch_view(http_client, cand["bvid"])
        with suppress(httpx.HTTPError, KeyError):
            cand["follower"] = await fetch_follower_cached(http_client, db, cand["mid"])

    await asyncio.gather(*(_enrich(cand) for cand in top))
    # 精排：营销号（粉丝极少）沉底，其余按阶段一分数降序
    top.sort(
        key=lambda c: (
            c.get("follower", float("inf")) < _SPAM_FOLLOWER_THRESHOLD,
            -c["score"],
        )
    )
    return top


__all__ = ["stage1_score", "rank_candidates", "fetch_follower_cached"]
