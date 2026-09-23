"""匹配状态机（里程碑 M3，对应文档 §4.1/§4.2/§4.3/§4.4/§9.1）。

闯关流程：WHITELIST_BV → SEARCH → BLACKLIST → UPLOADER_WL → SCORING
         → 降级关键词重试（文档 §4.3，config 驱动）→ MANUAL。

要点：
- 优先级铁律（文档 §4.1 唯一出处）：manual.json > whitelist.json > uploaders.json > 评分；
- 黑名单过滤在 UPLOADER_WL 之前执行（白名单免打分，不免内容审查）；
- 每次状态跃迁立即 db.upsert_song 落盘（§4.4 断点续跑）；
- 降级链轮数与关键词由 config.yaml 的 degrade.keywords 决定（支持
  {name}/{artist} 占位符），{name} 统一经过 sanitize 管线（§4.3）；
- 置信度准入 MatchGate（§4.2）：NO_TITLE_MATCH（含短歌名联合闸）→
  min_score → min_margin 三闸门，每轮内嵌判定，不达标继续降级轮，耗尽置
  MANUAL 并记对应 fail_reason；config.yaml match 段驱动，无硬编码；
- search_cache 缓存正确性（§4.4）：key 为完整 sanitize 后关键词；读写各
  json 序列化/解析保证对象隔离；空结果不缓存、不回退复用其他关键词结果；
  结果归属校验（标题须含歌名 token）拦截上游 stale 结果写入缓存；
- 搜索重试耗尽（BiliError）按 §9.1 单歌降级：status 置 MANUAL、
  fail_reason 记 SEARCH_FAILED，任务继续不中断。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time

from typing import Any

from bili_search import BiliError
from config import Config
from db import Database
from scorer import rank_candidates

logger = logging.getLogger(__name__)


def sanitize_song_name(name: str, keep_tokens: list[str]) -> str:
    """文档 §4.3 关键词卫生：构造降级关键词前的唯一净化入口（全管线无旁路）。

    规则：
    1. 括号注释剥离：全/半角括号内为 CJK 描述性文字（用户自定义标注，如
       "Constriction2.0（压迫感）"）整段删除；括号内容含任一 keep_tokens
       版本信息（不区分大小写，如 Live/Remix/Slowed）则整段保留；
    2. 括号外的 remix/版本信息原样保留（remix 信息优先于歌名本体，防止
       降级时匹配回原曲，文档 §4.3 规则 3）；
    3. 空歌名回退：输入为空返回空串，由降级链退化为艺人关键词搜索；
    4. 只做清洗不追加任何修饰词（追加词仅出自 degrade.keywords 模板）。
    """
    if not name:
        return ""

    keep = [t.lower() for t in keep_tokens if t]
    result = _strip_bracket_comments(name, keep)
    return re.sub(r"\s+", " ", result).strip()


def _strip_bracket_comments(text: str, keep_tokens: list[str]) -> str:
    """逐对剥离括号注释；内容含任一 keep token 的括号整段保留。"""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # 全角（ ）与半角 ( )
        closing = "）" if ch == "（" else ")" if ch == "(" else None
        if closing is not None:
            j = text.find(closing, i + 1)
            if j != -1:
                inner = text[i + 1 : j]
                keep = bool(inner) and any(t in inner.lower() for t in keep_tokens)
                if keep:
                    out.append(text[i : j + 1])  # 版本信息括号整段保留
                # 否则：注释括号整段删除
                i = j + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _normalize(text: str) -> str:
    """标题/歌名归一化（文档 §4.2 token 匹配与 §4.4 归属校验共用）。

    去 HTML 标签（B 站搜索结果标题含 <em class="keyword"> 高亮）、实体解码、
    去标点（保留 CJK 与 ASCII 字母数字）、字母小写、空白折叠。
    """
    t = re.sub(r"<[^>]*>", "", text)
    t = html.unescape(t)
    t = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff\s]", "", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _name_tokens(name: str) -> list[str]:
    """把 sanitize 后歌名切成 token（§4.2/§4.4 共用的匹配粒度）。"""
    return [t for t in _normalize(name).split() if t]


def _count_cjk(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def _title_contains_all(title: str, tokens: list[str]) -> bool:
    """归一化标题是否包含全部歌名 token（全包含/逐 token，文档 §4.2）。"""
    norm = _normalize(title)
    return all(tok in norm for tok in tokens)


# 短歌名联合闸的艺人 token 停用词（防"eason and the duo band"类数据误杀）
_ARTIST_STOPWORDS = frozenset({"the", "and", "with", "feat", "ft", "featuring"})


def _artist_tokens(artist: str) -> list[str]:
    """艺人 token（文档 §4.2 短歌名联合闸用）：过滤停用词与单字符 token。"""
    return [
        t for t in _name_tokens(artist)
        if len(t) > 1 and t not in _ARTIST_STOPWORDS
    ]


def _title_contains_any(title: str, tokens: list[str]) -> bool:
    """归一化标题是否含任一 token（短歌名联合闸：艺人 token 之一命中即可）。"""
    norm = _normalize(title)
    return any(tok in norm for tok in tokens)


class Matcher:
    """匹配状态机。

    Args:
        config: Config 实例（评分/降级参数）。
        db: Database 实例（每次状态跃迁落盘；whitelist_bv 表为优先级统一入口；
            songs 数据带 task_id 作用域，文档 §4.4 任务制）。
        bili_client: BiliSearchClient（搜索，构造注入）。
        ncm_client: NcmClient（构造注入）。
        http_client: httpx.AsyncClient（阶段二 view/relation 精排）。
        task_id: 当前任务标识；缺省 'default'（兼容非任务制调用）。
        whitelist: {"歌名|歌手": "BV号"}；提供时同步进 whitelist_bv 表（source=whitelist）。
        uploaders: {"mid": {"name": ..., "note": ...}}。
        blacklist_words: 黑名单词列表。
        manual: {"歌名|歌手": "BV号"}；提供时同步进 whitelist_bv 表（source=manual）。
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        bili_client: Any,
        ncm_client: Any,
        http_client: Any,
        *,
        task_id: str = "default",
        whitelist: dict | None = None,
        uploaders: dict | None = None,
        blacklist_words: list[str] | None = None,
        manual: dict | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._bili = bili_client
        self._ncm = ncm_client
        self._http = http_client
        self._task_id = task_id
        # 文档 §6：whitelist_bv 表为优先级统一入口，内存 dict 移除。
        # 传入的 whitelist/manual 同步进表，运行时仅从表读取。
        for key, bvid in (whitelist or {}).items():
            self._db.upsert_whitelist_bv(key, bvid, source="whitelist")
        for key, bvid in (manual or {}).items():
            self._db.upsert_whitelist_bv(key, bvid, source="manual")
        # uploaders 的 mid key 归一化为 int（JSON 中可能是字符串），与搜索返回的 int mid 对齐
        self._uploaders = {
            (int(k) if str(k).isdigit() else k): v for k, v in (uploaders or {}).items()
        }
        self._blacklist = [str(w).lower() for w in (blacklist_words or [])]

    def _priority(self) -> tuple[dict[str, str], dict[str, str]]:
        """从 whitelist_bv 表读取优先级映射（manual, whitelist）。"""
        manual = self._db.load_priority_map("manual")
        whitelist = self._db.load_priority_map("whitelist")
        return manual, whitelist

    # ---- 工具 -----------------------------------------------------

    @staticmethod
    def song_key(song: dict) -> str:
        return f"{song['name']}|{song['artist']}"

    def _is_blocked(self, title: str) -> bool:
        t = title.lower()
        return any(w in t for w in self._blacklist)

    def _candidate(self, video: dict) -> dict:
        """把 B 站搜索返回项归一化为评分所需字段（video_review → reply）。"""
        return {
            "bvid": video.get("bvid"),
            "mid": video.get("mid"),
            "title": video.get("title") or "",
            "author": video.get("author") or "",
            "play": video.get("play") or 0,
            "favorites": video.get("favorites") or 0,
            "reply": video.get("video_review") or 0,
            "duration": video.get("duration") or 0,
        }

    def _degrade_keywords(self, song: dict) -> list[str]:
        """文档 §4.3 降级链：从 config.degrade.keywords 取模板并填充占位符。

        {name} 占位符统一经过 sanitize_song_name 净化（全管线无旁路）：
        剥离括号注释、保留版本信息（degrade.keep_tokens）、remix 优先保留；
        追加词仅出自本模板（绝对不在 sanitize 内硬编码任何修饰词）。
        空歌名时 {name} 为空 → 关键词退化为纯艺人搜索（空歌名回退，§4.3）。
        """
        name = sanitize_song_name(
            song.get("name") or "", self._config.degrade.keep_tokens
        )
        artist = song.get("artist") or ""
        keywords: list[str] = []
        for tpl in self._config.degrade.keywords:
            kw = tpl.replace("{name}", name).replace("{artist}", artist).strip()
            if kw:
                keywords.append(kw)
        return keywords

    # ---- 状态机 -----------------------------------------------------

    async def match_song(self, song: dict) -> dict:
        """对一首歌走完整状态机，返回最终结果。

        起始先落盘 PENDING（含元数据），成功 DONE、失败 MANUAL——
        任何时刻中断后重启，从 db 读到的状态即可断点续跑（文档 §4.4）。
        """
        key = self.song_key(song)
        self._db.upsert_song(
            key,
            task_id=self._task_id,
            ncm_id=song.get("ncm_id"),
            name=song.get("name"),
            artist=song.get("artist"),
            album=song.get("album"),
            alia=json.dumps(song.get("alia") or [], ensure_ascii=False),
            origin=json.dumps(song.get("origin") or {}, ensure_ascii=False),
            status="PENDING",
        )

        # 优先级铁律（文档 §4.1）：manual > whitelist（来源：whitelist_bv 表）
        manual, whitelist = self._priority()
        if key in manual:
            return self._finish(key, "MANUAL", manual[key])
        if key in whitelist:
            return self._finish(key, "WHITELIST_BV", whitelist[key])

        blocked_total = 0
        # 降级链（文档 §4.3）：config.degrade.keywords 逐轮降级，每轮走黑名单+评分
        # MatchGate（§4.2）：每轮内嵌三闸门判定，不达标继续下一轮，耗尽置 MANUAL
        last_gate_fail: str | None = None        # 最近一轮闸门拦截原因（MANUAL 时记录）
        last_score_detail: dict | None = None     # 最近一轮评分明细（含 top3，供 review.html）
        try:
            for keyword in self._degrade_keywords(song):
                result = await self._search_and_score(song, keyword)
                blocked_total += result.get("blocked", 0) if result else 0
                if result and result.get("method"):
                    return self._finish(
                        key, result["method"], result["bvid"], result.get("score_detail")
                    )
                if result and result.get("gate_fail"):
                    last_gate_fail = result["gate_fail"]
                    last_score_detail = result.get("score_detail")
        except BiliError as exc:
            # 文档 §9.1：搜索重试耗尽（BiliError）不崩任务——单歌降级 MANUAL，
            # fail_reason 记 SEARCH_FAILED 并携带关键词与根因，任务继续执行。
            fail_reason = f"SEARCH_FAILED: {keyword} {type(exc).__name__}: {exc}"
            self._db.upsert_song(
                key, task_id=self._task_id, status="MANUAL", method="MANUAL",
                fail_reason=fail_reason,
            )
            return {
                "song_key": key,
                "status": "MANUAL",
                "method": "MANUAL",
                "bvid": None,
                "fail_reason": fail_reason,
            }

        # MANUAL（文档 §4.1 第⑦步；§4.2 闸门耗尽记对应 fail_reason + score_detail 落库）
        if last_gate_fail is not None:
            reason = last_gate_fail
            self._db.upsert_song(
                key, task_id=self._task_id, status="MANUAL", method="MANUAL",
                fail_reason=reason,
                score_detail=(
                    json.dumps(last_score_detail, ensure_ascii=False)
                    if last_score_detail else None
                ),
            )
            return {
                "song_key": key,
                "status": "MANUAL",
                "method": "MANUAL",
                "bvid": None,
                "fail_reason": reason,
                "score_detail": last_score_detail,
            }

        reason = "匹配失败：降级链全部未命中"
        if blocked_total:
            reason += f"；黑名单淘汰 {blocked_total} 个候选"
        self._db.upsert_song(
            key, task_id=self._task_id, status="MANUAL", method="MANUAL", fail_reason=reason
        )
        return {
            "song_key": key,
            "status": "MANUAL",
            "method": "MANUAL",
            "bvid": None,
            "fail_reason": reason,
        }

    async def _search_and_score(self, song: dict, keyword: str) -> dict | None:
        """单轮闯关：搜索（走 search_cache，文档 §4.4）→ 黑名单 → uploaders → 评分 + MatchGate。"""
        results = await self._search_cached(song, keyword)
        if not isinstance(results, list) or not results:
            return None
        candidates = [self._candidate(v) for v in results]
        name = sanitize_song_name(
            song.get("name") or "", self._config.degrade.keep_tokens
        )

        # ③ 黑名单过滤（先于 UPLOADER_WL 与评分，文档 §4.1）
        passed, blocked = [], []
        for cand in candidates:
            (blocked if self._is_blocked(cand["title"]) else passed).append(cand)

        # ④ uploaders 命中（跳过打分与 MatchGate，文档 §4.2：白名单路径不经门槛）
        for cand in passed:
            if cand.get("mid") in self._uploaders and name and name in _normalize(cand["title"]):
                return {"method": "UPLOADER_WL", "bvid": cand["bvid"], "blocked": len(blocked)}

        # ⑤ 两阶段评分 + 置信度准入（MatchGate，文档 §4.2）
        if passed:
            ranked = await rank_candidates(
                passed, name, self._config.scoring, self._http, self._db
            )
            best = ranked[0]
            score_detail = {
                "keyword": keyword,
                "score": best.get("score", 0),
                "bvid": best["bvid"],
                "title": best["title"],
                "candidates": [  # top 候选对比（供 preview_report.html / review.html 展示）
                    {"bvid": c["bvid"], "title": c["title"], "score": c.get("score", 0)}
                    for c in ranked
                ],
            }
            gate_fail = self._match_gate_fail(song, ranked)
            if gate_fail is not None:
                # 不达标：继续降级轮；reason + score_detail 留待 MANUAL 落库
                return {
                    "method": None,
                    "gate_fail": gate_fail,
                    "score_detail": score_detail,
                    "blocked": len(blocked),
                }
            if best.get("score", 0) > 0:
                return {
                    "method": "SCORED",
                    "bvid": best["bvid"],
                    "score_detail": score_detail,
                    "blocked": len(blocked),
                }
        return {"method": None, "blocked": len(blocked)} if blocked else None

    def _match_gate_fail(self, song: dict, ranked: list[dict]) -> str | None:
        """文档 §4.2 MatchGate 三闸门判定。

        顺序固定：NO_TITLE_MATCH（含短歌名联合闸）→ min_score → min_margin。
        任一不达标返回 fail_reason 文本（None = 通过准入，可采纳）。
        阈值取自 config.yaml match 段（min_score/min_margin），代码不硬编码。
        匹配规则（全包含/逐 token，见 _name_tokens / _title_contains_all）。
        """
        cfg = self._config.match
        name = sanitize_song_name(
            song.get("name") or "", self._config.degrade.keep_tokens
        )
        artist = song.get("artist") or ""
        name_tokens = _name_tokens(name)

        best = ranked[0]
        title = best.get("title") or ""

        # 闸门 1：相关性前置——标题归一化后须包含歌名主体 token（文档 §4.2）
        if name_tokens and not _title_contains_all(title, name_tokens):
            return f"NO_TITLE_MATCH: {name}"
        # 短歌名联合闸：歌名含 ≤2 个 CJK 字符时，标题还须含艺人 token 之一
        # （防"我们/海胆/黑洞/四季"类泛词短歌名命中同词非歌内容；纯拉丁短名
        #  如 "1-800"/"in heat." 不含 CJK，不受本闸约束）
        if name and name_tokens and 0 < _count_cjk(name) <= 2 and artist:
            artist_tokens = _artist_tokens(artist)
            if artist_tokens and not _title_contains_any(title, artist_tokens):
                return f"NO_TITLE_MATCH: artist={artist}"

        # 闸门 2：低置信（top1 分数门槛）
        top1 = best.get("score", 0)
        if top1 < cfg.min_score:
            return f"LOW_CONFIDENCE: top1={top1}"
        # 闸门 3：头部不分伯仲（top1 与 top2 分差门槛；单候选无竞争天然通过）
        if len(ranked) >= 2:
            top2 = ranked[1].get("score", 0)
            if top1 - top2 < cfg.min_margin:
                return f"LOW_CONFIDENCE: top1={top1} top2={top2}"
        return None

    async def _search_cached(self, song: dict, keyword: str) -> list | None:
        """文档 §4.4 搜索缓存：命中且未过期直接复用；否则发起搜索并写缓存。

        缓存正确性约束（§4.4，v0.4.1 实测一/三批次驱动）：
        - key 为完整 sanitize 后关键词（禁止截断/哈希/过度归一化导致碰撞）；
        - 读路径 json.loads 每次产生全新对象（跨 key 结果对象隔离）；
        - 空结果不写缓存、不回退复用其他关键词的结果；
        - 结果归属校验：结果须与当前歌名相关（标题含歌名 token，与 MatchGate
          同源）——上游可能对不同关键词返回逐字节相同的 stale 内容（实测：
          沙龙/阿牛、单车(Live)/等(Live)），脏缓存作废重取、新结果不写入。
        search_cache 表以 keyword 为 key，TTL 取 config.cache.search_cache_ttl_s
        （默认 7 天）。--refresh 只清匹配结果、不清 search_cache。

        预防层（§7 + F1-1）：sleep 下沉到每次搜索请求——含降级链每一轮、
        缓存命中路径。sleep = interval_ms + uniform(jitter)，作用于本方法
        入口（调用即睡，与是否命中缓存/发真实请求无关）。
        """
        # 文档 §7 预防层 / F1-1：每次搜索请求前 sleep = interval_ms + uniform(jitter)。
        # 下沉到 _search_cached 入口，覆盖降级链每一轮与缓存命中路径（F1-1 明确）。
        rl = self._config.rate_limit.search
        await asyncio.sleep(
            (rl.interval_ms + random.uniform(*rl.jitter_ms)) / 1000
        )

        ttl = self._config.cache.search_cache_ttl_s
        name = sanitize_song_name(
            song.get("name") or "", self._config.degrade.keep_tokens
        )

        row = self._db.query_one(
            "SELECT results, fetched_at FROM search_cache WHERE keyword = ?", (keyword,)
        )
        if row is not None and (time.time() - row["fetched_at"]) < ttl:
            try:
                results = json.loads(row["results"])
            except json.JSONDecodeError:
                results = None  # 缓存损坏则视为未命中
            if isinstance(results, list):
                # F1-2（§4.4）：逐条过滤候选，仅保留归一化标题含全部歌名 token 的项
                owned = self._filter_owned(results, name)
                if owned:
                    return owned
                # 过滤后为空：脏缓存（异关键词结果混入），作废并重新搜索
                logger.warning("search_cache 结果归属校验失败，作废重取: %s", keyword)
                self._db.execute("DELETE FROM search_cache WHERE keyword = ?", (keyword,))

        data = await self._bili.search_videos(keyword)
        results = data.get("result") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return results
        # F1-2（§4.4）：逐条过滤候选；仅缓存过滤后非空结果
        # （空结果不缓存、脏结果不缓存；过滤后为空视为该关键词未命中，照常进入降级链）
        owned = self._filter_owned(results, name)
        if owned:
            self._db.execute(
                "INSERT INTO search_cache (keyword, results, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(keyword) DO UPDATE SET results = excluded.results, "
                "fetched_at = excluded.fetched_at",
                (keyword, json.dumps(owned, ensure_ascii=False), int(time.time())),
            )
        elif not results:
            logger.debug("搜索结果为空，不写入 search_cache: %s", keyword)
        else:
            # 过滤后为空（真实结果不含歌名 token）：视为该关键词未命中，不写入缓存
            logger.warning("搜索结果未通过归属校验，不写入缓存: %s", keyword)
        return owned

    @staticmethod
    def _filter_owned(results: list, name: str) -> list:
        """文档 §4.4 结果归属校验（F1-2 逐条过滤）：仅保留归一化标题包含
        全部歌名 token 的候选。

        与 MatchGate NO_TITLE_MATCH 同源规则，拦截上游对相似关键词返回的逐字节
        相同 stale 内容（实测根因）。缓存读/写同规则：过滤后为空视为该关键词
        未命中（脏缓存行作废重取；真实空结果照常进入降级链）。歌名为空 →
        无法判定，全放行（降级链退化为艺人搜索）。
        """
        tokens = _name_tokens(name)
        if not tokens:
            return list(results)
        return [
            v for v in results
            if all(tok in _normalize(v.get("title") or "") for tok in tokens)
        ]

    def _finish(
        self,
        key: str,
        method: str,
        bvid: str,
        score_detail: dict | None = None,
    ) -> dict:
        self._db.upsert_song(
            key,
            task_id=self._task_id,
            status="DONE",
            method=method,
            bvid=bvid,
            score_detail=json.dumps(score_detail, ensure_ascii=False) if score_detail else None,
            fail_reason=None,
        )
        return {
            "song_key": key,
            "status": "DONE",
            "method": method,
            "bvid": bvid,
            "score_detail": score_detail,
        }

    # ---- 生命周期 ---------------------------------------------------

    async def __aenter__(self) -> Matcher:
        await self._bili.__aenter__()
        await self._ncm.__aenter__()
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._bili.__aexit__(*exc)
        await self._ncm.__aexit__(*exc)
        await self._http.__aexit__(*exc)


__all__ = ["Matcher"]
